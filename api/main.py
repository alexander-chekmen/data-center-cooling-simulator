"""Telemetry API: runs the collector, serves live state over WebSocket.

    python -m api.main   ->   http://localhost:8000

Everything this serves comes from what the COLLECTOR observed over Modbus. It
never reads simulator internals -- if a value is not exposed as a register, it
does not appear here.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from collector.collector import Collector, DeviceReading
from sim.devices import load_inventory

STATIC = Path(__file__).parent / "static"

# Cooling electrical draw is estimated from delivered cooling via a coefficient
# of performance; the collector cannot observe chiller power directly.
ASSUMED_COP = 3.4
DISTRIBUTION_LOSS_FRAC = 0.06


class Hub:
    """Latest observed state plus a WebSocket fan-out."""

    def __init__(self, endpoints: list | None = None) -> None:
        self.layout: list[dict] = [
            {"id": e.device_id, "row": e.row, "position": e.position,
             "crv": e.crv, "cdu": e.cdu}
            for e in (endpoints or []) if e.device_type == "PDU"
        ]
        self.devices: dict[str, dict] = {}
        self.clients: set[WebSocket] = set()
        self.collector: Collector | None = None
        self.identity_events: list[dict] = []
        self.started = time.time()

    def on_reading(self, r: DeviceReading) -> None:
        self.devices[r.device_id] = {
            "id": r.device_id,
            "type": r.device_type,
            "online": r.online,
            "ts": r.timestamp,
            "latency_ms": round(r.latency_ms, 1),
            "bad_points": r.bad_points,
            "values": r.values,
        }
        if r.identity_changed:
            self.identity_events.append({
                "device": r.device_id, "ts": r.timestamp,
                "previous_serial": r.previous_serial,
                "current_serial": r.values.get("serial_number"),
            })

    def snapshot(self) -> dict:
        racks, crvs, cdus, alarms = [], [], [], []
        it_kw = cooling_kw = 0.0

        for d in self.devices.values():
            v = d["values"]
            entry = {"id": d["id"], "online": d["online"],
                     "latency_ms": d["latency_ms"], **v}
            codes = v.get("alarm_word") or []
            for c in codes:
                alarms.append({"device": d["id"], "code": c})

            if d["type"] == "PDU":
                racks.append(entry)
                if d["online"]:
                    it_kw += float(v.get("total_power") or 0.0)
            elif d["type"] == "CRV":
                crvs.append(entry)
                if d["online"]:
                    cooling_kw += float(v.get("cooling_output") or 0.0)
            elif d["type"] == "CDU":
                cdus.append(entry)
                if d["online"]:
                    cooling_kw += float(v.get("cooling_load") or 0.0)

        pue = 0.0
        if it_kw > 0.5:
            pue = (it_kw + cooling_kw / ASSUMED_COP
                   + it_kw * DISTRIBUTION_LOSS_FRAC) / it_kw

        racks.sort(key=lambda r: r["id"])
        crvs.sort(key=lambda r: r["id"])
        cdus.sort(key=lambda r: r["id"])

        offline = [d["id"] for d in self.devices.values() if not d["online"]]
        return {
            "t": time.time(),
            "racks": racks, "crvs": crvs, "cdus": cdus,
            "alarms": alarms,
            "summary": {
                "it_kw": round(it_kw, 1),
                "cooling_kw": round(cooling_kw, 1),
                "pue": round(pue, 3),
                "alarm_count": len(alarms),
                "offline": offline,
                "devices": len(self.devices),
                "requests": self.collector.request_count if self.collector else 0,
                "identity_events": len(self.identity_events),
            },
            "identity_events": self.identity_events[-10:],
            "layout": self.layout,
        }

    async def broadcast(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if not self.clients:
                continue
            payload = json.dumps(self.snapshot())
            for ws in list(self.clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    self.clients.discard(ws)


def create_app(endpoints: list | None = None) -> FastAPI:
    endpoints = endpoints or load_inventory()
    hub = Hub(endpoints)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.collector = Collector(emit=hub.on_reading, endpoints=endpoints)
        await hub.collector.start()
        task = asyncio.create_task(hub.broadcast())
        yield
        task.cancel()
        await hub.collector.stop()

    app = FastAPI(title="ThermalEdge Telemetry", lifespan=lifespan)
    app.state.hub = hub

    @app.get("/api/health")
    def health():
        c = hub.collector
        return {
            "ok": True,
            "uptime_s": round(time.time() - hub.started, 1),
            "devices": len(hub.devices),
            "online": sum(1 for d in hub.devices.values() if d["online"]),
            "modbus_requests": c.request_count if c else 0,
            "poll_cycles": c.poll_count if c else 0,
        }

    @app.get("/api/topology")
    def topology():
        """Site layout: which cooling units serve which racks.

        Configuration the monitoring system holds, not telemetry — no real BMS
        asks a rack PDU which CRV is in front of it.
        """
        return {"racks": hub.layout}

    @app.get("/api/snapshot")
    def snapshot():
        return hub.snapshot()

    @app.get("/api/devices/{device_id}")
    def device(device_id: str):
        return hub.devices.get(device_id, {"error": "unknown device"})

    @app.websocket("/ws/telemetry")
    async def telemetry(ws: WebSocket):
        await ws.accept()
        hub.clients.add(ws)
        try:
            await ws.send_text(json.dumps(hub.snapshot()))
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(ws)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, log_level="warning")
