"""Exercise every control in the dashboard panel, exactly as the UI does.

Requires both services running (docker compose up, or sim.main + api.main).
Complements scripts/smoke.py: that one proves the pipeline end to end; this one
proves each individual control does what its label claims.

    python scripts/check_controls.py
"""

import json
import sys
import time
import urllib.error
import urllib.request

CTL, TEL = "http://127.0.0.1:8090", "http://127.0.0.1:8000"
results = []


def call(base, path, method="GET", payload=None):
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def snap():
    return call(TEL, "/api/snapshot")[1]


def state():
    return call(CTL, "/admin/state")[1]


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def settle(getter, abs_tol=0.05, n=5, gap=5, timeout=200):
    """Wait until a value stops moving.

    Uses an ABSOLUTE tolerance over a window of at least n*gap seconds. A
    relative tolerance is satisfied by small consecutive steps part-way up a
    slow ramp, which reports a mid-ramp value as if it were the final one.
    """
    hist, end = [], time.time() + timeout
    while time.time() < end:
        hist.append(getter())
        hist[:] = hist[-n:]
        if len(hist) == n and (max(hist) - min(hist)) <= abs_tol:
            return hist[-1]
        time.sleep(gap)
    return hist[-1]


def reset():
    call(CTL, "/admin/reset", "POST")
    time.sleep(2)


print("=== TIME SCALE ===")
for scale in (10, 30, 1):
    call(CTL, "/admin/clock", "POST", {"scale": scale})
    s0 = call(CTL, "/admin/health")[1]["steps"]
    t0 = time.time()
    time.sleep(6)
    s1 = call(CTL, "/admin/health")[1]["steps"]
    rate = (s1 - s0) / (time.time() - t0)
    check(
        f"clock {scale}x steps at ~{scale}/s",
        abs(rate - scale) < max(2, scale * 0.35),
        f"measured {rate:.1f} steps/s",
    )

call(CTL, "/admin/clock", "POST", {"scale": 20})  # speed up the rest of the run
reset()

print("\n=== LOAD: target options from the dropdown ===")
targets = [
    ("rows:A", {"rows": ["A"]}, 12),
    ("rows:B", {"rows": ["B"]}, 12),
    ("all", {}, 24),
    ("racks:RACK-A03", {"racks": ["RACK-A03"]}, 1),
    ("racks:RACK-A03,RACK-A04,RACK-A05", {"racks": ["RACK-A03", "RACK-A04", "RACK-A05"]}, 3),
]
for label, extra, expect in targets:
    reset()
    code, body = call(CTL, "/admin/load", "POST", {"cpu": 0.9, "gpu": 0.9, **extra})
    pinned = sum(1 for r in state()["racks"].values() if r["pinned"])
    check(
        f"target {label!r} pins {expect}",
        code == 200 and body.get("pinned") == expect and pinned == expect,
        f"api={body.get('pinned')} state={pinned}",
    )

print("\n=== LOAD: sliders actually change utilization ===")
reset()
call(CTL, "/admin/load", "POST", {"cpu": 0.25, "gpu": 0.35, "rows": ["A"]})
time.sleep(3)
r = state()["racks"]["RACK-A03"]
check(
    "cpu/gpu sliders reach the rack",
    abs(r["cpu"] - 0.25) < 0.02 and abs(r["gpu"] - 0.35) < 0.02,
    f"cpu={r['cpu']} gpu={r['gpu']}",
)
call(CTL, "/admin/load", "POST", {"cpu": 1.0, "gpu": 1.0, "rows": ["A"]})
time.sleep(3)
r = state()["racks"]["RACK-A03"]
check("sliders update an already-pinned rack", abs(r["cpu"] - 1.0) < 0.02, f"cpu={r['cpu']}")

print("\n=== LOAD: release returns racks to auto ===")
call(CTL, "/admin/load", "DELETE")
time.sleep(4)
pinned = sum(1 for x in state()["racks"].values() if x["pinned"])
r = state()["racks"]["RACK-A03"]
check("release unpins everything", pinned == 0, f"pinned={pinned}")
check("released rack drifts off 100%", r["cpu"] < 0.95, f"cpu={r['cpu']}")

print("\n=== SCENARIOS ===")
scenario_kw = {}
for label, cpu, gpu in [
    ("Idle", 0.10, 0.05),
    ("Normal", 0.45, 0.55),
    ("Heavy", 0.75, 0.88),
    ("Overload", 1.0, 1.0),
]:
    reset()
    call(CTL, "/admin/load", "POST", {"cpu": cpu, "gpu": gpu})
    kw = settle(lambda: snap()["summary"]["it_kw"], abs_tol=2.0)
    check(f"scenario {label}", kw > 0, f"IT {kw:.0f} kW")
    scenario_kw[label] = kw

order = ["Idle", "Normal", "Heavy", "Overload"]
check(
    "scenarios are monotonically hotter",
    all(scenario_kw[a] < scenario_kw[b] for a, b in zip(order, order[1:], strict=False)),
    " < ".join(f"{scenario_kw[k]:.0f}" for k in order) + " kW",
)

print("\n=== SCENARIO: hot spot A03-A05 ===")
reset()
call(
    CTL,
    "/admin/load",
    "POST",
    {"racks": ["RACK-A03", "RACK-A04", "RACK-A05"], "cpu": 1.0, "gpu": 1.0},
)
settle(lambda: snap()["racks"][2]["inlet_temp"], abs_tol=0.05)
racks = {r["id"]: r["inlet_temp"] for r in snap()["racks"]}
spot = racks["RACK-A04"]  # middle of the loaded group, the hottest point
neighbour = racks["RACK-A06"]
far = racks["RACK-A11"]
check(
    "hot spot is hotter than its neighbour",
    spot > neighbour + 0.3,
    f"spot {spot:.1f} vs A06 {neighbour:.1f}",
)
check(
    "neighbour is warmer than a distant rack",
    neighbour > far,
    f"A06 {neighbour:.1f} vs A11 {far:.1f}  (recirculation)",
)

print("\n=== FAULTS: catalogue is scoped by device type ===")
reset()
cat = call(CTL, "/admin/faults")[1]
check(
    "three device-scoped groups",
    len(cat["groups"]) == 3,
    ", ".join(g["title"] for g in cat["groups"]),
)
for dev, kind in [
    ("CRV-001", "pump_failure"),
    ("CDU-001", "fan_failure"),
    ("RACK-A03", "flow_clamp"),
]:
    code, _ = call(CTL, "/admin/faults", "POST", {"device_id": dev, "kind": kind})
    check(f"{kind} on {dev} refused", code == 400, f"HTTP {code}")

print("\n=== FAULTS: each kind on an appropriate device ===")
FAULTS = [
    ("CDU-001", "pump_failure", {"pump": "all"}, "cdu"),
    ("CDU-001", "flow_clamp", {"max_pct": 25}, "cdu"),
    ("CRV-001", "fan_failure", {}, "crv"),
    ("CRV-001", "airflow_clamp", {"max_pct": 30}, "crv"),
    ("CRV-001", "sensor_failure", {"point": "remote_temp_3"}, "sensor"),
    ("CRV-002", "network_latency", {"ms": 900}, "latency"),
    ("CRV-003", "device_offline", {}, "offline"),
]
for dev, kind, params, mode in FAULTS:
    reset()
    call(CTL, "/admin/load", "POST", {"cpu": 0.8, "gpu": 0.9})
    settle(lambda: snap()["summary"]["it_kw"], abs_tol=2.0)
    before = snap()
    code, _ = call(CTL, "/admin/faults", "POST", {"device_id": dev, "kind": kind, "params": params})
    if code != 200:
        check(f"{kind} on {dev}", False, f"HTTP {code}")
        continue
    time.sleep(32)
    after = snap()

    def dev_of(s, d):
        for k in ("cdus", "crvs"):
            for x in s[k]:
                if x["id"] == d:
                    return x
        return {}

    b, a = dev_of(before, dev), dev_of(after, dev)
    if mode == "cdu":
        ok = a.get("flow_rate", 1) < b.get("flow_rate", 0)
        detail = f"flow {b.get('flow_rate')} -> {a.get('flow_rate')} L/min"
    elif mode == "crv":
        ok = a.get("fan_speed", 1) < b.get("fan_speed", 0)
        detail = f"fan {b.get('fan_speed')} -> {a.get('fan_speed')} %"
    elif mode == "sensor":
        d2 = call(TEL, f"/api/devices/{dev}")[1]["values"]
        time.sleep(10)
        d3 = call(TEL, f"/api/devices/{dev}")[1]["values"]
        ok = (
            d2["remote_temp_3"] == d3["remote_temp_3"]
            and d2["remote_temp_4"] != d3["remote_temp_4"]
        )
        detail = f"frozen at {d2['remote_temp_3']}, neighbour still moving"
    elif mode == "latency":
        lat = call(TEL, f"/api/devices/{dev}")[1]["latency_ms"]
        ok = lat > 700
        detail = f"poll latency {lat} ms"
    else:
        ok = dev in after["summary"]["offline"]
        detail = f"offline list {after['summary']['offline']}"
    check(f"{kind} on {dev}", ok, detail)

print("\n=== FAULTS: N+1 pump failover ===")
reset()
call(CTL, "/admin/load", "POST", {"cpu": 0.8, "gpu": 0.9})
settle(lambda: snap()["summary"]["it_kw"], abs_tol=2.0)
call(
    CTL,
    "/admin/faults",
    "POST",
    {"device_id": "CDU-001", "kind": "pump_failure", "params": {"pump": "A"}},
)
time.sleep(25)
c = [x for x in snap()["cdus"] if x["id"] == "CDU-001"][0]
check(
    "single pump failure: standby takes over",
    c["flow_rate"] > 10,
    f"A={c['pump_a_status']} B={c['pump_b_status']} flow={c['flow_rate']}",
)
call(
    CTL,
    "/admin/faults",
    "POST",
    {"device_id": "CDU-001", "kind": "pump_failure", "params": {"pump": "all"}},
)
time.sleep(25)
c = [x for x in snap()["cdus"] if x["id"] == "CDU-001"][0]
check(
    "both pumps failed: flow collapses",
    c["flow_rate"] == 0,
    f"A={c['pump_a_status']} B={c['pump_b_status']} flow={c['flow_rate']}",
)

print("\n=== FAULTS: clear ===")
call(CTL, "/admin/faults", "DELETE")
time.sleep(20)
h = call(TEL, "/api/health")[1]
check("clear all faults restores the fleet", h["online"] == 30, f"{h['online']}/30 online")
check("no active faults remain", call(CTL, "/admin/faults")[1]["active"] == [])

print("\n=== ENVIRONMENT: humidity ===")
reset()
call(CTL, "/admin/load", "POST", {"cpu": 0.6, "gpu": 0.7})
call(CTL, "/admin/room", "POST", {"rh_pct": 40})
settle(lambda: snap()["cdus"][0]["supply_fluid_temp"], abs_tol=0.05)
dry = snap()["cdus"][0]
call(CTL, "/admin/room", "POST", {"rh_pct": 97})
settle(lambda: snap()["cdus"][0]["supply_fluid_temp"], abs_tol=0.05)
wet = snap()["cdus"][0]
check(
    "humidity raises the dew point",
    wet["dew_point"] > dry["dew_point"] + 3,
    f"{dry['dew_point']} -> {wet['dew_point']} C",
)
check(
    "dew point forces coolant supply up",
    wet["supply_fluid_temp"] > dry["supply_fluid_temp"] + 0.5,
    f"{dry['supply_fluid_temp']} -> {wet['supply_fluid_temp']} C",
)
check(
    "dew-point margin alarm raised",
    "DEWPOINT_MARGIN_LOW" in (wet.get("alarm_word") or []),
    str(wet.get("alarm_word")),
)
call(CTL, "/admin/room", "POST", {"rh_pct": None})
time.sleep(3)
check("humidity Auto accepted", call(CTL, "/admin/health")[1]["ok"])

print("\n=== RESET ===")
call(CTL, "/admin/load", "POST", {"cpu": 1.0, "gpu": 1.0})
call(CTL, "/admin/faults", "POST", {"device_id": "CDU-002", "kind": "pump_failure"})
call(CTL, "/admin/clock", "POST", {"scale": 30})
call(CTL, "/admin/reset", "POST")
time.sleep(3)
st, hl = state(), call(CTL, "/admin/health")[1]
check("reset unpins all racks", sum(1 for r in st["racks"].values() if r["pinned"]) == 0)
check("reset clears faults", call(CTL, "/admin/faults")[1]["active"] == [])
check("reset restores clock to 1x", hl["clock_scale"] == 1.0, f"scale={hl['clock_scale']}")

print("\n" + "=" * 60)
bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)}/{len(results)} checks passed")
if bad:
    print("FAILED:")
    [print("  -", n) for n in bad]
sys.exit(1 if bad else 0)
