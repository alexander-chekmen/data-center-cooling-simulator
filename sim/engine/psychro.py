"""Psychrometrics.

The dew-point constraint is the reason this module exists: a CDU may not drive
supply coolant below the room dew point plus a margin, or water condenses on
the cold plates. It is a constraint that fights the control objective, which
makes it far more interesting than a threshold alarm.
"""
from __future__ import annotations

import math

# Magnus-Tetens coefficients (over water, valid roughly 0-60 degC)
_A = 17.625
_B = 243.04


def dew_point_c(temp_c: float, rh_pct: float) -> float:
    """Dew point from dry-bulb temperature and relative humidity."""
    rh = max(0.1, min(100.0, rh_pct))
    gamma = (_A * temp_c) / (_B + temp_c) + math.log(rh / 100.0)
    return (_B * gamma) / (_A - gamma)


def rh_from_dew_point(temp_c: float, dew_c: float) -> float:
    """Inverse of dew_point_c."""
    gamma = (_A * dew_c) / (_B + dew_c)
    ln_rh = gamma - (_A * temp_c) / (_B + temp_c)
    return max(0.0, min(100.0, 100.0 * math.exp(ln_rh)))


def dewpoint_limited_supply(setpoint_c: float, dew_c: float, margin_k: float) -> float:
    """Lowest permissible coolant supply temperature.

    Returns the setpoint unless doing so would bring the supply within `margin_k`
    of the dew point, in which case the dew-point limit wins.
    """
    return max(setpoint_c, dew_c + margin_k)
