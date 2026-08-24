"""Polls simulated (or real) Modbus devices and emits decoded telemetry.

This module contains no reference to the simulator. It is configured from
config/devices.yaml and speaks only Modbus TCP, exactly as it would against
physical hardware.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pymodbus.client import AsyncModbusTcpClient

from collector.batching import ReadBlock, plan_reads
from sim.devices import Endpoint, load_inventory
from sim.regmap import RangeError, RegisterMap, decode, load_map

# How often each class of measurement is polled. This cadence is a property of
# the MONITORING SYSTEM, not a guarantee made by the device.
POLL_INTERVALS = {
    "critical": 1.0,
    "flow": 1.0,
    "status": 2.0,
    "power": 5.0,
    "maintenance": 30.0,
    "config": 60.0,
}

OFFLINE_AFTER_S = 15.0
READ_TIMEOUT_S = 2.0
RETRIES = 2


@dataclass
class DeviceReading:
    device_id: str
    device_type: str
    timestamp: float
    values: dict[str, object]
    online: bool
    bad_points: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    identity_changed: bool = False
    previous_serial: str | None = None


Emitter = Callable[[DeviceReading], Awaitable[None] | None]


@dataclass
class DeviceState:
    endpoint: Endpoint
    regmap: RegisterMap
    plans: dict[str, list[ReadBlock]]
    plan_cache: dict[frozenset, list[ReadBlock]] = field(default_factory=dict)
    client: AsyncModbusTcpClient | None = None
    online: bool = False
    last_success: float = 0.0
    consecutive_failures: int = 0
    known_serial: str | None = None
    values: dict[str, object] = field(default_factory=dict)


class Collector:
    def __init__(self, emit: Emitter, endpoints: list[Endpoint] | None = None) -> None:
        self.emit = emit
        self.endpoints = endpoints or load_inventory()
        self.devices: dict[str, DeviceState] = {}
        self.request_count = 0
        self.poll_count = 0
        self._tasks: list[asyncio.Task] = []
        self._build()

    def _build(self) -> None:
        maps = {t: load_map(t) for t in ("crv", "cdu", "pdu")}
        for ep in self.endpoints:
            regmap = maps[ep.device_type.lower()]
            plans = {}
            for cls in POLL_INTERVALS:
                pts = [p for p in regmap.all_points if p.poll_class == cls]
                if pts:
                    plans[cls] = plan_reads(pts)
            self.devices[ep.device_id] = DeviceState(ep, regmap, plans)

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._poll_device(d)) for d in self.devices.values()
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for d in self.devices.values():
            if d.client:
                d.client.close()
        self._tasks.clear()

    async def poll_once(self, device_id: str, classes: list[str] | None = None) -> None:
        """Poll one device immediately. Used by tests and by manual refresh."""
        dev = self.devices[device_id]
        await self._poll_once(dev, classes or list(dev.plans))

    # -- polling ---------------------------------------------------------

    async def _poll_device(self, dev: DeviceState) -> None:
        due = {cls: 0.0 for cls in dev.plans}
        while True:
            now = time.time()
            classes = [c for c, t in due.items() if now >= t]
            if classes:
                await self._poll_once(dev, classes)
                for c in classes:
                    due[c] = now + POLL_INTERVALS[c]
            await asyncio.sleep(0.1)

    async def _ensure_client(self, dev: DeviceState) -> AsyncModbusTcpClient:
        if dev.client is None or not dev.client.connected:
            dev.client = AsyncModbusTcpClient(
                dev.endpoint.host, port=dev.endpoint.port,
                timeout=READ_TIMEOUT_S, retries=RETRIES,
            )
            await dev.client.connect()
        return dev.client

    def _plan_for(self, dev: DeviceState, classes: list[str]) -> list[ReadBlock]:
        key = frozenset(classes)
        if key not in dev.plan_cache:
            points = [p for cls in classes for p in dev.regmap.all_points
                      if p.poll_class == cls]
            dev.plan_cache[key] = plan_reads(points)
        return dev.plan_cache[key]

    async def _poll_once(self, dev: DeviceState, classes: list[str]) -> None:
        started = time.perf_counter()
        bad: list[str] = []
        ok = True

        try:
            client = await self._ensure_client(dev)
            for block in self._plan_for(dev, classes):
                reader = (client.read_input_registers if block.space == "input"
                          else client.read_holding_registers)
                rr = await reader(block.address, count=block.count,
                                  device_id=dev.endpoint.unit_id)
                self.request_count += 1
                if rr.isError():
                    ok = False
                    continue
                regs = list(rr.registers)
                for p in block.points:
                    lo = p.offset - block.address
                    try:
                        dev.values[p.key] = decode(p, regs[lo:lo + p.width])
                    except RangeError:
                        # Out-of-range raw values are rejected, not passed
                        # silently downstream as if they were measurements.
                        bad.append(p.key)
        except Exception:
            ok = False
            if dev.client:
                dev.client.close()
            dev.client = None

        now = time.time()
        if ok:
            dev.last_success = now
            dev.consecutive_failures = 0
            dev.online = True
        else:
            dev.consecutive_failures += 1
            if now - dev.last_success > OFFLINE_AFTER_S:
                dev.online = False

        # Device substitution: same host, same port, same unit id, different
        # serial number. A failed unit swapped overnight looks identical to the
        # monitoring system unless identity is actually checked.
        identity_changed = False
        previous_serial = None
        serial = dev.values.get("serial_number")
        if isinstance(serial, str) and serial:
            if dev.known_serial is None:
                dev.known_serial = serial
            elif serial != dev.known_serial:
                identity_changed = True
                previous_serial = dev.known_serial
                dev.known_serial = serial

        self.poll_count += 1
        reading = DeviceReading(
            device_id=dev.endpoint.device_id,
            device_type=dev.endpoint.device_type,
            timestamp=now,
            values=dict(dev.values),
            online=dev.online,
            bad_points=bad,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            identity_changed=identity_changed,
            previous_serial=previous_serial,
        )
        result = self.emit(reading)
        if asyncio.iscoroutine(result):
            await result
