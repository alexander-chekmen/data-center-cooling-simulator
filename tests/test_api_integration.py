"""Integration tests for the telemetry API and the simulator control API.

The apps are driven over ASGI (no network listener), but the collector inside
the telemetry app talks to a real Modbus server on a real socket.
"""
import asyncio

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.main import Hub, create_app
from collector.collector import DeviceReading
from sim.admin_api import create_app as create_admin_app
from sim.devices import Endpoint
from sim.engine.state import StepInputs, build_state, step
from sim.modbus_server import DeviceRegisters, ModbusTCPServer
from sim.points import cdu_setpoints, cdu_values, crv_values, pdu_values
from sim.regmap import load_map
from sim.simulator import Simulator

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------- Hub logic

def reading(device_id, device_type, values, online=True):
    return DeviceReading(device_id=device_id, device_type=device_type,
                         timestamp=0.0, values=values, online=online)


def test_hub_sums_it_load_from_rack_pdus_only():
    h = Hub()
    h.on_reading(reading("RACK-A01", "PDU", {"total_power": 30.0}))
    h.on_reading(reading("RACK-A02", "PDU", {"total_power": 12.5}))
    h.on_reading(reading("CRV-001", "CRV", {"cooling_output": 40.0}))
    assert h.snapshot()["summary"]["it_kw"] == pytest.approx(42.5)


def test_hub_sums_cooling_across_crv_and_cdu():
    h = Hub()
    h.on_reading(reading("CRV-001", "CRV", {"cooling_output": 40.0}))
    h.on_reading(reading("CDU-001", "CDU", {"cooling_load": 100.0}))
    assert h.snapshot()["summary"]["cooling_kw"] == pytest.approx(140.0)


def test_hub_excludes_offline_devices_from_totals():
    h = Hub()
    h.on_reading(reading("RACK-A01", "PDU", {"total_power": 30.0}))
    h.on_reading(reading("RACK-A02", "PDU", {"total_power": 99.0}, online=False))
    s = h.snapshot()
    assert s["summary"]["it_kw"] == pytest.approx(30.0)
    assert s["summary"]["offline"] == ["RACK-A02"]


def test_hub_flattens_alarm_words_into_a_list():
    h = Hub()
    h.on_reading(reading("RACK-A01", "PDU",
                         {"alarm_word": ["HIGH_INLET_TEMP", "THERMAL_THROTTLING"]}))
    h.on_reading(reading("CDU-001", "CDU", {"alarm_word": ["PUMP_FAULT"]}))
    s = h.snapshot()
    assert s["summary"]["alarm_count"] == 3
    assert {"device": "CDU-001", "code": "PUMP_FAULT"} in s["alarms"]


def test_pue_is_above_one_and_zero_without_load():
    h = Hub()
    assert h.snapshot()["summary"]["pue"] == 0.0
    h.on_reading(reading("RACK-A01", "PDU", {"total_power": 100.0}))
    h.on_reading(reading("CDU-001", "CDU", {"cooling_load": 100.0}))
    assert h.snapshot()["summary"]["pue"] > 1.0


# ---------------------------------------------------------------- telemetry app

@pytest_asyncio.fixture
async def live_fleet():
    state = build_state()
    warm = StepInputs(load={r.id: (0.5, 0.6) for r in state.topo.racks})
    for _ in range(1800):
        step(state, 1.0, warm)

    regs = {
        "CRV-001": DeviceRegisters("CRV-001", load_map("crv")),
        "CDU-001": DeviceRegisters("CDU-001", load_map("cdu")),
        "RACK-A01": DeviceRegisters("RACK-A01", load_map("pdu")),
    }
    regs["CRV-001"].refresh(crv_values(state, "CRV-001"))
    regs["CDU-001"].refresh(cdu_values(state, "CDU-001"))
    regs["CDU-001"].refresh_setpoints(cdu_setpoints(state, "CDU-001"))
    regs["RACK-A01"].refresh(pdu_values(state, "RACK-A01"))

    server = ModbusTCPServer("127.0.0.1", 0, {1: regs["CRV-001"], 2: regs["CDU-001"],
                                              3: regs["RACK-A01"]})
    await server.start()
    yield [
        Endpoint("CRV-001", "CRV", "127.0.0.1", server.port, 1),
        Endpoint("CDU-001", "CDU", "127.0.0.1", server.port, 2),
        Endpoint("RACK-A01", "PDU", "127.0.0.1", server.port, 3),
    ]
    await server.stop()


async def test_telemetry_app_serves_real_collected_data(live_fleet):
    app = create_app(endpoints=live_fleet)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(1.5)                     # let the collector poll
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://t") as ac:
            health = (await ac.get("/api/health")).json()
            assert health["devices"] == 3
            assert health["online"] == 3
            assert health["modbus_requests"] > 0

            snap = (await ac.get("/api/snapshot")).json()
            assert len(snap["racks"]) == 1
            assert snap["summary"]["it_kw"] > 0
            assert snap["summary"]["cooling_kw"] > 0
            assert snap["racks"][0]["inlet_temp"] > 0


async def test_dashboard_is_served(live_fleet):
    app = create_app(endpoints=live_fleet)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://t") as ac:
            r = await ac.get("/")
            assert r.status_code == 200
            assert "Simulation Control" in r.text


async def test_unknown_device_returns_an_error_object(live_fleet):
    app = create_app(endpoints=live_fleet)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://t") as ac:
            assert "error" in (await ac.get("/api/devices/NOPE-999")).json()


# ---------------------------------------------------------------- control app

@pytest_asyncio.fixture
async def admin():
    """Control API over the physics loop, with no Modbus listeners bound."""
    sim = Simulator(serve_modbus=False)
    app = create_admin_app(sim)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://t") as ac:
            ac.sim = sim
            yield ac


async def test_topology_describes_the_whole_site(admin):
    t = (await admin.get("/admin/topology")).json()
    assert len(t["racks"]) == 24
    assert len(t["crvs"]) == 4
    assert len(t["cdus"]) == 2
    assert len(t["endpoints"]) == 30


async def test_load_control_actually_changes_the_physics(admin):
    """Driven deterministically rather than by sleeping past the simulator's
    background tick -- a timing race here would make CI flaky."""
    r = await admin.post("/admin/load", json={"rows": ["A"], "cpu": 1.0, "gpu": 1.0})
    assert r.json()["pinned"] == 12

    sim = admin.sim
    # current_load() is exactly what the run loop feeds into step().
    assert sim.current_load()["RACK-A03"] == (1.0, 1.0)
    assert sim.current_load()["RACK-B01"] != (1.0, 1.0), "row B must be untouched"

    before = sim.state.racks["RACK-A03"].power_w
    for _ in range(120):
        step(sim.state, 1.0, StepInputs(load=sim.current_load()))

    rack = sim.state.racks["RACK-A03"]
    assert rack.u_cpu_requested == pytest.approx(1.0)
    assert rack.power_w > before
    assert rack.pinned

    state = (await admin.get("/admin/state")).json()
    assert state["racks"]["RACK-A03"]["pinned"] is True


async def test_released_racks_return_to_the_baseline(admin):
    sim = admin.sim
    await admin.post("/admin/load", json={"rows": ["A"], "cpu": 1.0, "gpu": 1.0})
    assert sim.current_load()["RACK-A03"] == (1.0, 1.0)
    await admin.delete("/admin/load")
    assert sim.current_load()["RACK-A03"] != (1.0, 1.0)


async def test_release_unpins_every_rack(admin):
    await admin.post("/admin/load", json={"cpu": 0.9, "gpu": 0.9})
    assert (await admin.delete("/admin/load")).json()["released"] == 24
    assert not any(r.pinned for r in admin.sim.state.racks.values())


async def test_faults_are_registered_and_cleared(admin):
    await admin.post("/admin/faults",
                     json={"device_id": "CDU-001", "kind": "pump_failure"})
    active = (await admin.get("/admin/faults")).json()["active"]
    assert active[0]["device_id"] == "CDU-001"

    await admin.delete("/admin/faults/CDU-001")
    assert (await admin.get("/admin/faults")).json()["active"] == []


async def test_comms_faults_reach_the_register_layer(admin):
    await admin.post("/admin/faults",
                     json={"device_id": "CRV-001", "kind": "device_offline"})
    assert admin.sim.registers["CRV-001"].offline is True
    await admin.delete("/admin/faults")
    assert admin.sim.registers["CRV-001"].offline is False


async def test_unknown_fault_kind_is_rejected(admin):
    r = await admin.post("/admin/faults",
                         json={"device_id": "CDU-001", "kind": "explode"})
    assert r.status_code == 400


async def test_unknown_device_is_rejected(admin):
    r = await admin.post("/admin/faults",
                         json={"device_id": "CDU-999", "kind": "pump_failure"})
    assert r.status_code == 404


async def test_unknown_rack_is_rejected(admin):
    r = await admin.post("/admin/load", json={"racks": ["RACK-Z99"], "cpu": 0.5})
    assert r.status_code == 404


async def test_utilization_above_one_is_rejected(admin):
    assert (await admin.post("/admin/load", json={"cpu": 1.5})).status_code == 422


async def test_reset_restores_defaults(admin):
    await admin.post("/admin/load", json={"cpu": 1.0, "gpu": 1.0})
    await admin.post("/admin/faults", json={"device_id": "CDU-001", "kind": "pump_failure"})
    await admin.post("/admin/room", json={"rh_pct": 90})
    await admin.post("/admin/clock", json={"scale": 30})

    await admin.post("/admin/reset")
    assert admin.sim.overrides == {}
    assert admin.sim.faults == {}
    assert admin.sim.room_rh_override is None
    assert admin.sim.clock.scale == 1.0



# ---- site layout ----------------------------------------------------------

def test_inventory_records_which_unit_serves_each_rack():
    """The dashboard groups the rack map by cooling zone, so this mapping is
    load-bearing for the UI, not decoration."""
    from sim.devices import build_inventory
    racks = {e.device_id: e for e in build_inventory() if e.device_type == "PDU"}
    assert racks["RACK-A01"].crv == "CRV-001"
    assert racks["RACK-A01"].cdu == "CDU-001"
    assert racks["RACK-A06"].cdu == "CDU-001"
    assert racks["RACK-A07"].cdu == "CDU-002", "zone boundary sits between 6 and 7"
    assert racks["RACK-B01"].crv == "CRV-003"
    assert racks["RACK-B01"].cdu is None, "air-cooled racks have no CDU"


def test_every_cooling_zone_holds_six_racks():
    from collections import Counter

    from sim.devices import build_inventory
    racks = [e for e in build_inventory() if e.device_type == "PDU"]
    assert Counter(r.crv for r in racks) == {
        "CRV-001": 6, "CRV-002": 6, "CRV-003": 6, "CRV-004": 6}
    assert Counter(r.cdu for r in racks if r.cdu) == {"CDU-001": 6, "CDU-002": 6}


def test_layout_survives_a_round_trip_through_the_inventory_file(tmp_path):
    """The layout travels with config/devices.yaml, so it has to serialize."""
    from sim.devices import load_inventory, write_inventory
    path = tmp_path / "devices.yaml"
    write_inventory(path)
    racks = {e.device_id: e for e in load_inventory(path) if e.device_type == "PDU"}
    assert racks["RACK-A03"].crv == "CRV-001"
    assert racks["RACK-A03"].cdu == "CDU-001"
    assert racks["RACK-A03"].row == "A" and racks["RACK-A03"].position == 3
    assert racks["RACK-B12"].cdu is None


def test_snapshot_carries_the_layout_for_grouping():
    from sim.devices import build_inventory
    hub = Hub(build_inventory())
    layout = hub.snapshot()["layout"]
    assert len(layout) == 24, "one entry per rack, none for cooling units"
    assert {e["cdu"] for e in layout} == {"CDU-001", "CDU-002", None}
