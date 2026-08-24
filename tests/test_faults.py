"""Fault catalogue: applicability, parameter validation, and API enforcement."""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from sim.admin_api import create_app
from sim.faults import CATALOGUE, GROUPS, FaultValidationError, validate
from sim.simulator import COMMS_FAULTS, PHYSICS_FAULTS, Simulator

# ---- the catalogue itself -------------------------------------------------

def test_every_fault_belongs_to_exactly_one_group():
    groups = {g["id"] for g in GROUPS}
    for spec in CATALOGUE.values():
        assert spec.group in groups, spec.kind


def test_equipment_faults_are_scoped_to_their_equipment():
    """A pump failure means nothing to an air cooler. Offering it anyway
    produces a control that reports success and does nothing."""
    assert CATALOGUE["pump_failure"].device_types == {"CDU"}
    assert CATALOGUE["flow_clamp"].device_types == {"CDU"}
    assert CATALOGUE["fan_failure"].device_types == {"CRV"}
    assert CATALOGUE["airflow_clamp"].device_types == {"CRV"}


def test_comms_faults_apply_to_every_device_type():
    for kind in ("sensor_failure", "network_latency", "device_offline"):
        assert CATALOGUE[kind].device_types == {"CRV", "CDU", "PDU"}


def test_physics_and_comms_split_is_derived_from_the_catalogue():
    assert PHYSICS_FAULTS == {k for k, f in CATALOGUE.items() if f.layer == "physics"}
    assert COMMS_FAULTS == {k for k, f in CATALOGUE.items() if f.layer == "comms"}
    assert not (PHYSICS_FAULTS & COMMS_FAULTS)


def test_every_fault_has_a_description():
    for spec in CATALOGUE.values():
        assert len(spec.description) > 30, spec.kind


# ---- validation -----------------------------------------------------------

@pytest.mark.parametrize("kind,device_type", [
    ("pump_failure", "CRV"), ("flow_clamp", "PDU"),
    ("fan_failure", "CDU"), ("airflow_clamp", "PDU"),
])
def test_mismatched_device_type_is_rejected(kind, device_type):
    with pytest.raises(FaultValidationError, match="does not apply"):
        validate(kind, device_type, {}, set())


def test_error_message_names_the_applicable_devices():
    with pytest.raises(FaultValidationError, match="applies to: CDU"):
        validate("pump_failure", "CRV", {}, set())


def test_defaults_are_applied_when_params_are_omitted():
    assert validate("flow_clamp", "CDU", {}, set()) == {"max_pct": 25.0}
    assert validate("pump_failure", "CDU", {}, set()) == {"pump": "all"}


def test_choice_parameter_is_constrained():
    assert validate("pump_failure", "CDU", {"pump": "B"}, set()) == {"pump": "B"}
    with pytest.raises(FaultValidationError, match="must be one of"):
        validate("pump_failure", "CDU", {"pump": "Z"}, set())


@pytest.mark.parametrize("value", [-1, 101])
def test_number_parameter_range_is_enforced(value):
    with pytest.raises(FaultValidationError, match="between"):
        validate("flow_clamp", "CDU", {"max_pct": value}, set())


def test_non_numeric_parameter_is_rejected():
    with pytest.raises(FaultValidationError, match="must be a number"):
        validate("network_latency", "CRV", {"ms": "soon"}, set())


def test_sensor_point_must_exist_on_the_device():
    valid = {"flow_rate", "supply_fluid_temp"}
    assert validate("sensor_failure", "CDU", {"point": "flow_rate"}, valid)
    with pytest.raises(FaultValidationError, match="not a readable point"):
        validate("sensor_failure", "CDU", {"point": "remote_temp_3"}, valid)


def test_unknown_fault_kind_is_rejected():
    with pytest.raises(FaultValidationError, match="unknown fault"):
        validate("explode", "CDU", {}, set())


# ---- API enforcement ------------------------------------------------------

@pytest_asyncio.fixture
async def admin():
    sim = Simulator(serve_modbus=False)
    app = create_app(sim)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://t") as ac:
            ac.sim = sim
            yield ac


async def test_catalogue_endpoint_gives_the_ui_what_it_needs(admin):
    cat = (await admin.get("/admin/faults")).json()
    assert len(cat["groups"]) == 3
    assert len(cat["devices"]) == 30
    assert {f["kind"] for f in cat["catalogue"]} == set(CATALOGUE)
    for device_type in ("CRV", "CDU", "PDU"):
        assert len(cat["points"][device_type]) > 5


async def test_identity_points_are_not_offered_for_freezing(admin):
    cat = (await admin.get("/admin/faults")).json()
    for device_type in ("CRV", "CDU", "PDU"):
        assert "serial_number" not in cat["points"][device_type]
        assert "device_model" not in cat["points"][device_type]


@pytest.mark.parametrize("device_id,kind", [
    ("CRV-001", "pump_failure"), ("CRV-001", "flow_clamp"),
    ("CDU-001", "fan_failure"), ("CDU-001", "airflow_clamp"),
    ("RACK-A03", "pump_failure"),
])
async def test_api_rejects_faults_the_device_cannot_have(admin, device_id, kind):
    r = await admin.post("/admin/faults", json={"device_id": device_id, "kind": kind})
    assert r.status_code == 400
    assert "does not apply" in r.json()["detail"]
    assert admin.sim.faults == {}, "a rejected fault must not be registered"


@pytest.mark.parametrize("device_id,kind,params", [
    ("CDU-001", "pump_failure", {"pump": "A"}),
    ("CDU-002", "flow_clamp", {"max_pct": 30}),
    ("CRV-001", "fan_failure", {}),
    ("CRV-002", "airflow_clamp", {"max_pct": 35}),
    ("CRV-003", "sensor_failure", {"point": "remote_temp_3"}),
    ("RACK-A05", "device_offline", {}),
    ("RACK-A06", "network_latency", {"ms": 400}),
])
async def test_api_accepts_valid_combinations(admin, device_id, kind, params):
    r = await admin.post("/admin/faults",
                         json={"device_id": device_id, "kind": kind, "params": params})
    assert r.status_code == 200, r.json()
    assert r.json()["device_id"] == device_id


async def test_active_faults_report_their_layer(admin):
    await admin.post("/admin/faults", json={"device_id": "CDU-001", "kind": "pump_failure"})
    await admin.post("/admin/faults", json={"device_id": "CRV-001", "kind": "device_offline"})
    active = {a["device_id"]: a for a in (await admin.get("/admin/faults")).json()["active"]}
    assert active["CDU-001"]["layer"] == "physics"
    assert active["CRV-001"]["layer"] == "comms"


async def test_single_fault_can_be_cleared_without_clearing_the_rest(admin):
    await admin.post("/admin/faults", json={"device_id": "CDU-001", "kind": "pump_failure"})
    await admin.post("/admin/faults", json={"device_id": "CRV-001", "kind": "fan_failure"})
    await admin.delete("/admin/faults/CDU-001")
    active = (await admin.get("/admin/faults")).json()["active"]
    assert [a["device_id"] for a in active] == ["CRV-001"]


async def test_sensor_freeze_reaches_the_register_layer(admin):
    await admin.post("/admin/faults", json={
        "device_id": "CRV-001", "kind": "sensor_failure",
        "params": {"point": "remote_temp_3"}})
    assert admin.sim.registers["CRV-001"].frozen_points == {"remote_temp_3"}
