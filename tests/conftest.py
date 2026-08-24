import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from sim.engine.state import StepInputs, build_state, step


@pytest.fixture
def state():
    return build_state()


def settle(s, load, minutes=90, dt=1.0, **kw):
    """Run to steady state under a fixed load."""
    inputs = StepInputs(load=load, **kw)
    for _ in range(int(minutes * 60 / dt)):
        step(s, dt, inputs)
    return s


def uniform(s, u_cpu, u_gpu):
    return {r.id: (u_cpu, u_gpu) for r in s.topo.racks}
