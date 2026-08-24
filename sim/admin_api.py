"""Simulator control API -- the test fixture, NOT part of the monitored system.

The collector never touches this. It exists so a human (or a test) can drive the
simulated data center the way a load bank and a fault rig would drive real
hardware on a bench, entirely out of band from the telemetry path.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from sim.faults import CATALOGUE, GROUPS, FaultValidationError, validate
from sim.simulator import LoadOverride, Simulator

KNOWN_FAULTS = sorted(CATALOGUE)


class LoadRequest(BaseModel):
    racks: list[str] | None = None
    rows: list[str] | None = None
    cpu: float = Field(ge=0.0, le=1.0)
    gpu: float = Field(default=0.0, ge=0.0, le=1.0)


class FaultRequest(BaseModel):
    device_id: str
    kind: str
    params: dict = Field(default_factory=dict)


class RoomRequest(BaseModel):
    rh_pct: float | None = Field(default=None, ge=1.0, le=100.0)


class ClockRequest(BaseModel):
    scale: float = Field(gt=0.0, le=120.0)


def create_app(sim: Simulator | None = None) -> FastAPI:
    simulator = sim or Simulator()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await simulator.start()
        yield
        await simulator.stop()

    app = FastAPI(title="ThermalEdge Simulator Control", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    app.state.sim = simulator

    @app.get("/admin/health")
    def health():
        return {
            "ok": True,
            "sim_time": simulator.state.t,
            "steps": simulator.steps,
            "clock_scale": simulator.clock.scale,
            "listeners": [s.port for s in simulator.servers],
        }

    @app.get("/admin/topology")
    def topology():
        t = simulator.state.topo
        return {
            "site": t.site,
            "racks": [
                {"id": r.id, "row": r.row, "position": r.position,
                 "profile": r.profile, "nodes": r.n_nodes,
                 "liquid": r.liquid_capture_frac, "crv": r.crv_id, "cdu": r.cdu_id}
                for r in t.racks
            ],
            "crvs": [{"id": c.id, "racks": c.racks} for c in t.crvs],
            "cdus": [{"id": c.id, "racks": c.racks} for c in t.cdus],
            "endpoints": [
                {"id": e.device_id, "type": e.device_type,
                 "port": e.port, "unit_id": e.unit_id}
                for e in simulator.endpoints
            ],
        }

    @app.get("/admin/state")
    def state():
        s = simulator.state
        return {
            "t": s.t,
            "it_load_kw": round(s.it_load_kw, 2),
            "cooling_load_kw": round(s.cooling_load_kw, 2),
            "pue": round(s.pue, 3),
            "room": {"ambient_c": round(s.room.ambient_c, 2),
                     "rh_pct": round(s.room.rh_pct, 1),
                     "dew_point_c": round(s.room.dew_point_c, 2)},
            "racks": {r.id: {"inlet": round(r.t_inlet, 2),
                             "exhaust": round(r.t_exhaust, 2),
                             "kw": round(r.power_w / 1000, 2),
                             "cpu": round(r.u_cpu_effective, 3),
                             "gpu": round(r.u_gpu_effective, 3),
                             "derate": round(r.derate, 3),
                             "throttled": r.throttled,
                             "shutdown": r.shutdown,
                             "pinned": r.id in simulator.overrides}
                      for r in s.racks.values()},
            "alarms": [{"device": d, "code": c} for d, c in s.active_alarms],
        }

    # -- load control ----------------------------------------------------

    @app.post("/admin/load")
    def set_load(req: LoadRequest):
        targets: list[str] = []
        if req.racks:
            targets += req.racks
        if req.rows:
            targets += [r.id for r in simulator.state.topo.racks if r.row in req.rows]
        if not targets:
            targets = [r.id for r in simulator.state.topo.racks]

        unknown = [r for r in targets if r not in simulator.state.racks]
        if unknown:
            raise HTTPException(404, f"unknown racks: {unknown[:5]}")

        for rack_id in targets:
            simulator.overrides[rack_id] = LoadOverride(req.cpu, req.gpu)
            simulator.state.racks[rack_id].pinned = True
        return {"pinned": len(targets), "cpu": req.cpu, "gpu": req.gpu}

    @app.delete("/admin/load")
    def clear_load():
        for rack_id in list(simulator.overrides):
            simulator.state.racks[rack_id].pinned = False
        n = len(simulator.overrides)
        simulator.overrides.clear()
        return {"released": n}

    # -- faults ----------------------------------------------------------

    def _device_type(device_id: str) -> str | None:
        for ep in simulator.endpoints:
            if ep.device_id == device_id:
                return ep.device_type
        return None

    def _freezable_points(device_type: str) -> list[str]:
        """Points a sensor-freeze fault may target: real measurements, not
        identity strings."""
        for ep in simulator.endpoints:
            if ep.device_type == device_type:
                regmap = simulator.registers[ep.device_id].regmap
                return sorted(p.key for p in regmap.points if p.kind != "identity")
        return []

    @app.get("/admin/faults")
    def list_faults():
        """The catalogue the UI builds its sections from, plus what is active."""
        return {
            "known": KNOWN_FAULTS,
            "groups": GROUPS,
            "catalogue": [f.as_dict() for f in CATALOGUE.values()],
            "devices": [{"id": e.device_id, "type": e.device_type}
                        for e in simulator.endpoints],
            "points": {t: _freezable_points(t) for t in ("CRV", "CDU", "PDU")},
            "active": [{"device_id": d, "kind": f.kind, "params": f.params,
                        "device_type": _device_type(d),
                        "layer": CATALOGUE[f.kind].layer if f.kind in CATALOGUE else "?"}
                       for d, f in simulator.faults.items()],
        }

    @app.post("/admin/faults")
    def set_fault(req: FaultRequest):
        device_type = _device_type(req.device_id)
        if device_type is None:
            raise HTTPException(404, f"unknown device {req.device_id!r}")
        try:
            params = validate(req.kind, device_type, req.params,
                              set(_freezable_points(device_type)))
        except FaultValidationError as exc:
            raise HTTPException(400, str(exc)) from None
        simulator.set_fault(req.device_id, req.kind, params)
        return {"device_id": req.device_id, "device_type": device_type,
                "kind": req.kind, "params": params}

    @app.delete("/admin/faults/{device_id}")
    def clear_fault(device_id: str):
        simulator.clear_fault(device_id)
        return {"cleared": device_id}

    @app.delete("/admin/faults")
    def clear_faults():
        simulator.clear_all_faults()
        return {"cleared": "all"}

    # -- environment and clock -------------------------------------------

    @app.post("/admin/room")
    def set_room(req: RoomRequest):
        simulator.room_rh_override = req.rh_pct
        return {"rh_pct": req.rh_pct}

    @app.post("/admin/clock")
    def set_clock(req: ClockRequest):
        simulator.clock.set_scale(req.scale)
        return {"scale": req.scale}

    @app.post("/admin/reset")
    def reset():
        simulator.overrides.clear()
        simulator.clear_all_faults()
        simulator.room_rh_override = None
        simulator.clock.set_scale(1.0)
        for r in simulator.state.racks.values():
            r.pinned = False
        return {"reset": True}

    return app
