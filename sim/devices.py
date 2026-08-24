"""Device inventory: what exists, where it listens, and what map it speaks.

Both the simulator (which serves these endpoints) and the collector (which polls
them) read this file. The collector treats it exactly as it would a real device
inventory -- host, port, unit id, device type -- with no knowledge that the
other end is simulated.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from sim.topology import Topology, default_topology

INVENTORY_PATH = Path(__file__).resolve().parents[1] / "config" / "devices.yaml"

# Under Docker Compose the collector reaches the simulator by service name.
DEVICE_HOST = os.environ.get("THERMALEDGE_DEVICE_HOST", "127.0.0.1")

CRV_PORT_BASE = 5020
CDU_PORT_BASE = 5030
PDU_GATEWAY_PORT = 5040          # all rack PDUs behind one gateway, by unit id


@dataclass(frozen=True)
class Endpoint:
    device_id: str
    device_type: str             # CRV | CDU | PDU
    host: str
    port: int
    unit_id: int
    # Site layout. A monitoring system knows which cooling unit serves which
    # rack from its own commissioning records, not by asking the equipment --
    # so this travels with the inventory rather than over Modbus.
    row: str | None = None
    position: int | None = None
    crv: str | None = None
    cdu: str | None = None


def build_inventory(topo: Topology | None = None, host: str | None = None) -> list[Endpoint]:
    topo = topo or default_topology()
    host = host or DEVICE_HOST
    eps: list[Endpoint] = []

    for i, crv in enumerate(topo.crvs):
        eps.append(Endpoint(crv.id, "CRV", host, CRV_PORT_BASE + i, 1))
    for i, cdu in enumerate(topo.cdus):
        eps.append(Endpoint(cdu.id, "CDU", host, CDU_PORT_BASE + i, 1))
    # Rack PDUs share one listener and are addressed by unit id, the way a
    # serial-to-TCP gateway fronts many devices on one endpoint.
    for i, rack in enumerate(topo.racks, start=1):
        eps.append(Endpoint(rack.id, "PDU", host, PDU_GATEWAY_PORT, i,
                            row=rack.row, position=rack.position,
                            crv=rack.crv_id, cdu=rack.cdu_id))
    return eps


def write_inventory(path: Path = INVENTORY_PATH, host: str | None = None) -> None:
    eps = build_inventory(host=host)
    payload = {
        "site": "houston",
        "devices": [
            {k: v for k, v in
             {"id": e.device_id, "type": e.device_type, "host": e.host,
              "port": e.port, "unit_id": e.unit_id, "row": e.row,
              "position": e.position, "crv": e.crv, "cdu": e.cdu}.items()
             if v is not None}
            for e in eps
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Device inventory. The collector is configured from this file and has\n"
        "# no knowledge that the endpoints are simulated.\n"
        + yaml.safe_dump(payload, sort_keys=False)
    )


def load_inventory(path: Path = INVENTORY_PATH) -> list[Endpoint]:
    # A fresh checkout has no generated inventory yet (it is written on
    # simulator startup), so fall back to building it rather than failing.
    if not path.exists():
        return build_inventory()
    raw = yaml.safe_load(path.read_text())
    return [
        Endpoint(d["id"], d["type"], d["host"], d["port"], d["unit_id"],
                 row=d.get("row"), position=d.get("position"),
                 crv=d.get("crv"), cdu=d.get("cdu"))
        for d in raw["devices"]
    ]
