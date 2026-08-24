"""Project live SimState onto the telemetry points each device exposes.

This is the only place that knows how simulation state becomes device-visible
values. Everything downstream (register encoding, Modbus framing, the
collector's decoder) is driven by the register maps.
"""
from __future__ import annotations

from sim.engine.state import SimState
from sim.identity import (
    component_serial,
    device_identity,
    rack_asset_tag,
)

NOMINAL_VOLTAGE = 400.0
NOMINAL_FREQUENCY = 50.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _bits(alarms: list[str], allowed: dict[int, str]) -> list[str]:
    names = set(allowed.values())
    return [a for a in alarms if a in names]


def _identity_points(device_id: str, device_type: str) -> dict:
    ident = device_identity(device_id, device_type)
    return {
        "device_model": ident.model,
        "serial_number": ident.serial,
        "firmware_version": ident.firmware,
        "hardware_revision": float(ident.hardware_revision),
    }


def crv_values(state: SimState, crv_id: str) -> dict:
    cs = state.crvs[crv_id]
    spec = next(c for c in state.topo.crvs if c.id == crv_id)
    served = spec.racks

    # The remote temperature sensors are the rack inlet probes -- exactly what
    # they are on real in-row equipment.
    remote = {}
    for i in range(1, 6):
        rack_id = served[i - 1] if i - 1 < len(served) else served[-1]
        remote[f"remote_temp_{i}"] = _clamp(state.racks[rack_id].t_inlet, -20.0, 80.0)

    load_frac = cs.capacity_pct / 100.0
    suction_t = _clamp(cs.supply_air_temp - 6.0, -40.0, 100.0)
    discharge_t = _clamp(35.0 + 45.0 * load_frac, -20.0, 150.0)

    return {
        "operating_state": cs.operating_state,
        "teamwork_status": "TEAMWORK",
        "fan_control_mode": "AUTO",
        "compressor_control_mode": "AUTO" if load_frac > 0.02 else "OFF",
        "phase_a_voltage": _clamp(NOMINAL_VOLTAGE - 1.8, 0.0, 600.0),
        "phase_b_voltage": _clamp(NOMINAL_VOLTAGE + 0.9, 0.0, 600.0),
        "phase_c_voltage": _clamp(NOMINAL_VOLTAGE - 0.4, 0.0, 600.0),
        "frequency": NOMINAL_FREQUENCY,
        "return_air_temp": _clamp(cs.return_air_temp, -20.0, 80.0),
        "return_humidity": _clamp(state.room.rh_pct, 0.0, 100.0),
        "supply_air_temp": _clamp(cs.supply_air_temp, -20.0, 80.0),
        "supply_air_temp_2": _clamp(cs.supply_air_temp + 0.3, -20.0, 80.0),
        **remote,
        "fan_speed": _clamp(cs.fan_speed_pct, 0.0, 100.0),
        "cooling_capacity": _clamp(cs.capacity_pct, 0.0, 100.0),
        "cooling_output": _clamp(cs.cooling_kw, 0.0, 500.0),
        "comp_high_pressure": _clamp(8.0 + 14.0 * load_frac, 0.0, 50.0),
        "comp_low_pressure": _clamp(3.2 + 1.6 * load_frac, 0.0, 50.0),
        "comp_discharge_temp": discharge_t,
        "comp_suction_temp": suction_t,
        "comp_discharge_superheat": _clamp(discharge_t - 42.0, -20.0, 80.0),
        "comp_suction_superheat": _clamp(6.0 + 2.0 * load_frac, -20.0, 80.0),
        "filter_hours": _clamp(cs.filter_hours, 0.0, 200_000.0),
        "run_hours": _clamp(cs.run_hours, 0.0, 200_000.0),
        **_identity_points(crv_id, "CRV"),
        # Units in a row operate as a team; the lowest-numbered unit leads.
        "teamwork_group": float(1 if served[0].startswith("RACK-A") else 2),
        "teamwork_role": "LEAD" if crv_id in ("CRV-001", "CRV-003") else "MEMBER",
        "alarm_word": _bits(cs.alarms, {
            0: "HIGH_RETURN_TEMP", 1: "HIGH_SUPPLY_TEMP", 2: "LOW_AIRFLOW",
            3: "FILTER_SERVICE_DUE", 4: "COMPRESSOR_FAULT", 5: "SENSOR_FAULT",
        }),
    }


def cdu_values(state: SimState, cdu_id: str) -> dict:
    cs = state.cdus[cdu_id]
    return {
        "operating_state": cs.operating_state,
        "pump_control_mode": "AUTO_TEMP",
        "supply_fluid_temp": _clamp(cs.supply_fluid_temp, -10.0, 70.0),
        "return_fluid_temp": _clamp(cs.return_fluid_temp, -10.0, 70.0),
        "supply_pressure": _clamp(cs.supply_pressure_bar, 0.0, 10.0),
        "return_pressure": _clamp(cs.return_pressure_bar, 0.0, 10.0),
        "flow_rate": _clamp(cs.flow_lpm, 0.0, 600.0),
        "differential_pressure": _clamp(cs.differential_pressure_bar, 0.0, 10.0),
        "dew_point": _clamp(cs.dew_point_c, -20.0, 40.0),
        "pump_speed": _clamp(cs.pump_speed_pct, 0.0, 100.0),
        "cooling_load": _clamp(cs.cooling_load_kw, 0.0, 2000.0),
        "pump_suction_pressure": _clamp(cs.return_pressure_bar * 0.92, 0.0, 10.0),
        "pump_discharge_pressure": _clamp(cs.supply_pressure_bar * 1.04, 0.0, 10.0),
        "refrigerant_liquid_temp": _clamp(cs.supply_fluid_temp - 3.5, -20.0, 80.0),
        "run_hours": _clamp(cs.run_hours, 0.0, 200_000.0),
        **_identity_points(cdu_id, "CDU"),
        "pump_a_serial": component_serial(cdu_id, "pump_a"),
        "pump_a_status": cs.pump_status("A"),
        "pump_a_run_hours": _clamp(cs.pump_a_run_hours, 0.0, 200_000.0),
        "pump_b_serial": component_serial(cdu_id, "pump_b"),
        "pump_b_status": cs.pump_status("B"),
        "pump_b_run_hours": _clamp(cs.pump_b_run_hours, 0.0, 200_000.0),
        "alarm_word": _bits(cs.alarms, {
            0: "HIGH_SUPPLY_TEMP", 1: "LOW_FLOW", 2: "HIGH_DIFF_PRESSURE",
            3: "DEWPOINT_MARGIN_LOW", 4: "PUMP_FAULT", 5: "SENSOR_FAULT",
        }),
    }


def cdu_setpoints(state: SimState, cdu_id: str) -> dict:
    spec = next(c for c in state.topo.cdus if c.id == cdu_id)
    return {
        "supply_fluid_temp_sp": spec.supply_fluid_setpoint_c,
        "return_temp_limit": spec.return_temp_limit_c,
        "pump_flow_sp": spec.nominal_flow_lpm,
        "differential_pressure_sp": 0.75,
        "supply_pressure_limit": 4.0,
        "manual_pump_speed": 0.0,
        "dewpoint_margin": spec.dewpoint_margin_k,
    }


def pdu_values(state: SimState, rack_id: str) -> dict:
    rs = state.racks[rack_id]
    kw = rs.power_w / 1000.0
    # Three-phase current, mildly unbalanced so the phases are not identical.
    amps = kw * 1000.0 / (3.0 * (NOMINAL_VOLTAGE / 1.732))
    alarms = list(rs.alarms)
    if amps > 380.0:
        alarms.append("OVERCURRENT")

    return {
        "operating_state": "FAULT" if rs.shutdown else "ONLINE",
        "total_power": _clamp(kw, 0.0, 200.0),
        "phase_a_current": _clamp(amps * 1.02, 0.0, 400.0),
        "phase_b_current": _clamp(amps * 0.98, 0.0, 400.0),
        "phase_c_current": _clamp(amps * 1.00, 0.0, 400.0),
        "cpu_utilization": _clamp(rs.u_cpu_effective * 100.0, 0.0, 100.0),
        "gpu_utilization": _clamp(rs.u_gpu_effective * 100.0, 0.0, 100.0),
        "inlet_temp": _clamp(rs.t_inlet, -20.0, 80.0),
        "outlet_temp": _clamp(rs.t_exhaust, -20.0, 80.0),
        "energy_total": _clamp(rs.energy_kwh, 0.0, 100_000_000.0),
        **_identity_points(rack_id, "PDU"),
        "rack_asset_tag": rack_asset_tag(rack_id),
        "alarm_word": _bits(alarms, {
            0: "HIGH_INLET_TEMP", 1: "THERMAL_THROTTLING",
            2: "THERMAL_SHUTDOWN", 3: "OVERCURRENT",
        }),
    }
