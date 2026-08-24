"""Minimal Modbus TCP server serving live simulation state.

Why hand-rolled instead of pymodbus's server: as of pymodbus 3.15 the classic
ModbusDeviceContext / ModbusSparseDataBlock path is deprecated (its
async_getValues returns DEVICE_BUSY), and the replacement SimData/SimDevice API
models static register content rather than values recomputed from live state on
every read. This implementation is ~150 lines, reads straight from the running
simulation, and gives direct control over comms-fault injection (latency,
silence, exception responses) which a stock server would fight.

The collector uses the real pymodbus CLIENT against this, so the wire framing is
continuously validated by an independent implementation.

Frame layout (MBAP header + PDU):

    +----------------+----------------+----------------+----------+
    | transaction id | protocol id (0)| length         | unit id  |
    | 2 bytes        | 2 bytes        | 2 bytes        | 1 byte   |
    +----------------+----------------+----------------+----------+
    | function code  | data ...                                   |
    | 1 byte         |                                            |
    +----------------+--------------------------------------------+
"""
from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field
from typing import Any

from sim.regmap import RegisterMap, encode

# Function codes
READ_HOLDING = 0x03
READ_INPUT = 0x04
WRITE_SINGLE = 0x06
WRITE_MULTIPLE = 0x10
READ_DEVICE_ID = 0x2B          # with MEI type 14, "Read Device Identification"

MEI_DEVICE_ID = 0x0E
CONFORMITY_EXTENDED_INDIVIDUAL = 0x83

# Standard object ids (Modbus Application Protocol, Read Device Identification).
OBJ_VENDOR_NAME = 0x00
OBJ_PRODUCT_CODE = 0x01
OBJ_REVISION = 0x02
OBJ_VENDOR_URL = 0x03
OBJ_PRODUCT_NAME = 0x04
OBJ_MODEL_NAME = 0x05
OBJ_USER_APPLICATION = 0x06
# 0x80+ is the vendor-specific extended range; serial number has no standard id.
OBJ_SERIAL_NUMBER = 0x80
OBJ_HARDWARE_REVISION = 0x81

BASIC, REGULAR, EXTENDED, INDIVIDUAL = 0x01, 0x02, 0x03, 0x04

# Exception codes
ILLEGAL_FUNCTION = 0x01
ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_DATA_VALUE = 0x03
SERVER_FAILURE = 0x04

MAX_READ_REGISTERS = 125          # protocol limit for 16-bit register reads


class ModbusException(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"modbus exception {code}")
        self.code = code


@dataclass
class DeviceRegisters:
    """Register banks for one simulated device, refreshed from SimState."""
    device_id: str
    regmap: RegisterMap
    input: list[int] = field(default_factory=list)
    holding: list[int] = field(default_factory=list)

    # Fault injection hooks
    offline: bool = False           # accept the connection but never answer
    latency_s: float = 0.0          # delay every response
    frozen_points: set[str] = field(default_factory=set)
    identity_objects: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        def size(points):
            return max((p.last_offset for p in points), default=-1) + 1
        if not self.input:
            self.input = [0] * size(self.regmap.points)
        if not self.holding:
            self.holding = [0] * size(self.regmap.setpoints)

    def refresh(self, values: dict[str, Any]) -> None:
        """Encode engineering values into the register banks."""
        for point in self.regmap.points:
            if point.key in self.frozen_points or point.key not in values:
                continue
            for i, word in enumerate(encode(point, values[point.key])):
                self.input[point.offset + i] = word

    def refresh_setpoints(self, values: dict[str, Any]) -> None:
        for point in self.regmap.setpoints:
            if point.key not in values:
                continue
            for i, word in enumerate(encode(point, values[point.key])):
                self.holding[point.offset + i] = word

    def bank(self, function: int) -> list[int]:
        if function == READ_INPUT:
            return self.input
        if function in (READ_HOLDING, WRITE_SINGLE, WRITE_MULTIPLE):
            return self.holding
        raise ModbusException(ILLEGAL_FUNCTION)


class ModbusTCPServer:
    """One TCP listener serving one or more unit ids."""

    def __init__(self, host: str, port: int, devices: dict[int, DeviceRegisters]) -> None:
        self.host = host
        self.port = port
        self.devices = devices
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        # Binding port 0 asks the OS for a free port; record which one we got so
        # tests (and callers) can find the listener.
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header = await reader.readexactly(7)
                txn, proto, length, unit = struct.unpack(">HHHB", header)
                if proto != 0:
                    break
                body = await reader.readexactly(max(0, length - 1))
                if not body:
                    break

                device = self.devices.get(unit)
                if device is None:
                    await self._send_exception(writer, txn, unit, body[0], ILLEGAL_DATA_ADDRESS)
                    continue

                # Comms-failure injection: hold the connection open but never
                # answer, so the collector experiences a real read timeout.
                if device.offline:
                    continue
                if device.latency_s > 0:
                    await asyncio.sleep(device.latency_s)

                try:
                    pdu = self._dispatch(device, body)
                except ModbusException as exc:
                    await self._send_exception(writer, txn, unit, body[0], exc.code)
                    continue

                frame = struct.pack(">HHHB", txn, 0, len(pdu) + 1, unit) + pdu
                writer.write(frame)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    @staticmethod
    async def _send_exception(writer, txn: int, unit: int, function: int, code: int) -> None:
        pdu = struct.pack(">BB", function | 0x80, code)
        writer.write(struct.pack(">HHHB", txn, 0, len(pdu) + 1, unit) + pdu)
        await writer.drain()

    def _dispatch(self, device: DeviceRegisters, body: bytes) -> bytes:
        function = body[0]

        if function in (READ_INPUT, READ_HOLDING):
            address, count = struct.unpack(">HH", body[1:5])
            if not 1 <= count <= MAX_READ_REGISTERS:
                raise ModbusException(ILLEGAL_DATA_VALUE)
            bank = device.bank(function)
            if address < 0 or address + count > len(bank):
                raise ModbusException(ILLEGAL_DATA_ADDRESS)
            words = bank[address:address + count]
            return struct.pack(">BB", function, count * 2) + struct.pack(f">{count}H", *words)

        if function == WRITE_SINGLE:
            address, value = struct.unpack(">HH", body[1:5])
            bank = device.bank(function)
            if address >= len(bank):
                raise ModbusException(ILLEGAL_DATA_ADDRESS)
            bank[address] = value
            return body[:5]

        if function == WRITE_MULTIPLE:
            address, count, byte_count = struct.unpack(">HHB", body[1:6])
            bank = device.bank(function)
            if address + count > len(bank) or byte_count != count * 2:
                raise ModbusException(ILLEGAL_DATA_ADDRESS)
            words = struct.unpack(f">{count}H", body[6:6 + byte_count])
            bank[address:address + count] = list(words)
            return struct.pack(">BHH", function, address, count)

        if function == READ_DEVICE_ID:
            return self._device_identification(device, body)

        raise ModbusException(ILLEGAL_FUNCTION)

    @staticmethod
    def _device_identification(device: DeviceRegisters, body: bytes) -> bytes:
        """Function code 0x2B / MEI type 14.

        The standard way to ask a Modbus device what it is, as opposed to the
        vendor-specific identity registers this server also exposes. Real fleets
        contain both mechanisms, so the project implements both.
        """
        if len(body) < 4:
            raise ModbusException(ILLEGAL_DATA_VALUE)
        mei, read_code, object_id = body[1], body[2], body[3]
        if mei != MEI_DEVICE_ID:
            raise ModbusException(ILLEGAL_FUNCTION)

        objects = device.identity_objects
        if read_code == BASIC:
            selected = {k: v for k, v in objects.items() if k <= OBJ_REVISION}
        elif read_code == REGULAR:
            selected = {k: v for k, v in objects.items()
                        if OBJ_VENDOR_URL <= k <= OBJ_USER_APPLICATION}
        elif read_code == EXTENDED:
            selected = {k: v for k, v in objects.items() if k >= OBJ_SERIAL_NUMBER}
        elif read_code == INDIVIDUAL:
            if object_id not in objects:
                raise ModbusException(ILLEGAL_DATA_ADDRESS)
            selected = {object_id: objects[object_id]}
        else:
            raise ModbusException(ILLEGAL_DATA_VALUE)

        out = bytearray([
            READ_DEVICE_ID, MEI_DEVICE_ID, read_code,
            CONFORMITY_EXTENDED_INDIVIDUAL,
            0x00,               # more follows: no, everything fits one response
            0x00,               # next object id
            len(selected),
        ])
        for oid, value in sorted(selected.items()):
            encoded = str(value).encode("ascii", errors="replace")
            out += bytes([oid, len(encoded)]) + encoded
        return bytes(out)
