"""IT load -> electrical power -> heat, with thermal throttling.

Two details make this credible rather than a multiplication:

1. Power is not linear in utilization. An idle server still draws a large
   fraction of its peak.
2. Throttling closes the loop. Cooling affects compute, not only the reverse.
   Without it, a pump-failure fault drives temperatures to absurd values;
   with it, the simulation is self-limiting.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NodeProfile:
    """Per-node power characteristics."""
    name: str
    cpu_idle_w: float
    cpu_max_w: float
    gpu_idle_w: float
    gpu_max_w: float
    gpu_tdp_cap_w: float
    mem_storage_w: float
    k_cpu: float = 1.4          # mildly superlinear
    k_gpu: float = 1.1          # close to linear once loaded


@dataclass(frozen=True)
class ThrottlePolicy:
    start_c: float = 32.0       # derating begins
    end_c: float = 40.0         # derating floor reached
    min_derate: float = 0.35
    critical_c: float = 45.0    # node sheds load entirely


# Air-inlet thresholds do not apply to coolant. A direct-to-chip loop running a
# 38 degC return is operating normally; judging it against the 32 degC air-inlet
# threshold would throttle every liquid-cooled rack during healthy operation.
AIR_THROTTLE = ThrottlePolicy(start_c=32.0, end_c=40.0, min_derate=0.35, critical_c=45.0)
LIQUID_THROTTLE = ThrottlePolicy(start_c=44.0, end_c=54.0, min_derate=0.35, critical_c=60.0)


@dataclass(frozen=True)
class RackLoad:
    power_w: float
    u_cpu_effective: float
    u_gpu_effective: float
    derate: float
    throttled: bool
    shutdown: bool


# Reference profiles. These are design assumptions for the simulation, chosen to
# land in realistic territory: an air-cooled CPU rack around 8-15 kW, a dense
# GPU rack around 30-40 kW.
PROFILES: dict[str, NodeProfile] = {
    "cpu": NodeProfile(
        name="cpu",
        cpu_idle_w=180.0, cpu_max_w=400.0,
        gpu_idle_w=0.0, gpu_max_w=0.0, gpu_tdp_cap_w=0.0,
        mem_storage_w=90.0,
    ),
    "gpu": NodeProfile(
        name="gpu",
        cpu_idle_w=200.0, cpu_max_w=450.0,
        gpu_idle_w=8 * 85.0, gpu_max_w=8 * 700.0, gpu_tdp_cap_w=8 * 700.0,
        mem_storage_w=250.0,
    ),
}


def node_power_w(profile: NodeProfile, u_cpu: float, u_gpu: float) -> float:
    """Electrical power for one node at the given effective utilizations."""
    u_cpu = max(0.0, min(1.0, u_cpu))
    u_gpu = max(0.0, min(1.0, u_gpu))

    p_cpu = profile.cpu_idle_w + (profile.cpu_max_w - profile.cpu_idle_w) * (u_cpu ** profile.k_cpu)

    if profile.gpu_max_w > 0:
        p_gpu = profile.gpu_idle_w + (profile.gpu_max_w - profile.gpu_idle_w) * (u_gpu ** profile.k_gpu)
        p_gpu = min(p_gpu, profile.gpu_tdp_cap_w)
    else:
        p_gpu = 0.0

    return p_cpu + p_gpu + profile.mem_storage_w


def derate_factor(policy: ThrottlePolicy, inlet_c: float) -> float:
    """Fraction of requested utilization the hardware will actually sustain."""
    if inlet_c <= policy.start_c:
        return 1.0
    if inlet_c >= policy.end_c:
        return policy.min_derate
    span = policy.end_c - policy.start_c
    frac = (inlet_c - policy.start_c) / span
    return 1.0 - frac * (1.0 - policy.min_derate)


def rack_load_at_derate(
    profile: NodeProfile,
    n_nodes: int,
    u_cpu_requested: float,
    u_gpu_requested: float,
    derate: float,
    shutdown: bool,
    psu_overhead_frac: float = 0.08,
) -> RackLoad:
    """Delivered load and rack power for an already-computed derate factor.

    Callers with more than one cooling path (a direct-to-chip rack is cooled by
    both air and coolant) compute the derate for each path and pass the most
    restrictive one here.
    """
    if shutdown:
        idle = node_power_w(profile, 0.0, 0.0) * 0.15 * n_nodes
        return RackLoad(
            power_w=idle * (1 + psu_overhead_frac),
            u_cpu_effective=0.0, u_gpu_effective=0.0,
            derate=0.0, throttled=True, shutdown=True,
        )

    u_cpu = u_cpu_requested * derate
    u_gpu = u_gpu_requested * derate
    power = node_power_w(profile, u_cpu, u_gpu) * n_nodes * (1 + psu_overhead_frac)
    return RackLoad(
        power_w=power,
        u_cpu_effective=u_cpu,
        u_gpu_effective=u_gpu,
        derate=derate,
        throttled=derate < 1.0,
        shutdown=False,
    )


def rack_load(
    profile: NodeProfile,
    n_nodes: int,
    u_cpu_requested: float,
    u_gpu_requested: float,
    inlet_c: float,
    policy: ThrottlePolicy = AIR_THROTTLE,
    psu_overhead_frac: float = 0.08,
) -> RackLoad:
    """Requested load + inlet temperature -> delivered load and rack power."""
    if inlet_c >= policy.critical_c:
        # Thermal shutdown: heat collapses to near zero. This is what stops a
        # cooling failure from running away.
        idle = node_power_w(profile, 0.0, 0.0) * 0.15 * n_nodes
        return RackLoad(
            power_w=idle * (1 + psu_overhead_frac),
            u_cpu_effective=0.0, u_gpu_effective=0.0,
            derate=0.0, throttled=True, shutdown=True,
        )

    derate = derate_factor(policy, inlet_c)
    u_cpu = u_cpu_requested * derate
    u_gpu = u_gpu_requested * derate
    power = node_power_w(profile, u_cpu, u_gpu) * n_nodes * (1 + psu_overhead_frac)

    return RackLoad(
        power_w=power,
        u_cpu_effective=u_cpu,
        u_gpu_effective=u_gpu,
        derate=derate,
        throttled=derate < 1.0,
        shutdown=False,
    )
