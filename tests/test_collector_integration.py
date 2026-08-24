"""Integration tests for the collector against a live Modbus server."""
import asyncio

import pytest
import pytest_asyncio

import collector.collector as collector_mod
from collector.collector import Collector, DeviceReading
from sim.devices import Endpoint
from sim.engine.state import StepInputs, build_state, step
from sim.modbus_server import DeviceRegisters, ModbusTCPServer
from sim.points import cdu_setpoints, cdu_values, crv_values, pdu_values
from sim.regmap import encode_string, load_map

pytestmark = pytest.mark.integration


class Fleet:
    def __init__(self, server, state, regs, endpoints):
        self.server, self.state, self.regs, self.endpoints = server, state, regs, endpoints

    def refresh(self):
        self.regs["CRV-001"].refresh(crv_values(self.state, "CRV-001"))
        self.regs["CDU-001"].refresh(cdu_values(self.state, "CDU-001"))
        self.regs["RACK-A01"].refresh(pdu_values(self.state, "RACK-A01"))

    def advance(self, seconds, load=0.95):
        inputs = StepInputs(load={r.id: (load, load) for r in self.state.topo.racks})
        for _ in range(seconds):
            step(self.state, 1.0, inputs)
        self.refresh()


@pytest_asyncio.fixture
async def fleet():
    state = build_state()
    warm = StepInputs(load={r.id: (0.5, 0.6) for r in state.topo.racks})
    for _ in range(1800):
        step(state, 1.0, warm)

    regs = {
        "CRV-001": DeviceRegisters("CRV-001", load_map("crv")),
        "CDU-001": DeviceRegisters("CDU-001", load_map("cdu")),
        "RACK-A01": DeviceRegisters("RACK-A01", load_map("pdu")),
    }
    regs["CDU-001"].refresh_setpoints(cdu_setpoints(state, "CDU-001"))
    server = ModbusTCPServer("127.0.0.1", 0, {1: regs["CRV-001"], 2: regs["CDU-001"],
                                              3: regs["RACK-A01"]})
    await server.start()
    endpoints = [
        Endpoint("CRV-001", "CRV", "127.0.0.1", server.port, 1),
        Endpoint("CDU-001", "CDU", "127.0.0.1", server.port, 2),
        Endpoint("RACK-A01", "PDU", "127.0.0.1", server.port, 3),
    ]
    f = Fleet(server, state, regs, endpoints)
    f.refresh()
    yield f
    await server.stop()


@pytest_asyncio.fixture
async def readings(fleet):
    """A collector wired to the fixture fleet, recording everything it emits."""
    captured: list[DeviceReading] = []
    c = Collector(emit=captured.append, endpoints=fleet.endpoints)
    c.captured = captured
    yield c
    await c.stop()


async def test_collector_reads_and_decodes_every_device(fleet, readings):
    for ep in fleet.endpoints:
        await readings.poll_once(ep.device_id)
    assert {r.device_id for r in readings.captured} == {"CRV-001", "CDU-001", "RACK-A01"}
    for r in readings.captured:
        assert r.online and not r.bad_points and r.values


async def test_decoded_values_match_the_simulation(fleet, readings):
    await readings.poll_once("RACK-A01")
    r = readings.captured[-1]
    expected = pdu_values(fleet.state, "RACK-A01")
    assert r.values["total_power"] == pytest.approx(expected["total_power"], abs=0.1)
    assert r.values["inlet_temp"] == pytest.approx(expected["inlet_temp"], abs=0.1)


async def test_polling_is_batched_not_one_request_per_point(fleet, readings):
    """The whole reason batching exists: a naive collector issues one request
    per point and collapses at fleet scale."""
    before = readings.request_count
    await readings.poll_once("CRV-001")
    used = readings.request_count - before
    n_points = len(load_map("crv").all_points)
    assert n_points > 20
    assert used <= 4, f"{n_points} points should not cost {used} requests"


async def test_values_track_the_simulation_between_polls(fleet, readings):
    await readings.poll_once("RACK-A01")
    before = readings.captured[-1].values["total_power"]
    fleet.advance(180, load=1.0)
    await readings.poll_once("RACK-A01")
    assert readings.captured[-1].values["total_power"] > before


async def test_out_of_range_values_are_rejected_not_passed_through(fleet, readings):
    """A raw value outside the declared range must be reported as bad, never
    forwarded downstream as if it were a measurement."""
    p = load_map("crv").by_key("return_humidity")     # range 0-100 %RH, scale 10
    fleet.regs["CRV-001"].input[p.offset] = 2000      # 200 %RH
    await readings.poll_once("CRV-001")
    r = readings.captured[-1]
    assert "return_humidity" in r.bad_points
    assert r.values.get("return_humidity", 0) <= 100.0


async def test_offline_device_is_detected(fleet, readings, monkeypatch):
    monkeypatch.setattr(collector_mod, "OFFLINE_AFTER_S", 0.05)
    monkeypatch.setattr(collector_mod, "READ_TIMEOUT_S", 0.3)
    await readings.poll_once("CRV-001")
    assert readings.captured[-1].online

    fleet.regs["CRV-001"].offline = True
    await asyncio.sleep(0.1)
    await readings.poll_once("CRV-001")
    assert not readings.captured[-1].online


async def test_device_recovers_after_the_fault_clears(fleet, readings, monkeypatch):
    monkeypatch.setattr(collector_mod, "OFFLINE_AFTER_S", 0.05)
    monkeypatch.setattr(collector_mod, "READ_TIMEOUT_S", 0.3)
    fleet.regs["CRV-001"].offline = True
    await asyncio.sleep(0.1)
    await readings.poll_once("CRV-001")
    assert not readings.captured[-1].online

    fleet.regs["CRV-001"].offline = False
    await readings.poll_once("CRV-001")
    assert readings.captured[-1].online


async def test_one_offline_device_does_not_stall_the_others(fleet, readings, monkeypatch):
    monkeypatch.setattr(collector_mod, "READ_TIMEOUT_S", 0.3)
    fleet.regs["CRV-001"].offline = True
    await readings.poll_once("CDU-001")
    assert readings.captured[-1].device_id == "CDU-001"
    assert readings.captured[-1].online


async def test_latency_is_measured(fleet, readings):
    fleet.regs["CDU-001"].latency_s = 0.25
    await readings.poll_once("CDU-001")
    assert readings.captured[-1].latency_ms >= 200


async def test_background_loop_polls_all_devices(fleet, readings):
    await readings.start()
    await asyncio.sleep(1.6)
    assert {r.device_id for r in readings.captured} == {"CRV-001", "CDU-001", "RACK-A01"}
    assert readings.poll_count >= 3


# ---------------------------------------------------------------- identity

async def test_collector_records_device_serial(fleet, readings):
    from sim.identity import device_identity
    await readings.poll_once("CRV-001")
    r = readings.captured[-1]
    assert r.values["serial_number"] == device_identity("CRV-001", "CRV").serial
    assert not r.identity_changed


async def test_silent_device_substitution_is_detected(fleet, readings):
    """A failed unit is swapped overnight. Same host, same port, same unit id,
    same registers -- only the serial number differs. The monitoring system must
    notice, or it is reporting on equipment it cannot actually identify.
    """
    await readings.poll_once("CRV-001")
    original = readings.captured[-1].values["serial_number"]
    assert not readings.captured[-1].identity_changed

    p = load_map("crv").by_key("serial_number")
    replacement = "TE-CRV-DEAD-BEEF01"
    for i, word in enumerate(encode_string(replacement, p.chars)):
        fleet.regs["CRV-001"].input[p.offset + i] = word

    await readings.poll_once("CRV-001")
    swapped = readings.captured[-1]
    assert swapped.identity_changed, "device substitution went unnoticed"
    assert swapped.previous_serial == original
    assert swapped.values["serial_number"] == replacement


async def test_identity_change_is_reported_once_not_every_poll(fleet, readings):
    await readings.poll_once("CRV-001")
    p = load_map("crv").by_key("serial_number")
    for i, word in enumerate(encode_string("TE-CRV-0000-000001", p.chars)):
        fleet.regs["CRV-001"].input[p.offset + i] = word

    await readings.poll_once("CRV-001")
    assert readings.captured[-1].identity_changed
    await readings.poll_once("CRV-001")
    assert not readings.captured[-1].identity_changed, "should not re-fire"
