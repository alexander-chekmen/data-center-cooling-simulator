"""Deterministic device identity: model, serial number, firmware, components.

Serials MUST be stable across restarts. If they regenerate, the collector's
device-substitution detector fires spuriously on every boot, and Phase 2's
generated history stops matching the live fleet. They are therefore derived by
hash from a fixed seed and the device id, never randomly generated.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

SEED = "thermaledge-v1"
VENDOR = "ThermalEdge Simulated Equipment"

MODELS = {"CRV": "TE-CRV-350A", "CDU": "TE-CDU-1200L", "PDU": "TE-PDU-63A3P"}

# A fleet in the field is never uniformly patched. Spreading devices across a
# few firmware revisions makes drift detection a real thing to test.
FIRMWARE_POOL = ["4.2.3", "4.2.3", "4.2.3", "4.2.1", "4.1.9"]


@dataclass(frozen=True)
class Identity:
    vendor: str
    model: str
    serial: str
    firmware: str
    hardware_revision: int

    def as_dict(self) -> dict:
        return {
            "vendor": self.vendor, "model": self.model, "serial": self.serial,
            "firmware": self.firmware, "hardware_revision": self.hardware_revision,
        }


def _hash(*parts: str) -> str:
    return hashlib.sha256((SEED + "|" + "|".join(parts)).encode()).hexdigest().upper()


def _serial(prefix: str, *parts: str) -> str:
    h = _hash(*parts)
    return f"{prefix}-{h[:4]}-{h[4:10]}"


def device_identity(device_id: str, device_type: str) -> Identity:
    h = _hash("device", device_id)
    return Identity(
        vendor=VENDOR,
        model=MODELS.get(device_type, "TE-GEN-000"),
        serial=_serial(f"TE-{device_type}", "device", device_id),
        firmware=FIRMWARE_POOL[int(h[:2], 16) % len(FIRMWARE_POOL)],
        hardware_revision=1 + int(h[2:4], 16) % 3,
    )


def component_serial(device_id: str, component: str) -> str:
    """Serial for a sub-assembly (a pump, a fan module) inside a device."""
    return _serial("TE-CMP", "component", device_id, component)


def rack_asset_tag(rack_id: str) -> str:
    """The RACK's asset tag, which is not the PDU's serial.

    A rack is an asset with a tag; the PDU bolted into it is a device with a
    serial. Replacing a failed PDU leaves the rack's identity untouched, so the
    two must never be conflated.
    """
    return f"AST-{_hash('rack', rack_id)[:8]}"
