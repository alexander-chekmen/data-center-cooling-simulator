"""The running simulator: physics loop, Modbus listeners, fault state."""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field

from sim.devices import Endpoint, build_inventory
from sim.engine.clock import ScaledClock
from sim.engine.state import Fault, SimState, StepInputs, build_state, step
from sim.faults import CATALOGUE
from sim.identity import device_identity
from sim.modbus_server import DeviceRegisters, ModbusTCPServer
from sim.points import cdu_setpoints, cdu_values, crv_values, pdu_values
from sim.regmap import load_map

# Derived from the catalogue so the split has exactly one definition.
PHYSICS_FAULTS = {k for k, f in CATALOGUE.items() if f.layer == "physics"}
COMMS_FAULTS = {k for k, f in CATALOGUE.items() if f.layer == "comms"}

DEFAULT_LOAD = {"gpu": (0.35, 0.45), "cpu": (0.40, 0.0)}


@dataclass
class LoadOverride:
    u_cpu: float
    u_gpu: float
    pinned: bool = True


@dataclass
class Simulator:
    dt: float = 1.0
    serve_modbus: bool = True
    state: SimState = field(default=None)
    clock: ScaledClock = field(default=None)
    overrides: dict[str, LoadOverride] = field(default_factory=dict)
    faults: dict[str, Fault] = field(default_factory=dict)
    room_rh_override: float | None = None
    registers: dict[str, DeviceRegisters] = field(default_factory=dict)
    endpoints: list[Endpoint] = field(default_factory=list)
    servers: list[ModbusTCPServer] = field(default_factory=list)
    steps: int = 0
    _task: asyncio.Task | None = None

    def __post_init__(self) -> None:
        now = time.time()
        self.state = build_state(t0=now)
        self.clock = ScaledClock(scale=1.0, start=now)
        self.endpoints = build_inventory(self.state.topo, host="0.0.0.0")
        self._build_registers()

    # -- register banks ---------------------------------------------------

    def _build_registers(self) -> None:
        maps = {t: load_map(t) for t in ("crv", "cdu", "pdu")}
        for ep in self.endpoints:
            ident = device_identity(ep.device_id, ep.device_type)
            self.registers[ep.device_id] = DeviceRegisters(
                device_id=ep.device_id,
                regmap=maps[ep.device_type.lower()],
                identity_objects={
                    0x00: ident.vendor,
                    0x01: ident.model,
                    0x02: ident.firmware,
                    0x03: "https://example.invalid/thermaledge",
                    0x04: f"{ep.device_type} cooling unit (simulated)",
                    0x05: ident.model,
                    0x06: ep.device_id,
                    0x80: ident.serial,
                    0x81: str(ident.hardware_revision),
                },
            )
        for cdu in self.state.topo.cdus:
            self.registers[cdu.id].refresh_setpoints(cdu_setpoints(self.state, cdu.id))

    def refresh_registers(self) -> None:
        for crv in self.state.topo.crvs:
            self.registers[crv.id].refresh(crv_values(self.state, crv.id))
        for cdu in self.state.topo.cdus:
            self.registers[cdu.id].refresh(cdu_values(self.state, cdu.id))
        for rack in self.state.topo.racks:
            self.registers[rack.id].refresh(pdu_values(self.state, rack.id))

    # -- load ------------------------------------------------------------

    def current_load(self) -> dict[str, tuple[float, float]]:
        """Baseline load with a gentle diurnal wobble, plus any overrides."""
        phase = math.sin(2 * math.pi * (self.state.t % 86400) / 86400.0)
        out: dict[str, tuple[float, float]] = {}
        for spec in self.state.topo.racks:
            if spec.id in self.overrides:
                o = self.overrides[spec.id]
                out[spec.id] = (o.u_cpu, o.u_gpu)
                continue
            base_cpu, base_gpu = DEFAULT_LOAD[spec.profile]
            out[spec.id] = (
                max(0.02, min(1.0, base_cpu + 0.10 * phase)),
                max(0.0, min(1.0, base_gpu + 0.12 * phase)) if base_gpu else 0.0,
            )
        return out

    # -- faults ----------------------------------------------------------

    def set_fault(self, device_id: str, kind: str, params: dict | None = None) -> None:
        params = params or {}
        self.faults[device_id] = Fault(kind=kind, params=params)
        self._apply_comms_faults()

    def clear_fault(self, device_id: str) -> None:
        self.faults.pop(device_id, None)
        self._apply_comms_faults()

    def clear_all_faults(self) -> None:
        self.faults.clear()
        self._apply_comms_faults()

    def _apply_comms_faults(self) -> None:
        for reg in self.registers.values():
            reg.offline = False
            reg.latency_s = 0.0
            reg.frozen_points = set()
        for device_id, fault in self.faults.items():
            reg = self.registers.get(device_id)
            if reg is None:
                continue
            if fault.kind in ("device_offline", "comms_failure"):
                reg.offline = True
            elif fault.kind == "network_latency":
                reg.latency_s = float(fault.params.get("ms", 800)) / 1000.0
            elif fault.kind == "sensor_failure":
                point = fault.params.get("point")
                reg.frozen_points = {point} if point else set()
            # pump_failure / flow_clamp / fan_failure / airflow_clamp act on the
            # physics engine, not the comms layer, and are applied in step().

    def physics_faults(self) -> dict[str, Fault]:
        return {d: f for d, f in self.faults.items() if f.kind in PHYSICS_FAULTS}

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if not self.serve_modbus:
            self.refresh_registers()
            self._task = asyncio.create_task(self._run())
            return
        by_port: dict[int, dict[int, DeviceRegisters]] = {}
        for ep in self.endpoints:
            by_port.setdefault(ep.port, {})[ep.unit_id] = self.registers[ep.device_id]
        for port, devices in sorted(by_port.items()):
            server = ModbusTCPServer("0.0.0.0", port, devices)
            await server.start()
            self.servers.append(server)
        self.refresh_registers()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for server in self.servers:
            await server.stop()
        self.servers.clear()

    async def _run(self) -> None:
        while True:
            inputs = StepInputs(
                load=self.current_load(),
                faults=self.physics_faults(),
                room_rh_pct=self.room_rh_override,
            )
            step(self.state, self.dt, inputs)
            self.refresh_registers()
            self.steps += 1
            await asyncio.sleep(self.dt / self.clock.scale)
