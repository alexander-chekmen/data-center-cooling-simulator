"""Integration tests for the hand-rolled Modbus TCP server.

Two levels of verification:

* the real pymodbus CLIENT drives the server, so the wire framing is validated
  by an independent implementation rather than by my own encoder;
* raw frames are hand-built for the protocol edge cases the client refuses to
  send (zero count, over-length reads, undefined function codes).
"""
import asyncio
import struct

import pytest
import pytest_asyncio
from pymodbus.client import AsyncModbusTcpClient

from sim.engine.state import StepInputs, build_state, step
from sim.identity import device_identity, rack_asset_tag
from sim.modbus_server import DeviceRegisters, ModbusTCPServer
from sim.points import cdu_setpoints, cdu_values, crv_values, pdu_values
from sim.regmap import decode, load_map

pytestmark = pytest.mark.integration

CRV_UNIT, CDU_UNIT, PDU_A01, PDU_A03 = 1, 2, 3, 4


class Rig:
    def __init__(self, server, state, regs):
        self.server, self.state, self.regs = server, state, regs

    @property
    def port(self):
        return self.server.port

    def refresh(self):
        self.regs["CRV-001"].refresh(crv_values(self.state, "CRV-001"))
        self.regs["CDU-001"].refresh(cdu_values(self.state, "CDU-001"))
        self.regs["RACK-A01"].refresh(pdu_values(self.state, "RACK-A01"))
        self.regs["RACK-A03"].refresh(pdu_values(self.state, "RACK-A03"))

    def advance(self, seconds, load=0.9):
        inputs = StepInputs(load={r.id: (load, load) for r in self.state.topo.racks})
        for _ in range(seconds):
            step(self.state, 1.0, inputs)
        self.refresh()


@pytest_asyncio.fixture
async def rig():
    state = build_state()
    warm = StepInputs(load={r.id: (0.5, 0.6) for r in state.topo.racks})
    for _ in range(1800):
        step(state, 1.0, warm)

    def make(device_id, kind):
        ident = device_identity(device_id, kind)
        return DeviceRegisters(
            device_id, load_map(kind.lower()),
            identity_objects={
                0x00: ident.vendor, 0x01: ident.model, 0x02: ident.firmware,
                0x03: "https://example.invalid/thermaledge",
                0x04: f"{kind} unit (simulated)", 0x05: ident.model,
                0x06: device_id, 0x80: ident.serial,
                0x81: str(ident.hardware_revision),
            },
        )

    regs = {
        "CRV-001": make("CRV-001", "CRV"),
        "CDU-001": make("CDU-001", "CDU"),
        "RACK-A01": make("RACK-A01", "PDU"),
        "RACK-A03": make("RACK-A03", "PDU"),
    }
    regs["CDU-001"].refresh_setpoints(cdu_setpoints(state, "CDU-001"))

    # Port 0 = let the OS pick, so tests never collide with a running simulator.
    server = ModbusTCPServer("127.0.0.1", 0, {
        CRV_UNIT: regs["CRV-001"], CDU_UNIT: regs["CDU-001"],
        PDU_A01: regs["RACK-A01"], PDU_A03: regs["RACK-A03"],
    })
    await server.start()
    r = Rig(server, state, regs)
    r.refresh()
    yield r
    await server.stop()


@pytest_asyncio.fixture
async def client(rig):
    c = AsyncModbusTcpClient("127.0.0.1", port=rig.port, timeout=2.0, retries=1)
    await c.connect()
    yield c
    c.close()


async def raw(port, payload: bytes, txn=0x1234, unit=CRV_UNIT, timeout=2.0):
    """Send a hand-built MBAP frame and return (txn, proto, unit, pdu)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(struct.pack(">HHHB", txn, 0, len(payload) + 1, unit) + payload)
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(7), timeout)
        rtxn, proto, length, runit = struct.unpack(">HHHB", head)
        body = await asyncio.wait_for(reader.readexactly(length - 1), timeout)
        return rtxn, proto, runit, body
    finally:
        writer.close()


# ---------------------------------------------------------------- MBAP framing

async def test_mbap_echoes_transaction_id_and_unit(rig):
    txn, proto, unit, _ = await raw(rig.port, struct.pack(">BHH", 4, 35, 1),
                                    txn=0xBEEF, unit=CRV_UNIT)
    assert txn == 0xBEEF
    assert proto == 0
    assert unit == CRV_UNIT


async def test_mbap_length_field_matches_payload(rig):
    reader, writer = await asyncio.open_connection("127.0.0.1", rig.port)
    writer.write(struct.pack(">HHHB", 1, 0, 6, CRV_UNIT) + struct.pack(">BHH", 4, 35, 3))
    await writer.drain()
    head = await reader.readexactly(7)
    _, _, length, _ = struct.unpack(">HHHB", head)
    body = await reader.readexactly(length - 1)
    writer.close()
    assert len(body) + 1 == length          # unit id counts toward length
    assert body[0] == 4 and body[1] == 6    # fc, byte count for 3 registers


# ---------------------------------------------------------------- reads

async def test_scaled_signed_and_enum_points_round_trip(rig, client):
    m = load_map("crv")
    for key in ("return_air_temp", "supply_air_temp", "return_humidity",
                "fan_speed", "operating_state", "compressor_control_mode"):
        p = m.by_key(key)
        rr = await client.read_input_registers(p.offset, count=p.width, device_id=CRV_UNIT)
        assert not rr.isError(), key
        decode(p, list(rr.registers))       # raises RangeError if malformed


async def test_offsets_match_modicon_over_the_wire(rig, client):
    m = load_map("crv")
    p = m.by_key("return_air_temp")
    assert p.modicon == 30036 and p.offset == 35
    rr = await client.read_input_registers(35, count=1, device_id=CRV_UNIT)
    expected = rig.regs["CRV-001"].input[35]
    assert rr.registers[0] == expected


async def test_uint32_point_spans_two_registers(rig, client):
    p = load_map("crv").by_key("filter_hours")
    rr = await client.read_input_registers(p.offset, count=2, device_id=CRV_UNIT)
    assert decode(p, list(rr.registers)) > 0


async def test_bitfield_alarm_word_decodes(rig, client):
    p = load_map("pdu").by_key("alarm_word")
    rr = await client.read_input_registers(p.offset, count=1, device_id=PDU_A03)
    assert isinstance(decode(p, list(rr.registers)), list)


async def test_batched_block_read_decodes_every_contained_point(rig, client):
    m = load_map("crv")
    rr = await client.read_input_registers(35, count=17, device_id=CRV_UNIT)
    assert len(rr.registers) == 17
    points = m.window("input", 35, 17)
    assert len(points) >= 8
    for p in points:
        lo = p.offset - 35
        decode(p, rr.registers[lo:lo + p.width])


async def test_holding_registers_serve_setpoints(rig, client):
    p = load_map("cdu").by_key("supply_fluid_temp_sp")
    rr = await client.read_holding_registers(p.offset, count=1, device_id=CDU_UNIT)
    assert decode(p, list(rr.registers)) == pytest.approx(28.0, abs=0.1)


# ---------------------------------------------------------------- writes

async def test_write_single_register_round_trips(rig, client):
    p = load_map("cdu").by_key("manual_pump_speed")
    await client.write_register(p.offset, 655, device_id=CDU_UNIT)
    rr = await client.read_holding_registers(p.offset, count=1, device_id=CDU_UNIT)
    assert rr.registers[0] == 655


async def test_write_multiple_registers_round_trips(rig, client):
    await client.write_registers(0, [301, 402], device_id=CDU_UNIT)
    rr = await client.read_holding_registers(0, count=2, device_id=CDU_UNIT)
    assert list(rr.registers) == [301, 402]


# ---------------------------------------------------------------- exceptions

async def test_read_past_end_of_bank_is_illegal_data_address(rig, client):
    rr = await client.read_input_registers(9000, count=2, device_id=CRV_UNIT)
    assert rr.isError()
    assert rr.exception_code == 2


async def test_zero_count_is_illegal_data_value(rig):
    """pymodbus's client refuses to send this, so the frame is built by hand."""
    _, _, _, body = await raw(rig.port, struct.pack(">BHH", 4, 35, 0))
    assert body[0] == 4 | 0x80
    assert body[1] == 3


async def test_count_above_protocol_limit_is_illegal_data_value(rig):
    _, _, _, body = await raw(rig.port, struct.pack(">BHH", 4, 0, 126))
    assert body[0] == 4 | 0x80
    assert body[1] == 3


async def test_undefined_function_code_is_illegal_function(rig):
    _, _, _, body = await raw(rig.port, struct.pack(">BHH", 0x41, 0, 1))
    assert body[0] == 0x41 | 0x80
    assert body[1] == 1


async def test_unknown_unit_id_is_rejected(rig):
    _, _, _, body = await raw(rig.port, struct.pack(">BHH", 4, 35, 1), unit=99)
    assert body[0] == 4 | 0x80
    assert body[1] == 2


# ---------------------------------------------------------------- unit routing

async def test_unit_ids_address_genuinely_different_devices(rig, client):
    """All four devices share one listener, the way a serial gateway fronts many
    field devices behind a single IP."""
    p = load_map("pdu").by_key("inlet_temp")
    a01 = await client.read_input_registers(p.offset, count=1, device_id=PDU_A01)
    a03 = await client.read_input_registers(p.offset, count=1, device_id=PDU_A03)
    assert a01.registers != a03.registers


async def test_same_offset_means_different_things_per_device_type(rig, client):
    """Offset 9 is CDU supply fluid temp and PDU total power. The unit id, not
    the address, decides which."""
    cdu = await client.read_input_registers(9, count=1, device_id=CDU_UNIT)
    pdu = await client.read_input_registers(9, count=1, device_id=PDU_A01)
    assert decode(load_map("cdu").by_key("supply_fluid_temp"), list(cdu.registers)) > 10
    assert decode(load_map("pdu").by_key("total_power"), list(pdu.registers)) > 0


# ---------------------------------------------------------------- live values

async def test_registers_track_the_running_simulation(rig, client):
    p = load_map("pdu").by_key("total_power")
    before = (await client.read_input_registers(p.offset, count=1, device_id=PDU_A03)).registers[0]
    rig.advance(120, load=1.0)
    after = (await client.read_input_registers(p.offset, count=1, device_id=PDU_A03)).registers[0]
    assert after > before, "a read must reflect current state, not a snapshot"


# ---------------------------------------------------------------- fault modes

async def test_offline_device_never_answers(rig):
    rig.regs["CRV-001"].offline = True
    with pytest.raises(asyncio.TimeoutError):
        await raw(rig.port, struct.pack(">BHH", 4, 35, 1), timeout=1.0)


async def test_offline_affects_only_the_faulted_device(rig, client):
    rig.regs["CRV-001"].offline = True
    rr = await client.read_input_registers(9, count=1, device_id=CDU_UNIT)
    assert not rr.isError()


async def test_latency_fault_delays_the_response(rig):
    rig.regs["CRV-001"].latency_s = 0.5
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await raw(rig.port, struct.pack(">BHH", 4, 35, 1), timeout=3.0)
    assert loop.time() - t0 >= 0.45


async def test_frozen_sensor_stops_updating_while_neighbours_continue(rig, client):
    m = load_map("crv")
    frozen = m.by_key("remote_temp_3")
    live = m.by_key("remote_temp_4")
    rig.regs["CRV-001"].frozen_points = {"remote_temp_3"}

    f0 = (await client.read_input_registers(frozen.offset, count=1, device_id=CRV_UNIT)).registers[0]
    l0 = (await client.read_input_registers(live.offset, count=1, device_id=CRV_UNIT)).registers[0]
    rig.advance(180, load=1.0)
    f1 = (await client.read_input_registers(frozen.offset, count=1, device_id=CRV_UNIT)).registers[0]
    l1 = (await client.read_input_registers(live.offset, count=1, device_id=CRV_UNIT)).registers[0]

    assert f1 == f0, "frozen sensor must not move"
    assert l1 != l0, "its neighbour must keep reporting"


# ---------------------------------------------------------------- identity

async def test_identity_registers_carry_model_serial_and_firmware(rig, client):
    m = load_map("crv")
    out = {}
    for key in ("device_model", "serial_number", "firmware_version"):
        p = m.by_key(key)
        rr = await client.read_input_registers(p.offset, count=p.width, device_id=CRV_UNIT)
        out[key] = decode(p, list(rr.registers))
    expected = device_identity("CRV-001", "CRV")
    assert out["serial_number"] == expected.serial
    assert out["device_model"] == expected.model
    assert out["firmware_version"] == expected.firmware


async def test_rack_asset_tag_is_served_separately_from_pdu_serial(rig, client):
    m = load_map("pdu")
    tag_p, ser_p = m.by_key("rack_asset_tag"), m.by_key("serial_number")
    tag = decode(tag_p, list((await client.read_input_registers(
        tag_p.offset, count=tag_p.width, device_id=PDU_A03)).registers))
    ser = decode(ser_p, list((await client.read_input_registers(
        ser_p.offset, count=ser_p.width, device_id=PDU_A03)).registers))
    assert tag == rack_asset_tag("RACK-A03")
    assert tag != ser


async def test_pump_components_expose_their_own_serials_and_run_hours(rig, client):
    m = load_map("cdu")
    vals = {}
    for key in ("pump_a_serial", "pump_b_serial", "pump_a_run_hours",
                "pump_b_run_hours", "pump_a_status", "pump_b_status"):
        p = m.by_key(key)
        rr = await client.read_input_registers(p.offset, count=p.width, device_id=CDU_UNIT)
        vals[key] = decode(p, list(rr.registers))
    assert vals["pump_a_serial"] != vals["pump_b_serial"]
    assert vals["pump_a_run_hours"] != vals["pump_b_run_hours"]
    assert {vals["pump_a_status"], vals["pump_b_status"]} <= {
        "RUNNING", "STANDBY", "STOPPED", "FAULT"}


async def test_read_device_identification_basic(rig, client):
    """Function code 0x2B / MEI 14 -- the standard identity mechanism."""
    rr = await client.read_device_information(read_code=1, device_id=CRV_UNIT)
    assert not rr.isError()
    info = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in rr.information.items()}
    ident = device_identity("CRV-001", "CRV")
    assert info[0x00] == ident.vendor
    assert info[0x01] == ident.model
    assert info[0x02] == ident.firmware


async def test_read_device_identification_regular_objects(rig, client):
    rr = await client.read_device_information(read_code=2, device_id=CDU_UNIT)
    assert not rr.isError()
    assert set(rr.information) == {0x03, 0x04, 0x05, 0x06}


async def test_read_device_identification_extended_carries_serial(rig, client):
    """Serial number has no standard object id, so it lives in the 0x80+
    vendor-specific extended range, as it does on real equipment."""
    rr = await client.read_device_information(read_code=3, device_id=CDU_UNIT)
    assert not rr.isError()
    val = rr.information[0x80]
    serial = val.decode() if isinstance(val, bytes) else val
    assert serial == device_identity("CDU-001", "CDU").serial


async def test_read_device_identification_individual_object(rig, client):
    rr = await client.read_device_information(read_code=4, object_id=0x80,
                                              device_id=PDU_A01)
    assert not rr.isError()
    val = rr.information[0x80]
    serial = val.decode() if isinstance(val, bytes) else val
    assert serial == device_identity("RACK-A01", "PDU").serial


async def test_identity_reports_the_right_device_per_unit_id(rig, client):
    async def serial_of(unit, device_id):
        rr = await client.read_device_information(read_code=3, device_id=unit)
        v = rr.information[0x80]
        return (v.decode() if isinstance(v, bytes) else v)

    assert await serial_of(PDU_A01, "RACK-A01") == device_identity("RACK-A01", "PDU").serial
    assert await serial_of(PDU_A03, "RACK-A03") == device_identity("RACK-A03", "PDU").serial


async def test_unknown_identity_object_is_rejected(rig):
    _, _, _, body = await raw(rig.port, struct.pack(">BBBB", 0x2B, 0x0E, 4, 0x7F))
    assert body[0] == 0x2B | 0x80
    assert body[1] == 2


async def test_wrong_mei_type_is_rejected(rig):
    _, _, _, body = await raw(rig.port, struct.pack(">BBBB", 0x2B, 0x0D, 1, 0))
    assert body[0] == 0x2B | 0x80
    assert body[1] == 1


async def test_invalid_read_device_id_code_is_rejected(rig):
    _, _, _, body = await raw(rig.port, struct.pack(">BBBB", 0x2B, 0x0E, 9, 0))
    assert body[0] == 0x2B | 0x80
    assert body[1] == 3
