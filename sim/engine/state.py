"""Simulation state and the single step() contract.

step() performs no I/O, opens no socket and never reads the wall clock. Time
arrives only as the `dt` argument. That is what makes Phase 2 fast-forward
history generation and fast deterministic tests possible from the same code.

Note on purity: step() advances state IN PLACE and returns it, for speed at
362,880 steps per history generation. It is deterministic and I/O-free, which
are the properties that matter; it is not a value-returning pure function.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from sim.engine import psychro
from sim.engine.control import PID
from sim.engine.load import (
    AIR_THROTTLE,
    LIQUID_THROTTLE,
    PROFILES,
    RackLoad,
    ThrottlePolicy,
    derate_factor,
    rack_load_at_derate,
)
from sim.engine.thermal import (
    CP_AIR,
    CP_WATER,
    delta_t_from_heat,
    exhaust_step,
    first_order_step,
    lpm_to_kg_s,
)
from sim.topology import Topology, default_topology

# Below this fraction of nominal flow the liquid loop is treated as stalled
# and its heat reverts to the air path.
FLOW_STALL_RATIO = 0.10


# --------------------------------------------------------------------------
# State containers
# --------------------------------------------------------------------------

@dataclass
class RackState:
    id: str
    u_cpu_requested: float = 0.05
    u_gpu_requested: float = 0.02
    u_cpu_effective: float = 0.05
    u_gpu_effective: float = 0.02
    pinned: bool = False
    power_w: float = 0.0
    q_air_w: float = 0.0
    q_liquid_w: float = 0.0
    t_inlet: float = 22.0
    t_exhaust: float = 32.0
    derate: float = 1.0
    thermal_stress_c: float = 22.0
    throttled: bool = False
    shutdown: bool = False
    energy_kwh: float = 0.0
    alarms: list[str] = field(default_factory=list)


@dataclass
class CRVState:
    id: str
    operating_state: str = "RUNNING"
    fan_speed_pct: float = 60.0
    airflow_kg_s: float = 0.0
    supply_air_temp: float = 18.0
    return_air_temp: float = 30.0
    capacity_pct: float = 0.0
    cooling_kw: float = 0.0
    filter_hours: float = 1200.0
    run_hours: float = 8600.0
    alarms: list[str] = field(default_factory=list)


@dataclass
class CDUState:
    id: str
    operating_state: str = "RUNNING"
    pump_speed_pct: float = 55.0
    flow_lpm: float = 0.0
    supply_fluid_temp: float = 28.0
    return_fluid_temp: float = 34.0
    supply_pressure_bar: float = 1.82
    return_pressure_bar: float = 1.10
    differential_pressure_bar: float = 0.72
    cooling_load_kw: float = 0.0
    dew_point_c: float = 11.0
    run_hours: float = 6100.0
    # N+1 pump redundancy. Run hours accrue per PUMP, not per device: that is
    # the whole reason a sub-assembly carries its own serial and service life.
    lead_pump: str = "A"
    failed_pumps: set = field(default_factory=set)
    pump_a_run_hours: float = 6100.0
    pump_b_run_hours: float = 2280.0
    alarms: list[str] = field(default_factory=list)

    def pump_status(self, which: str) -> str:
        if which in self.failed_pumps:
            return "FAULT"
        if which == self.lead_pump:
            return "RUNNING" if self.pump_speed_pct > 0.5 else "STOPPED"
        return "STANDBY"


@dataclass
class RoomState:
    ambient_c: float = 24.0
    rh_pct: float = 45.0
    dew_point_c: float = 11.3
    outdoor_c: float = 22.0


@dataclass
class Fault:
    """An injected equipment fault."""
    kind: str
    params: dict = field(default_factory=dict)


@dataclass
class StepInputs:
    """Everything external that can influence one step."""
    load: dict[str, tuple[float, float]] = field(default_factory=dict)
    faults: dict[str, Fault] = field(default_factory=dict)
    outdoor_c: float | None = None
    room_rh_pct: float | None = None


@dataclass
class SimState:
    t: float
    topo: Topology
    racks: dict[str, RackState]
    crvs: dict[str, CRVState]
    cdus: dict[str, CDUState]
    room: RoomState
    fan_pid: dict[str, PID]
    pump_pid: dict[str, PID]
    throttle: ThrottlePolicy = field(default_factory=ThrottlePolicy)

    @property
    def it_load_kw(self) -> float:
        return sum(r.power_w for r in self.racks.values()) / 1000.0

    @property
    def cooling_load_kw(self) -> float:
        return (sum(c.cooling_kw for c in self.crvs.values())
                + sum(c.cooling_load_kw for c in self.cdus.values()))

    @property
    def pue(self) -> float:
        it = self.it_load_kw
        if it <= 0.01:
            return 0.0
        # Cooling electrical draw approximated from delivered cooling via a
        # coefficient of performance, plus a fixed distribution-loss factor.
        cooling_electrical = self.cooling_load_kw / 3.4
        return (it + cooling_electrical + it * 0.06) / it

    @property
    def active_alarms(self) -> list[tuple[str, str]]:
        out = []
        for d in (*self.racks.values(), *self.crvs.values(), *self.cdus.values()):
            out.extend((d.id, a) for a in d.alarms)
        return out


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------

def build_state(topo: Topology | None = None, t0: float = 0.0) -> SimState:
    topo = topo or default_topology()

    racks = {r.id: RackState(id=r.id) for r in topo.racks}
    crvs = {c.id: CRVState(id=c.id, supply_air_temp=c.supply_air_setpoint_c)
            for c in topo.crvs}
    cdus = {c.id: CDUState(id=c.id, supply_fluid_temp=c.supply_fluid_setpoint_c)
            for c in topo.cdus}

    # Reverse-acting: rising temperature must raise fan/pump speed.
    fan_pid = {
        c.id: PID(kp=6.0, ki=0.35, setpoint=c.return_air_setpoint_c,
                  out_min=25.0, out_max=100.0, reverse=True)
        for c in topo.crvs
    }
    pump_pid = {
        c.id: PID(kp=7.0, ki=0.45, setpoint=c.return_temp_limit_c,
                  out_min=20.0, out_max=100.0, reverse=True)
        for c in topo.cdus
    }

    return SimState(
        t=t0, topo=topo, racks=racks, crvs=crvs, cdus=cdus,
        room=RoomState(), fan_pid=fan_pid, pump_pid=pump_pid,
    )


# --------------------------------------------------------------------------
# The step
# --------------------------------------------------------------------------

def step(state: SimState, dt: float, inputs: StepInputs | None = None) -> SimState:
    inputs = inputs or StepInputs()
    topo = state.topo
    hours = dt / 3600.0

    _step_room(state, dt, inputs)

    # 1. Requested load -> delivered load -> rack power -> heat split.
    #    Load uses the CURRENT inlet temperature, so throttling is a true
    #    feedback path: cooling affects compute.
    for spec in topo.racks:
        rs = state.racks[spec.id]

        # Load precedence (manual pin > scheduler > baseline) is resolved by
        # the caller before it reaches here. `rs.pinned` is a display flag only
        # -- gating on it here would reject the very override being pinned.
        if spec.id in inputs.load:
            rs.u_cpu_requested, rs.u_gpu_requested = inputs.load[spec.id]

        # Each cooling path is judged against its own policy, and the rack is
        # limited by whichever is in the most trouble. An air-cooled rack has
        # only the air path; a direct-to-chip rack throttles if EITHER its air
        # inlet or its coolant loop goes out of range.
        derate = derate_factor(AIR_THROTTLE, rs.t_inlet)
        shutdown = rs.t_inlet >= AIR_THROTTLE.critical_c
        stress_c = rs.t_inlet

        if spec.cdu_id:
            coolant_c = state.cdus[spec.cdu_id].return_fluid_temp
            derate = min(derate, derate_factor(LIQUID_THROTTLE, coolant_c))
            shutdown = shutdown or coolant_c >= LIQUID_THROTTLE.critical_c
            if derate_factor(LIQUID_THROTTLE, coolant_c) < derate_factor(AIR_THROTTLE, rs.t_inlet):
                stress_c = coolant_c

        result: RackLoad = rack_load_at_derate(
            profile=PROFILES[spec.profile],
            n_nodes=spec.n_nodes,
            u_cpu_requested=rs.u_cpu_requested,
            u_gpu_requested=rs.u_gpu_requested,
            derate=derate,
            shutdown=shutdown,
        )
        rs.thermal_stress_c = stress_c
        rs.power_w = result.power_w
        rs.u_cpu_effective = result.u_cpu_effective
        rs.u_gpu_effective = result.u_gpu_effective
        rs.derate = result.derate
        rs.throttled = result.throttled
        rs.shutdown = result.shutdown
        rs.energy_kwh += result.power_w / 1000.0 * hours

        # Heat only leaves through the liquid loop if the loop is actually
        # moving. When flow collapses, the direct-to-chip fraction reverts to
        # the air path and lands on the CRVs instead -- which is what makes a
        # pump failure cascade rather than silently disappear.
        liquid_frac = spec.liquid_capture_frac
        if spec.cdu_id:
            cdu_spec = _cdu_spec(topo, spec.cdu_id)
            flow_ratio = state.cdus[spec.cdu_id].flow_lpm / cdu_spec.nominal_flow_lpm
            if flow_ratio < FLOW_STALL_RATIO:
                liquid_frac *= max(0.0, flow_ratio / FLOW_STALL_RATIO)

        rs.q_liquid_w = result.power_w * liquid_frac
        rs.q_air_w = result.power_w - rs.q_liquid_w

    # 2. Liquid loop: CDUs reject the direct-to-chip fraction.
    for cdu in topo.cdus:
        _step_cdu(state, cdu, dt, inputs.faults.get(cdu.id))

    # 3. Air loop: CRVs reject residual air heat.
    for crv in topo.crvs:
        _step_crv(state, crv, dt, inputs.faults.get(crv.id))

    # 4. Rack exhaust temperature, with thermal mass.
    for spec in topo.racks:
        rs = state.racks[spec.id]
        crv = state.crvs[spec.crv_id]
        n_served = len(_crv_spec(topo, spec.crv_id).racks)
        rack_airflow = crv.airflow_kg_s / max(1, n_served)
        rs.t_exhaust = exhaust_step(
            t_exhaust=rs.t_exhaust,
            t_inlet=rs.t_inlet,
            q_air_w=rs.q_air_w,
            m_dot_air_kg_s=rack_airflow,
            c_rack_j_per_k=spec.c_rack_j_per_k,
            dt=dt,
        )

    # 5. Inlet temperatures via the recirculation matrix. This is what makes
    #    hot spots emergent rather than scripted.
    _step_inlets(state)

    # 6. Counters and alarms.
    for crv in state.crvs.values():
        crv.run_hours += hours
        crv.filter_hours += hours
    for cdu in state.cdus.values():
        cdu.run_hours += hours
        if cdu.pump_speed_pct > 0.5:
            if cdu.lead_pump == "A":
                cdu.pump_a_run_hours += hours
            else:
                cdu.pump_b_run_hours += hours
    _evaluate_alarms(state)

    state.t += dt
    return state


# --------------------------------------------------------------------------
# Sub-steps
# --------------------------------------------------------------------------

def _crv_spec(topo, crv_id):
    return next(c for c in topo.crvs if c.id == crv_id)


def _cdu_spec(topo, cdu_id):
    return next(c for c in topo.cdus if c.id == cdu_id)


def _step_room(state: SimState, dt: float, inputs: StepInputs) -> None:
    room = state.room
    if inputs.outdoor_c is not None:
        room.outdoor_c = inputs.outdoor_c
    if inputs.room_rh_pct is not None:
        room.rh_pct = inputs.room_rh_pct

    # Room dry-bulb drifts slowly toward the return-air average.
    if state.crvs:
        avg_return = sum(c.return_air_temp for c in state.crvs.values()) / len(state.crvs)
        room.ambient_c = first_order_step(room.ambient_c, avg_return - 4.0, 900.0, dt)

    room.dew_point_c = psychro.dew_point_c(room.ambient_c, room.rh_pct)


def _step_cdu(state: SimState, spec, dt: float, fault: Fault | None) -> None:
    cs = state.cdus[spec.id]
    q_w = sum(state.racks[r].q_liquid_w for r in spec.racks)
    cs.cooling_load_kw = q_w / 1000.0

    # Pump speed from the PI controller chasing the return-temperature limit.
    speed = state.pump_pid[spec.id].step(cs.return_fluid_temp, dt)

    # Which pumps are out. `pump_failure` with no params means the whole pump
    # set is lost; naming one pump exercises N+1 failover instead.
    failed = set()
    if fault is not None and fault.kind == "pump_failure":
        which = str(fault.params.get("pump", "all")).upper()
        failed = {"A", "B"} if which == "ALL" else {which}
    cs.failed_pumps = failed

    # Standby takes over if the lead is the one that died.
    if cs.lead_pump in failed:
        standby = "B" if cs.lead_pump == "A" else "A"
        if standby not in failed:
            cs.lead_pump = standby

    if cs.lead_pump in failed:
        speed = 0.0
    elif fault is not None and fault.kind == "flow_clamp":
        speed = min(speed, float(fault.params.get("max_pct", 40.0)))

    cs.pump_speed_pct = speed
    cs.operating_state = "FAULT" if speed <= 0.5 else "RUNNING"
    cs.flow_lpm = spec.nominal_flow_lpm * (speed / 100.0)

    # Supply temperature: the setpoint, unless the dew point forbids it.
    cs.dew_point_c = state.room.dew_point_c
    target_supply = psychro.dewpoint_limited_supply(
        spec.supply_fluid_setpoint_c, cs.dew_point_c, spec.dewpoint_margin_k
    )
    # Capacity limit: beyond max_cooling_kw the unit cannot hold the setpoint.
    if cs.cooling_load_kw > spec.max_cooling_kw:
        target_supply += (cs.cooling_load_kw - spec.max_cooling_kw) * 0.05
    cs.supply_fluid_temp = first_order_step(cs.supply_fluid_temp, target_supply, 120.0, dt)

    m_dot = lpm_to_kg_s(cs.flow_lpm)
    if m_dot <= 1e-6:
        # No flow: coolant in the loop absorbs heat with nowhere to go.
        loop_mass_kg = 180.0
        rise = (q_w / (loop_mass_kg * CP_WATER)) * dt
        cs.return_fluid_temp = min(cs.return_fluid_temp + rise, 95.0)
    else:
        target_return = cs.supply_fluid_temp + delta_t_from_heat(q_w, m_dot, CP_WATER)
        cs.return_fluid_temp = first_order_step(
            cs.return_fluid_temp, min(target_return, 95.0), 45.0, dt
        )

    frac = speed / 100.0
    cs.supply_pressure_bar = 0.35 + 2.10 * frac ** 2
    cs.return_pressure_bar = 0.30 + 0.95 * frac ** 2
    cs.differential_pressure_bar = cs.supply_pressure_bar - cs.return_pressure_bar


def _step_crv(state: SimState, spec, dt: float, fault: Fault | None) -> None:
    """Advance one in-row cooling unit.

    Energy bookkeeping note: return air temperature is derived from the energy
    balance (supply + q/(m_dot*cp)) rather than from mean rack exhaust. Using
    mean exhaust double-counts recirculated heat, because recirculation raises
    rack inlet above CRV supply while the mass flow through both is the same --
    the unit then appears to reject more heat than the racks generate. The
    return sensor physically sees a mixed return stream whose enthalpy equals
    the heat the coil must reject, which is exactly what this computes.
    """
    cs = state.crvs[spec.id]
    q_w = sum(state.racks[r].q_air_w for r in spec.racks)

    speed = state.fan_pid[spec.id].step(cs.return_air_temp, dt)
    if fault is not None:
        if fault.kind == "fan_failure":
            speed = 0.0
        elif fault.kind == "airflow_clamp":
            speed = min(speed, float(fault.params.get("max_pct", 40.0)))

    cs.fan_speed_pct = speed
    cs.operating_state = "FAULT" if speed <= 0.5 else "RUNNING"
    cs.airflow_kg_s = spec.nominal_airflow_kg_s * (speed / 100.0)

    m_dot = cs.airflow_kg_s
    if m_dot <= 1e-6:
        # No airflow: the coil rejects nothing and air stagnates toward exhaust.
        cs.capacity_pct = 0.0
        cs.cooling_kw = 0.0
        mean_exhaust = sum(state.racks[r].t_exhaust for r in spec.racks) / len(spec.racks)
        cs.return_air_temp = first_order_step(cs.return_air_temp, mean_exhaust, 60.0, dt)
        cs.supply_air_temp = first_order_step(cs.supply_air_temp, cs.return_air_temp, 60.0, dt)
        return

    capacity_w = spec.max_cooling_kw * 1000.0
    delivered_w = min(q_w, capacity_w)
    cs.cooling_kw = delivered_w / 1000.0
    cs.capacity_pct = 100.0 * delivered_w / capacity_w

    # Supply air holds the setpoint unless the coil runs out of capacity, in
    # which case the shortfall pushes supply air upward.
    shortfall_w = max(0.0, q_w - capacity_w)
    target_supply = spec.supply_air_setpoint_c + shortfall_w / (m_dot * CP_AIR)
    cs.supply_air_temp = first_order_step(cs.supply_air_temp, target_supply, 25.0, dt)

    # Energy-exact: delivered_w == m_dot * cp * (return - supply) by construction.
    target_return = cs.supply_air_temp + delivered_w / (m_dot * CP_AIR)
    cs.return_air_temp = first_order_step(cs.return_air_temp, target_return, 30.0, dt)


def _step_inlets(state: SimState) -> None:
    topo = state.topo
    exhausts = np.array([state.racks[r.id].t_exhaust for r in topo.racks])
    # Each rack's cold-aisle supply comes from the CRV serving it.
    supplies = np.array([state.crvs[r.crv_id].supply_air_temp for r in topo.racks])

    deltas = topo.coupling @ (exhausts - supplies)
    for i, spec in enumerate(topo.racks):
        state.racks[spec.id].t_inlet = float(supplies[i] + deltas[i])


def _evaluate_alarms(state: SimState) -> None:
    for rs in state.racks.values():
        a = []
        if rs.t_inlet > 32.0:
            a.append("HIGH_INLET_TEMP")
        if rs.throttled:
            a.append("THERMAL_THROTTLING")
        if rs.shutdown:
            a.append("THERMAL_SHUTDOWN")
        rs.alarms = a

    for cs in state.crvs.values():
        a = []
        if cs.return_air_temp > 40.0:
            a.append("HIGH_RETURN_TEMP")
        if cs.supply_air_temp > 24.0:
            a.append("HIGH_SUPPLY_TEMP")
        if cs.airflow_kg_s < 0.5:
            a.append("LOW_AIRFLOW")
        if cs.filter_hours > 4000.0:
            a.append("FILTER_SERVICE_DUE")
        cs.alarms = a

    for cs in state.cdus.values():
        spec = _cdu_spec(state.topo, cs.id)
        a = []
        if cs.supply_fluid_temp > 34.0:
            a.append("HIGH_SUPPLY_TEMP")
        # Insufficient flow is a relationship between flow and heat load, not
        # an absolute number: at low load the pump idles at its minimum speed
        # and that is entirely healthy.
        if cs.pump_speed_pct <= 0.5 or cs.return_fluid_temp > spec.return_temp_limit_c + 3.0:
            a.append("LOW_FLOW")
        if cs.supply_fluid_temp - cs.dew_point_c < spec.dewpoint_margin_k + 0.3:
            a.append("DEWPOINT_MARGIN_LOW")
        if cs.pump_speed_pct <= 0.5:
            a.append("PUMP_FAULT")
        cs.alarms = a
