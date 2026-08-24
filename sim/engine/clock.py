"""Time as an injected dependency.

This is load-bearing infrastructure, not a testing convenience. The same
simulation engine runs under three clocks:

    WallClock      live mode, gated to real time
    VirtualClock   fast-forward history generation and pytest
    ScaledClock    UI demo speed (10x / 60x)
"""
from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Current simulated time as a unix timestamp."""

    def sleep(self, seconds: float) -> None:
        """Advance simulated time by `seconds`."""


class VirtualClock:
    """Advances instantly. Never touches the wall clock."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += float(seconds)

    advance = sleep


class WallClock:
    """Real time, 1:1."""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class ScaledClock:
    """Simulated time advances `scale` times faster than real time."""

    def __init__(self, scale: float = 1.0, start: float | None = None) -> None:
        if scale <= 0:
            raise ValueError("scale must be positive")
        self.scale = float(scale)
        self._sim_origin = float(start) if start is not None else time.time()
        self._real_origin = time.time()

    def now(self) -> float:
        return self._sim_origin + (time.time() - self._real_origin) * self.scale

    def sleep(self, seconds: float) -> None:
        real = seconds / self.scale
        if real > 0:
            time.sleep(real)

    def set_scale(self, scale: float) -> None:
        """Re-anchor so simulated time is continuous across a speed change."""
        if scale <= 0:
            raise ValueError("scale must be positive")
        self._sim_origin = self.now()
        self._real_origin = time.time()
        self.scale = float(scale)
