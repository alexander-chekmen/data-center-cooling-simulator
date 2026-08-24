"""Phase 1 physics demo -- runs without Modbus, HTTP or React.

    ./.venv/bin/python scripts/demo.py

Shows the two behaviors that matter: temperature lagging a load step on a
realistic time constant, and a pump failure cascading into throttling that
self-limits instead of running away.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim.engine.state import Fault, StepInputs, build_state, step

BAR = "-" * 92


def run(state, load, seconds, faults=None):
    inputs = StepInputs(load=load, faults=faults or {})
    for _ in range(seconds):
        step(state, 1.0, inputs)


def row(state, tag):
    r = state.racks["RACK-A03"]
    c = state.cdus["CDU-001"]
    v = state.crvs["CRV-001"]
    flag = "SHUTDOWN" if r.shutdown else ("THROTTLED" if r.throttled else "")
    print(f"{tag:>9} | A03 inlet {r.t_inlet:5.1f}C  exhaust {r.t_exhaust:5.1f}C  "
          f"{r.power_w/1000:5.1f}kW  derate {r.derate:4.2f} {flag:<10}"
          f"| CDU flow {c.flow_lpm:5.1f}L/min sup {c.supply_fluid_temp:4.1f} ret {c.return_fluid_temp:4.1f}"
          f" | CRV fan {v.fan_speed_pct:5.1f}%")


def main():
    s = build_state()
    idle = {r.id: (0.15, 0.08) for r in s.topo.racks}
    busy = {r.id: (0.60, 0.92) if r.row == "A" else (0.55, 0.0) for r in s.topo.racks}

    print("\n=== 1. warm up at low load ===")
    print(BAR)
    run(s, idle, 3600)
    row(s, "steady")
    print(f"{'':>9} | IT {s.it_load_kw:6.1f} kW   cooling {s.cooling_load_kw:6.1f} kW   PUE {s.pue:.2f}")

    print("\n=== 2. step row A to 92% GPU -- watch temperature LAG the load ===")
    print(BAR)
    for m in (0, 1, 2, 4, 8, 15, 30):
        run(s, busy, 60 if m else 1)
        row(s, f"t+{m}min" if m else "t+1s")
    print(f"{'':>9} | IT {s.it_load_kw:6.1f} kW   cooling {s.cooling_load_kw:6.1f} kW   PUE {s.pue:.2f}")

    print("\n=== 3. inject CDU-001 pump failure ===")
    print(BAR)
    fault = {"CDU-001": Fault("pump_failure")}
    prev = 0
    for m in (1, 2, 4, 8, 15, 25):
        run(s, busy, 60 * (m - prev), faults=fault)
        prev = m
        row(s, f"t+{m}min")
    print()
    print("  active alarms:", ", ".join(sorted({a for _, a in s.active_alarms})) or "none")
    print(f"  IT load fell to {s.it_load_kw:.0f} kW -- throttling is shedding heat, "
          f"which is what stops this running away.\n")


if __name__ == "__main__":
    main()
