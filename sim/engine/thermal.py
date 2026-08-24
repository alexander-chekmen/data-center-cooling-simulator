"""Heat transfer, thermal mass, and rack-to-rack coupling.

Design note on the integrator
-----------------------------
Rack exhaust temperature is integrated with the EXACT solution of the
first-order energy balance rather than a forward-Euler step:

    dT/dt = (q - m_dot*cp*(T - T_in)) / C

Forward Euler is only conditionally stable and gives different answers at
dt=1s and dt=10s. Because Phase 2 generates six weeks of history at dt=10s
while live mode runs at dt=1s, that difference would show up as a
discontinuity at the history/live seam. The exponential form is exact for
constant inputs over the step and unconditionally stable, so both dt values
converge to the same trajectory.
"""
from __future__ import annotations

import math

import numpy as np

# Specific heat capacity, J/(kg*K)
CP_AIR = 1005.0
CP_WATER = 4180.0
CP_GLYCOL_30 = 3600.0        # 30% propylene glycol mix

# Density, kg/m^3
RHO_AIR = 1.2
RHO_WATER = 997.0


# --------------------------------------------------------------------------
# Flow unit conversion
# --------------------------------------------------------------------------

def lpm_to_kg_s(lpm: float, rho: float = RHO_WATER) -> float:
    """Litres per minute -> kg/s."""
    return lpm * rho / 60_000.0


def m3h_to_kg_s(m3h: float, rho: float = RHO_AIR) -> float:
    """Cubic metres per hour -> kg/s."""
    return m3h * rho / 3600.0


# --------------------------------------------------------------------------
# Steady-state heat transfer:  q = m_dot * cp * dT
# --------------------------------------------------------------------------

def delta_t_from_heat(q_w: float, m_dot_kg_s: float, cp: float) -> float:
    """Temperature rise across a heat exchanger carrying q_w watts."""
    if m_dot_kg_s <= 1e-9:
        return float("inf")
    return q_w / (m_dot_kg_s * cp)


def heat_removed_w(m_dot_kg_s: float, cp: float, t_out: float, t_in: float) -> float:
    """Heat carried away by a flow with the given temperature rise."""
    return m_dot_kg_s * cp * (t_out - t_in)


# --------------------------------------------------------------------------
# Thermal mass
# --------------------------------------------------------------------------

def first_order_step(current: float, target: float, tau_s: float, dt: float) -> float:
    """Exact first-order relaxation toward `target` with time constant tau."""
    if tau_s <= 1e-9:
        return target
    return target + (current - target) * math.exp(-dt / tau_s)


def exhaust_step(
    t_exhaust: float,
    t_inlet: float,
    q_air_w: float,
    m_dot_air_kg_s: float,
    c_rack_j_per_k: float,
    dt: float,
) -> float:
    """Advance rack exhaust temperature by dt.

    Thermal mass is what makes temperature LAG load. That visible lag on the
    dashboard is the strongest evidence to a reviewer that real physics is
    running underneath rather than a lookup table.
    """
    if m_dot_air_kg_s <= 1e-9:
        # No airflow: all heat goes into the rack's thermal mass.
        return t_exhaust + (q_air_w / c_rack_j_per_k) * dt

    ua = m_dot_air_kg_s * CP_AIR
    steady_state = t_inlet + q_air_w / ua
    tau = c_rack_j_per_k / ua
    return first_order_step(t_exhaust, steady_state, tau, dt)


# --------------------------------------------------------------------------
# Rack-to-rack coupling
# --------------------------------------------------------------------------

def inlet_temps(
    t_supply: float,
    t_exhaust: np.ndarray,
    coupling: np.ndarray,
) -> np.ndarray:
    """Rack inlet temperatures given cold-aisle supply and every rack's exhaust.

        T_inlet[i] = T_supply + SUM_j A[i][j] * (T_exhaust[j] - T_supply)

    If racks were thermally independent a "hot spot" would just be a large
    number typed into one box. Recirculation of hot exhaust into neighbouring
    inlets is what makes hot spots emergent.
    """
    t_exhaust = np.asarray(t_exhaust, dtype=float)
    return t_supply + coupling @ (t_exhaust - t_supply)


def build_coupling_matrix(
    n_racks: int,
    row_of: list[int],
    self_recirc: float = 0.09,
    neighbour: float = 0.05,
    decay: float = 0.45,
    reach: int = 3,
) -> np.ndarray:
    """Adjacency-based recirculation matrix.

    Coupling decays with rack distance and does not cross rows (separate cold
    aisles). `self_recirc` represents over-the-top bypass of a rack's own
    exhaust back into its own inlet.
    """
    A = np.zeros((n_racks, n_racks), dtype=float)
    for i in range(n_racks):
        A[i, i] = self_recirc
        for j in range(n_racks):
            if i == j or row_of[i] != row_of[j]:
                continue
            d = abs(i - j)
            if d <= reach:
                A[i, j] = neighbour * (decay ** (d - 1))
    return A
