"""End-to-end smoke test against running services.

Assumes `python -m sim.main` and `python -m api.main` are already up. Drives the
simulator through its control API and asserts the consequences arrive back
through the real pipeline -- physics, Modbus, collector, telemetry API.

    python scripts/smoke.py

Exits non-zero on the first failed expectation.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

CONTROL = "http://127.0.0.1:8090"
TELEMETRY = "http://127.0.0.1:8000"

failures: list[str] = []


def call(url: str, method: str = "GET", payload=None, timeout=10):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_for(url: str, predicate, what: str, timeout=90, fatal=True):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = call(url)
            if predicate(last):
                print(f"  ready: {what}")
                return last
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(2)
    if fatal:
        sys.exit(f"TIMEOUT waiting for {what}; last response: {last}")
    print(f"  TIMEOUT: {what}")
    return last


def wait_stable(getter, what: str, tol=0.015, samples=3, interval=6, timeout=240):
    """Wait until a measurement stops changing, then return it.

    A data center has thermal time constants of minutes. Sampling the instant a
    value crosses a threshold captures it MID-RAMP, so a later reading of the
    same quantity can legitimately be larger and any comparison against it is
    meaningless. Waiting for the physics to settle is the only sound way to take
    a reference measurement here.
    """
    deadline = time.time() + timeout
    history: list[float] = []
    while time.time() < deadline:
        history.append(getter())
        if len(history) > samples:
            history.pop(0)
        if len(history) == samples:
            lo, hi = min(history), max(history)
            if hi <= 0 or (hi - lo) / hi <= tol:
                print(f"  settled: {what} at {history[-1]:.1f}")
                return history[-1]
        time.sleep(interval)
    print(f"  NOT SETTLED: {what}, last {history[-1]:.1f}")
    return history[-1]


def require(name: str, ok: bool, detail: str = ""):
    """A precondition. If this fails the later assertions are meaningless, so
    stop rather than report a confusing downstream failure."""
    if not ok:
        sys.exit(f"PRECONDITION FAILED: {name}{'  -- ' + detail if detail else ''}")
    print(f"  ok: {name}")


def active_faults():
    return {(f["device_id"], f["kind"]) for f in call(f"{CONTROL}/admin/faults")["active"]}


def pinned_count():
    return sum(1 for r in call(f"{CONTROL}/admin/state")["racks"].values() if r["pinned"])


def check(name: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  -- ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


def summary():
    return call(f"{TELEMETRY}/api/snapshot")["summary"]


def snapshot():
    return call(f"{TELEMETRY}/api/snapshot")


def main() -> int:
    print("waiting for services")
    wait_for(f"{CONTROL}/admin/health", lambda h: h.get("ok"), "control API :8090")
    wait_for(f"{TELEMETRY}/api/health",
             lambda h: h.get("devices") == 30 and h.get("online") == 30,
             "all 30 devices polled and online")

    print("\nbaseline")
    call(f"{CONTROL}/admin/reset", "POST")
    require("reset cleared all faults", active_faults() == set())
    require("reset released all pinned racks", pinned_count() == 0)
    wait_stable(lambda: summary()["it_kw"], "baseline IT load")
    base = summary()
    print(f"  IT {base['it_kw']} kW  cooling {base['cooling_kw']} kW  PUE {base['pue']}")
    check("baseline IT load is non-zero", base["it_kw"] > 50, f"{base['it_kw']} kW")
    check("energy roughly balances at steady state",
          abs(base["cooling_kw"] - base["it_kw"]) < base["it_kw"] * 0.12,
          f"IT {base['it_kw']} vs cooling {base['cooling_kw']}")

    print("\noverload all racks via the control API")
    call(f"{CONTROL}/admin/load", "POST", {"cpu": 1.0, "gpu": 1.0})
    require("all 24 racks are pinned", pinned_count() == 24, f"{pinned_count()} pinned")
    wait_stable(lambda: summary()["it_kw"], "overloaded IT load")
    hot = summary()
    print(f"  IT {hot['it_kw']} kW  cooling {hot['cooling_kw']} kW  PUE {hot['pue']}")
    check("overload raises IT load", hot["it_kw"] > base["it_kw"] * 1.4,
          f"{base['it_kw']} -> {hot['it_kw']} kW")
    check("cooling responds to the extra heat",
          hot["cooling_kw"] > base["cooling_kw"] * 1.3,
          f"{base['cooling_kw']} -> {hot['cooling_kw']} kW")

    print("\ninject CDU-001 pump failure")
    call(f"{CONTROL}/admin/faults", "POST",
         {"device_id": "CDU-001", "kind": "pump_failure"})
    require("pump fault is registered", ("CDU-001", "pump_failure") in active_faults())
    require("racks are still pinned", pinned_count() == 24, f"{pinned_count()} pinned")

    served = {f"RACK-A{i:02d}" for i in range(1, 7)}

    def stressed(s):
        return {r["id"] for r in s["racks"]
                if (r.get("alarm_word") or []) and
                   ({"THERMAL_THROTTLING", "THERMAL_SHUTDOWN"} & set(r["alarm_word"]))}

    wait_for(f"{TELEMETRY}/api/snapshot", lambda s: stressed(s) >= served,
             "racks on the failed CDU begin throttling", timeout=120, fatal=False)
    wait_stable(lambda: summary()["it_kw"], "post-fault IT load")
    snap = snapshot()
    cdu = next(c for c in snap["cdus"] if c["id"] == "CDU-001")
    codes = {a["code"] for a in snap["alarms"]}
    print(f"  CDU-001 flow {cdu['flow_rate']} L/min   stressed: {sorted(stressed(snap))}")

    check("pump fault stops coolant flow", cdu["flow_rate"] == 0, f"{cdu['flow_rate']} L/min")
    check("PUMP_FAULT alarm is observed over Modbus", "PUMP_FAULT" in codes, str(sorted(codes)))
    check("racks served by the failed CDU are thermally stressed",
          stressed(snap) >= served, f"got {sorted(stressed(snap))}")
    check("racks on the healthy CDU are unaffected",
          not (stressed(snap) & {f"RACK-A{i:02d}" for i in range(7, 13)}))
    check("throttling sheds load rather than running away",
          snap["summary"]["it_kw"] < hot["it_kw"],
          f"{hot['it_kw']} -> {snap['summary']['it_kw']} kW")

    print("\ncomms fault: take CRV-002 offline")
    call(f"{CONTROL}/admin/faults", "POST",
         {"device_id": "CRV-002", "kind": "device_offline"})
    require("offline fault is registered", ("CRV-002", "device_offline") in active_faults())
    h = wait_for(f"{TELEMETRY}/api/health", lambda h: h["online"] < 30,
                 "collector marks the device offline", timeout=90, fatal=False)
    check("collector detects the offline device", h["online"] < 30, f"{h['online']}/30 online")

    print("\nreset")
    call(f"{CONTROL}/admin/reset", "POST")
    h = wait_for(f"{TELEMETRY}/api/health", lambda h: h["online"] == 30,
                 "fleet recovers", timeout=90, fatal=False)
    check("fleet recovers after faults clear", h["online"] == 30, f"{h['online']}/30")

    print()
    if failures:
        print(f"SMOKE FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
