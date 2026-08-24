"""The fault catalogue: which faults apply to which equipment, and their inputs.

A pump failure means nothing to an in-row air cooler, and a fan failure means
nothing to a coolant distribution unit. Offering every fault for every device
produces a control that returns success and then silently does nothing, which is
worse than an error. This module is the single source of truth: the API
validates against it and the UI builds itself from it, so the two cannot drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ALL_TYPES = frozenset({"CRV", "CDU", "PDU"})


@dataclass(frozen=True)
class FaultSpec:
    kind: str
    label: str
    device_types: frozenset[str]
    layer: str                       # "physics" | "comms"
    group: str                       # UI section: "cdu" | "crv" | "comms"
    description: str
    params: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "label": self.label,
            "device_types": sorted(self.device_types),
            "layer": self.layer, "group": self.group,
            "description": self.description, "params": self.params,
        }


CATALOGUE: dict[str, FaultSpec] = {
    f.kind: f for f in [
        FaultSpec(
            kind="pump_failure", label="Pump failure",
            device_types=frozenset({"CDU"}), layer="physics", group="cdu",
            description="Stops a pump. With N+1 redundancy the standby takes over; "
                        "losing both collapses flow and heat reverts to the air path.",
            params={"pump": {"type": "choice", "options": ["all", "A", "B"],
                             "default": "all", "label": "Pump"}},
        ),
        FaultSpec(
            kind="flow_clamp", label="Reduced coolant flow",
            device_types=frozenset({"CDU"}), layer="physics", group="cdu",
            description="Caps pump speed, so the loop cannot keep up with the heat load.",
            params={"max_pct": {"type": "number", "min": 0, "max": 100,
                                "default": 25, "label": "Max pump speed %"}},
        ),
        FaultSpec(
            kind="fan_failure", label="Fan failure",
            device_types=frozenset({"CRV"}), layer="physics", group="crv",
            description="Stops the fan. Airflow collapses and the unit rejects no heat.",
        ),
        FaultSpec(
            kind="airflow_clamp", label="Reduced airflow",
            device_types=frozenset({"CRV"}), layer="physics", group="crv",
            description="Caps fan speed, reducing air-side cooling capacity.",
            params={"max_pct": {"type": "number", "min": 0, "max": 100,
                                "default": 30, "label": "Max fan speed %"}},
        ),
        FaultSpec(
            kind="sensor_failure", label="Sensor freeze",
            device_types=ALL_TYPES, layer="comms", group="comms",
            description="One reading stops updating while the rest stay live. The "
                        "equipment is healthy; the measurement is not.",
            params={"point": {"type": "point", "label": "Frozen point"}},
        ),
        FaultSpec(
            kind="network_latency", label="Network latency",
            device_types=ALL_TYPES, layer="comms", group="comms",
            description="Delays every Modbus response, testing whether the collector's "
                        "poll scheduler survives a slow device.",
            params={"ms": {"type": "number", "min": 50, "max": 5000,
                           "default": 900, "label": "Delay ms"}},
        ),
        FaultSpec(
            kind="device_offline", label="Device offline",
            device_types=ALL_TYPES, layer="comms", group="comms",
            description="Accepts the connection but never answers, so the collector "
                        "experiences a real read timeout and marks the device OFFLINE.",
        ),
    ]
}

GROUPS = [
    {"id": "cdu", "title": "Coolant distribution faults",
     "subtitle": "Liquid loop · CDU only", "device_types": ["CDU"]},
    {"id": "crv", "title": "In-row cooling faults",
     "subtitle": "Air loop · CRV only", "device_types": ["CRV"]},
    {"id": "comms", "title": "Communication faults",
     "subtitle": "Any device · equipment stays healthy",
     "device_types": ["CRV", "CDU", "PDU"]},
]


class FaultValidationError(ValueError):
    pass


def validate(kind: str, device_type: str, params: dict, valid_points: set[str]) -> dict:
    """Check a fault request and return its normalized parameters."""
    spec = CATALOGUE.get(kind)
    if spec is None:
        raise FaultValidationError(
            f"unknown fault {kind!r}; known: {sorted(CATALOGUE)}"
        )
    if device_type not in spec.device_types:
        raise FaultValidationError(
            f"{spec.label!r} does not apply to a {device_type}. "
            f"It applies to: {', '.join(sorted(spec.device_types))}."
        )

    out: dict = {}
    for name, schema in spec.params.items():
        supplied = params.get(name, schema.get("default"))
        if schema["type"] == "choice":
            if supplied not in schema["options"]:
                raise FaultValidationError(
                    f"{name} must be one of {schema['options']}, got {supplied!r}"
                )
        elif schema["type"] == "number":
            try:
                supplied = float(supplied)
            except (TypeError, ValueError):
                raise FaultValidationError(f"{name} must be a number") from None
            if not schema["min"] <= supplied <= schema["max"]:
                raise FaultValidationError(
                    f"{name} must be between {schema['min']} and {schema['max']}"
                )
        elif schema["type"] == "point":
            if supplied is None:
                raise FaultValidationError(f"{name} is required for {spec.label!r}")
            if supplied not in valid_points:
                raise FaultValidationError(
                    f"{supplied!r} is not a readable point on this device"
                )
        out[name] = supplied
    return out
