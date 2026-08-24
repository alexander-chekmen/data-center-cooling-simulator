"""Data-center layout: rows, racks, cooling equipment, and the coupling matrix.

Default site: two rows of twelve racks.

    Row A   12 dense GPU racks, direct-to-chip liquid cooled
            CDU-001 serves A01-A06, CDU-002 serves A07-A12
            CRV-001 serves A01-A06, CRV-002 serves A07-A12  (residual air heat)

    Row B   12 conventional CPU racks, fully air cooled
            CRV-003 serves B01-B06, CRV-004 serves B07-B12
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from sim.engine.thermal import build_coupling_matrix


@dataclass(frozen=True)
class RackSpec:
    id: str
    row: str
    position: int
    profile: str                    # key into engine.load.PROFILES
    n_nodes: int
    liquid_capture_frac: float      # 0.0 = fully air cooled
    c_rack_j_per_k: float = 90_000.0
    crv_id: str = ""
    cdu_id: str | None = None


@dataclass(frozen=True)
class CRVSpec:
    id: str
    racks: list[str]
    nominal_airflow_kg_s: float = 6.0
    max_cooling_kw: float = 70.0
    supply_air_setpoint_c: float = 18.0
    return_air_setpoint_c: float = 32.0


@dataclass(frozen=True)
class CDUSpec:
    id: str
    racks: list[str]
    nominal_flow_lpm: float = 300.0
    max_cooling_kw: float = 250.0
    supply_fluid_setpoint_c: float = 28.0
    return_temp_limit_c: float = 38.0
    dewpoint_margin_k: float = 2.0


@dataclass
class Topology:
    site: str
    racks: list[RackSpec]
    crvs: list[CRVSpec]
    cdus: list[CDUSpec]
    coupling: np.ndarray = field(default=None)

    def __post_init__(self) -> None:
        if self.coupling is None:
            rows = sorted({r.row for r in self.racks})
            row_index = {r: i for i, r in enumerate(rows)}
            self.coupling = build_coupling_matrix(
                len(self.racks), [row_index[r.row] for r in self.racks]
            )
        self.rack_index = {r.id: i for i, r in enumerate(self.racks)}

    def rack(self, rack_id: str) -> RackSpec:
        return self.racks[self.rack_index[rack_id]]


def default_topology() -> Topology:
    racks: list[RackSpec] = []

    for i in range(1, 13):
        cdu = "CDU-001" if i <= 6 else "CDU-002"
        crv = "CRV-001" if i <= 6 else "CRV-002"
        racks.append(RackSpec(
            id=f"RACK-A{i:02d}", row="A", position=i,
            profile="gpu", n_nodes=5, liquid_capture_frac=0.75,
            c_rack_j_per_k=110_000.0, crv_id=crv, cdu_id=cdu,
        ))

    for i in range(1, 13):
        crv = "CRV-003" if i <= 6 else "CRV-004"
        racks.append(RackSpec(
            id=f"RACK-B{i:02d}", row="B", position=i,
            profile="cpu", n_nodes=28, liquid_capture_frac=0.0,
            c_rack_j_per_k=90_000.0, crv_id=crv, cdu_id=None,
        ))

    def ids(row: str, lo: int, hi: int) -> list[str]:
        return [f"RACK-{row}{i:02d}" for i in range(lo, hi + 1)]

    # Cooling is sized to DESIGN load, which assumes a diversity factor -- real
    # rows are not specified for every rack running flat out simultaneously.
    # Driving the whole floor to 100% therefore exceeds capacity, which is what
    # makes the overload scenario produce a real thermal response rather than a
    # shrug.
    crvs = [
        CRVSpec("CRV-001", ids("A", 1, 6),  nominal_airflow_kg_s=6.0, max_cooling_kw=55.0),
        CRVSpec("CRV-002", ids("A", 7, 12), nominal_airflow_kg_s=6.0, max_cooling_kw=55.0),
        CRVSpec("CRV-003", ids("B", 1, 6),  nominal_airflow_kg_s=6.5, max_cooling_kw=72.0),
        CRVSpec("CRV-004", ids("B", 7, 12), nominal_airflow_kg_s=6.5, max_cooling_kw=72.0),
    ]
    cdus = [
        CDUSpec("CDU-001", ids("A", 1, 6)),
        CDUSpec("CDU-002", ids("A", 7, 12)),
    ]
    return Topology(site="houston", racks=racks, crvs=crvs, cdus=cdus)
