"""Entrypoint: Modbus TCP listeners + the simulator control API.

    python -m sim.main

Serves Modbus on 5020-5023 (CRV), 5030-5031 (CDU) and 5040 (rack PDU gateway,
by unit id), and the control API on http://localhost:8090/docs
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from sim.admin_api import create_app
from sim.devices import write_inventory

if __name__ == "__main__":
    write_inventory()
    uvicorn.run(create_app(), host="0.0.0.0", port=8090, log_level="warning")
