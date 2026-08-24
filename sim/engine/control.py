"""PI/PID controllers for pump and fan speed.

Gain scaling with dt matters here. Phase 2 runs this identical engine at
dt=10s to generate history while live mode runs at dt=1s. If the integral and
derivative terms are not scaled correctly by dt, historical behavior differs
subtly from live behavior and the seam becomes visible.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PID:
    kp: float
    ki: float = 0.0
    kd: float = 0.0
    setpoint: float = 0.0
    out_min: float = 0.0
    out_max: float = 100.0
    reverse: bool = False
    """If True, an increase in measurement should increase the output.

    Cooling loops are reverse-acting: rising temperature must raise pump speed.
    """

    integral: float = field(default=0.0)
    _prev_error: float | None = field(default=None)

    def reset(self, integral: float = 0.0) -> None:
        self.integral = integral
        self._prev_error = None

    def step(self, measurement: float, dt: float) -> float:
        if dt <= 0:
            raise ValueError("dt must be positive")

        error = self.setpoint - measurement
        if self.reverse:
            error = -error

        p = self.kp * error

        d = 0.0
        if self.kd > 0 and self._prev_error is not None:
            d = self.kd * (error - self._prev_error) / dt
        self._prev_error = error

        # Anti-windup by conditional integration. A hard clamp on the integral
        # is not enough: once wound up, the integral only unwinds if the error
        # reverses sign, so a controller sitting exactly at setpoint stays
        # saturated forever. Instead, refuse to integrate when the output is
        # already saturated and the error would push it further out of range.
        candidate = self.integral + error * dt
        unclamped = p + self.ki * candidate + d

        saturated_high = unclamped > self.out_max
        saturated_low = unclamped < self.out_min
        pushing_further = (saturated_high and error > 0) or (saturated_low and error < 0)

        if not pushing_further:
            self.integral = candidate

        return max(self.out_min, min(self.out_max, p + self.ki * self.integral + d))


def snapshot(pid: PID) -> dict:
    """Serialize controller state for the Phase 2 history/live seam.

    The integral term MUST cross the seam. Omitting it produces a subtle
    discontinuity that is painful to diagnose later.
    """
    return {"integral": pid.integral, "prev_error": pid._prev_error}


def restore(pid: PID, state: dict) -> None:
    pid.integral = state.get("integral", 0.0)
    pid._prev_error = state.get("prev_error")
