"""Physics and control tests.

Every test here runs on VirtualClock time -- step() takes dt as an argument and
never reads a real clock -- so the suite completes in seconds rather than the
hours of simulated time it covers.
"""
import numpy as np
import pytest
from conftest import settle, uniform

from sim.engine.clock import ScaledClock, VirtualClock
from sim.engine.control import PID
from sim.engine.load import (
    AIR_THROTTLE,
    LIQUID_THROTTLE,
    PROFILES,
    derate_factor,
    node_power_w,
)
from sim.engine.psychro import dew_point_c, dewpoint_limited_supply, rh_from_dew_point
from sim.engine.state import Fault, StepInputs, build_state, step
from sim.engine.thermal import (
    CP_WATER,
    build_coupling_matrix,
    delta_t_from_heat,
    exhaust_step,
    lpm_to_kg_s,
)

# ---- conservation --------------------------------------------------------

def test_energy_is_conserved_at_steady_state(state):
    """Total heat rejected must equal total IT power. This is the test that
    caught the CRV double-counting recirculated heat."""
    settle(state, uniform(state, 0.25, 0.15), minutes=90)

    it_w = sum(r.power_w for r in state.racks.values())
    rejected_w = 1000.0 * (
        sum(c.cooling_kw for c in state.crvs.values())
        + sum(c.cooling_load_kw for c in state.cdus.values())
    )
    assert rejected_w == pytest.approx(it_w, rel=0.01)


def test_heat_split_accounts_for_all_rack_power(state):
    settle(state, uniform(state, 0.4, 0.4), minutes=30)
    for r in state.racks.values():
        assert r.q_air_w + r.q_liquid_w == pytest.approx(r.power_w, rel=1e-9)


def test_air_cooled_racks_send_no_heat_to_liquid(state):
    settle(state, uniform(state, 0.5, 0.5), minutes=20)
    for spec in state.topo.racks:
        if spec.liquid_capture_frac == 0.0:
            assert state.racks[spec.id].q_liquid_w == 0.0


# ---- dt independence (the history/live seam depends on this) -------------

def test_dt_independence_of_steady_state():
    """dt=1s (live mode) and dt=10s (Phase 2 history generation) must converge
    to the same trajectory, or the seam between generated history and live
    simulation becomes visible as a step in every chart."""
    fine, coarse = build_state(), build_state()
    load_f = uniform(fine, 0.5, 0.6)
    load_c = uniform(coarse, 0.5, 0.6)

    settle(fine, load_f, minutes=120, dt=1.0)
    settle(coarse, load_c, minutes=120, dt=10.0)

    for rid in fine.racks:
        assert fine.racks[rid].t_inlet == pytest.approx(
            coarse.racks[rid].t_inlet, abs=0.15
        ), rid
    for cid in fine.cdus:
        assert fine.cdus[cid].flow_lpm == pytest.approx(
            coarse.cdus[cid].flow_lpm, abs=3.0
        ), cid


def test_exhaust_integrator_is_unconditionally_stable():
    """Forward Euler would oscillate or diverge at large dt; the exponential
    form must not."""
    t = 25.0
    for _ in range(50):
        t = exhaust_step(t, 20.0, 30_000.0, 0.8, 90_000.0, dt=600.0)
    assert 20.0 < t < 80.0 and np.isfinite(t)


# ---- thermal mass produces lag ------------------------------------------

def test_temperature_lags_a_load_step(state):
    """Thermal mass means temperature must NOT track load instantly. The
    visible lag on the dashboard is the evidence that real physics is running."""
    settle(state, uniform(state, 0.1, 0.05), minutes=60)
    rack = state.racks["RACK-A03"]
    before = rack.t_exhaust

    hot = uniform(state, 0.9, 0.95)
    step(state, 1.0, StepInputs(load=hot))
    after_1s = rack.t_exhaust

    settle(state, hot, minutes=45)
    final = rack.t_exhaust

    assert final > before + 1.0, "load step must eventually raise temperature"
    moved = (after_1s - before) / (final - before)
    assert moved < 0.05, f"temperature jumped {moved:.0%} in one second"


# ---- coupling ------------------------------------------------------------

def test_loading_one_rack_warms_its_neighbours(state):
    """If racks were thermally independent, a hot spot would just be a large
    number typed into one box."""
    base = uniform(state, 0.1, 0.05)
    settle(state, base, minutes=60)
    baseline = state.racks["RACK-A04"].t_inlet

    spot = dict(base)
    spot["RACK-A03"] = (0.95, 0.98)
    spot["RACK-A05"] = (0.95, 0.98)
    settle(state, spot, minutes=60)

    assert state.racks["RACK-A04"].t_inlet > baseline + 0.3


def test_coupling_does_not_cross_the_aisle():
    topo = build_state().topo
    a_idx = topo.rack_index["RACK-A06"]
    b_idx = topo.rack_index["RACK-B01"]
    assert topo.coupling[a_idx, b_idx] == 0.0


def test_coupling_matrix_decays_with_distance():
    A = build_coupling_matrix(6, [0] * 6)
    assert A[2, 2] > A[2, 3] > A[2, 4] > A[2, 5]


def test_coupling_matrix_is_symmetric():
    A = build_coupling_matrix(8, [0] * 4 + [1] * 4)
    assert np.allclose(A, A.T)


# ---- throttling ----------------------------------------------------------

def test_derate_is_monotonic_in_temperature():
    temps = [20, 32, 34, 36, 38, 40, 44]
    d = [derate_factor(AIR_THROTTLE, t) for t in temps]
    assert all(d[i] >= d[i + 1] for i in range(len(d) - 1))
    assert d[0] == 1.0 and d[-1] == AIR_THROTTLE.min_derate


def test_liquid_policy_does_not_throttle_normal_coolant_temps():
    """A direct-to-chip loop at 38 degC return is healthy. Judging it against
    the 32 degC air-inlet threshold would throttle every liquid rack during
    normal operation."""
    assert derate_factor(LIQUID_THROTTLE, 38.0) == 1.0
    assert derate_factor(AIR_THROTTLE, 38.0) < 1.0


def test_normal_operation_does_not_throttle(state):
    settle(state, uniform(state, 0.6, 0.9), minutes=90)
    assert not any(r.throttled for r in state.racks.values())


def test_thermal_shutdown_collapses_power():
    from sim.engine.load import rack_load
    p = PROFILES["gpu"]
    hot = rack_load(p, 5, 0.95, 0.95, inlet_c=50.0)
    ok = rack_load(p, 5, 0.95, 0.95, inlet_c=22.0)
    assert hot.shutdown and hot.power_w < 0.2 * ok.power_w


def test_power_is_nonlinear_in_utilization():
    """An idle node still draws a large fraction of peak; that spread is why
    cooling control is interesting."""
    p = PROFILES["gpu"]
    idle, half, full = (node_power_w(p, u, u) for u in (0.0, 0.5, 1.0))
    assert idle > 0.1 * full
    assert half < 0.5 * (idle + full) + 1e-6


# ---- the pump-failure cascade -------------------------------------------

def test_pump_failure_cascades_and_self_limits(state):
    """Pump stops -> flow collapses -> liquid heat reverts to the air path ->
    inlet temperatures climb -> racks throttle -> heat falls -> the system
    settles at a degraded operating point instead of running away."""
    load = {r.id: (0.6, 0.9) if r.row == "A" else (0.5, 0.0) for r in state.topo.racks}
    settle(state, load, minutes=90)

    rack = state.racks["RACK-A03"]
    inlet_before, power_before = rack.t_inlet, rack.power_w
    assert not rack.throttled

    fault = {"CDU-001": Fault("pump_failure")}
    settle(state, load, minutes=30, faults=fault)

    assert state.cdus["CDU-001"].flow_lpm == 0.0
    assert rack.t_inlet > inlet_before + 5.0
    assert rack.throttled
    assert rack.power_w < power_before
    assert rack.t_inlet < 45.0, "throttling must keep this self-limiting"

    alarms = {a for _, a in state.active_alarms}
    assert {"PUMP_FAULT", "LOW_FLOW", "THERMAL_THROTTLING"} <= alarms


def test_racks_on_the_healthy_cdu_are_less_affected(state):
    load = {r.id: (0.6, 0.9) if r.row == "A" else (0.5, 0.0) for r in state.topo.racks}
    settle(state, load, minutes=90)
    settle(state, load, minutes=30, faults={"CDU-001": Fault("pump_failure")})
    assert state.racks["RACK-A09"].t_inlet < state.racks["RACK-A03"].t_inlet


# ---- dew point -----------------------------------------------------------

def test_dew_point_roundtrip():
    assert rh_from_dew_point(24.0, dew_point_c(24.0, 55.0)) == pytest.approx(55.0, abs=0.1)


def test_dew_point_rises_with_humidity():
    assert dew_point_c(24, 30) < dew_point_c(24, 55) < dew_point_c(24, 80)


def test_supply_is_clamped_above_dew_point():
    dew = dew_point_c(24.0, 70.0)          # ~18.2 degC
    assert dewpoint_limited_supply(15.0, dew, 2.0) == pytest.approx(dew + 2.0)
    assert dewpoint_limited_supply(28.0, dew, 2.0) == 28.0


def test_humidity_excursion_forces_supply_temperature_up(state):
    """A cooling failure caused by humidity, with no equipment fault anywhere.

    Raise room humidity -> dew point rises -> the CDU may not drive supply
    coolant below dew point + margin -> it loses cooling capacity -> racks warm.
    """
    load = uniform(state, 0.5, 0.6)
    settle(state, load, minutes=60, room_rh_pct=40.0)
    cdu = state.cdus["CDU-001"]
    supply_dry = cdu.supply_fluid_temp
    inlet_dry = state.racks["RACK-A03"].t_inlet

    settle(state, load, minutes=90, room_rh_pct=97.0)

    assert cdu.dew_point_c > 26.0, "excursion must actually raise the dew point"
    assert cdu.supply_fluid_temp > supply_dry + 0.5, "supply must be forced up"
    assert state.racks["RACK-A03"].t_inlet > inlet_dry
    assert "DEWPOINT_MARGIN_LOW" in cdu.alarms


def test_supply_never_violates_the_dew_point_margin(state):
    """Invariant: whatever the humidity, coolant supply stays above the dew
    point by the configured margin. Condensation on cold plates is not an
    acceptable operating state."""
    load = uniform(state, 0.4, 0.5)
    for rh in (35.0, 60.0, 80.0, 95.0):
        settle(state, load, minutes=45, room_rh_pct=rh)
        for spec in state.topo.cdus:
            cdu = state.cdus[spec.id]
            assert cdu.supply_fluid_temp >= cdu.dew_point_c + spec.dewpoint_margin_k - 0.05


# ---- controllers ---------------------------------------------------------

def test_pid_integral_is_dt_scaled():
    """Integral must accumulate error*dt, or history generated at dt=10s
    behaves differently from live simulation at dt=1s."""
    a, b = PID(kp=0, ki=1.0, setpoint=10.0, out_max=1e9), PID(kp=0, ki=1.0, setpoint=10.0, out_max=1e9)
    for _ in range(10):
        a.step(0.0, dt=1.0)
    b.step(0.0, dt=10.0)
    assert a.integral == pytest.approx(b.integral)


def test_pid_antiwindup_allows_recovery():
    pid = PID(kp=1.0, ki=0.5, setpoint=20.0, out_min=0.0, out_max=100.0, reverse=True)
    for _ in range(5000):
        pid.step(90.0, dt=1.0)          # sustained huge error
    out = [pid.step(20.0, dt=1.0) for _ in range(200)][-1]
    assert out < 100.0, "integral wound up and the controller cannot recover"


def test_pid_reverse_acting_raises_output_on_rising_temperature():
    pid = PID(kp=2.0, ki=0.1, setpoint=30.0, out_min=0.0, out_max=100.0, reverse=True)
    cool = pid.step(25.0, dt=1.0)
    pid.reset()
    hot = pid.step(38.0, dt=1.0)
    assert hot > cool


# ---- clocks --------------------------------------------------------------

def test_virtual_clock_never_touches_real_time():
    c = VirtualClock(start=1000.0)
    c.sleep(3600)
    assert c.now() == 4600.0


def test_scaled_clock_is_continuous_across_a_speed_change():
    c = ScaledClock(scale=1.0, start=0.0)
    before = c.now()
    c.set_scale(60.0)
    assert c.now() == pytest.approx(before, abs=0.05)


# ---- unit conversions ----------------------------------------------------

def test_flow_conversion():
    assert lpm_to_kg_s(60.0) == pytest.approx(0.997, abs=1e-3)


def test_heat_transfer_matches_the_source_document_example():
    """The source design doc cites ~180 L/min carrying a ~6 K coolant delta."""
    dt = delta_t_from_heat(70_000, lpm_to_kg_s(180), CP_WATER)
    assert dt == pytest.approx(6.0, abs=0.6)


def test_zero_flow_gives_infinite_delta():
    assert delta_t_from_heat(1000.0, 0.0, CP_WATER) == float("inf")


def test_pinned_racks_still_accept_their_override(state):
    """Regression: the control API marks a rack pinned AND supplies its load in
    the same call. A `not pinned` guard inside step() silently discarded every
    manual load change made from the UI."""
    state.racks["RACK-A03"].pinned = True
    settle(state, {"RACK-A03": (1.0, 1.0)}, minutes=10)
    assert state.racks["RACK-A03"].u_cpu_effective > 0.9
    assert state.racks["RACK-A03"].power_w > 20_000
