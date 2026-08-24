"""Register map loading, validation and Modbus codec.

This module is the single source of truth for every telemetry point. Both the
simulator (which serves registers) and the collector (which reads them) build
from the same YAML files, so the two cannot drift apart.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REGISTERS_DIR = Path(__file__).parent / "registers"

WIDTHS = {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2}
SIGNED = {"int16", "int32"}

# Strings occupy ceil(chars/2) registers, two ASCII characters per 16-bit word,
# first character in the HIGH byte. Character order within the word is a classic
# source of Modbus integration bugs, so it is pinned by tests.
STRING = "string"


class RegisterMapError(ValueError):
    """Raised when a register map is internally inconsistent."""


class RangeError(ValueError):
    """Raised when a decoded value falls outside its declared range."""


@dataclass(frozen=True)
class Point:
    key: str
    modicon: int
    offset: int
    type: str
    space: str                      # "input" | "holding"
    poll_class: str
    source: str                     # "published" | "inferred"
    scale: float = 1.0
    unit: str | None = None
    range: tuple[float, float] | None = None
    kind: str = "analog"            # "analog" | "enum" | "bitfield" | "identity"
    enum: dict[int, str] = field(default_factory=dict)
    bits: dict[int, str] = field(default_factory=dict)
    chars: int = 0                  # string points only

    @property
    def width(self) -> int:
        if self.type == STRING:
            return (self.chars + 1) // 2
        return WIDTHS[self.type]

    @property
    def last_offset(self) -> int:
        return self.offset + self.width - 1


@dataclass
class RegisterMap:
    device_type: str
    description: str
    points: list[Point]                       # input space
    setpoints: list[Point] = field(default_factory=list)   # holding space

    def __post_init__(self) -> None:
        self._by_key = {p.key: p for p in self.all_points}
        self.validate()

    @property
    def all_points(self) -> list[Point]:
        return self.points + self.setpoints

    def by_key(self, key: str) -> Point:
        try:
            return self._by_key[key]
        except KeyError:
            raise RegisterMapError(
                f"{self.device_type}: no point named {key!r}"
            ) from None

    def space(self, space: str) -> list[Point]:
        return self.points if space == "input" else self.setpoints

    def window(self, space: str, address: int, count: int) -> list[Point]:
        """Points wholly contained in [address, address+count)."""
        end = address + count
        return [
            p for p in self.space(space)
            if p.offset >= address and p.last_offset < end
        ]

    def validate(self) -> None:
        for space_name, pts in (("input", self.points), ("holding", self.setpoints)):
            base = 30001 if space_name == "input" else 40001
            seen: dict[int, str] = {}
            for p in pts:
                if p.offset != p.modicon - base:
                    raise RegisterMapError(
                        f"{self.device_type}.{p.key}: offset {p.offset} does not "
                        f"match modicon {p.modicon} (expected {p.modicon - base}). "
                        f"Modicon {base} is offset 0."
                    )
                if p.type == STRING:
                    if p.chars <= 0:
                        raise RegisterMapError(
                            f"{self.device_type}.{p.key}: string point needs `chars`"
                        )
                elif p.type not in WIDTHS:
                    raise RegisterMapError(
                        f"{self.device_type}.{p.key}: unknown type {p.type!r}"
                    )
                for reg in range(p.offset, p.offset + p.width):
                    if reg in seen:
                        raise RegisterMapError(
                            f"{self.device_type}: register {reg} claimed by both "
                            f"{seen[reg]!r} and {p.key!r}"
                        )
                    seen[reg] = p.key
                if p.kind == "analog" and p.type != STRING and p.range is None:
                    raise RegisterMapError(
                        f"{self.device_type}.{p.key}: analog point needs a range"
                    )


# YAML 1.1 parses bare OFF/ON/YES/NO/TRUE/FALSE as booleans, silently turning
# an enum label like OFF into False. Catching that here is far cheaper than
# debugging a KeyError from the encoder at runtime.
def _labels(raw: dict, field: str, key: str) -> dict[int, str]:
    out = {}
    for k, v in (raw.get(field) or {}).items():
        if not isinstance(v, str):
            raise RegisterMapError(
                f"{key}.{field}[{k}]: value {v!r} is not a string. Bare "
                f"OFF/ON/YES/NO/TRUE/FALSE are parsed as booleans by YAML - "
                f"quote them."
            )
        out[int(k)] = v
    return out


def _point(raw: dict[str, Any], space: str) -> Point:
    rng = raw.get("range")
    return Point(
        key=raw["key"],
        modicon=raw["modicon"],
        offset=raw["offset"],
        type=raw["type"],
        space=space,
        poll_class=raw["poll_class"],
        source=raw["source"],
        scale=float(raw.get("scale", 1.0)),
        unit=raw.get("unit"),
        range=(float(rng[0]), float(rng[1])) if rng else None,
        kind=raw.get("kind", "analog"),
        chars=int(raw.get("chars", 0)),
        enum=_labels(raw, "enum", raw["key"]),
        bits=_labels(raw, "bits", raw["key"]),
    )


def load_map(name: str) -> RegisterMap:
    """Load a register map by device type name, e.g. load_map("crv")."""
    path = REGISTERS_DIR / f"{name.lower()}.yaml"
    raw = yaml.safe_load(path.read_text())
    sp = raw.get("setpoints") or {}
    return RegisterMap(
        device_type=raw["device_type"],
        description=raw.get("description", ""),
        points=[_point(p, "input") for p in raw["points"]],
        setpoints=[_point(p, "holding") for p in sp.get("points", [])],
    )


def load_all() -> dict[str, RegisterMap]:
    return {
        p.stem.upper(): load_map(p.stem)
        for p in sorted(REGISTERS_DIR.glob("*.yaml"))
    }


# --------------------------------------------------------------------------
# Modbus codec
# --------------------------------------------------------------------------

def _to_words(value: int, width: int) -> list[int]:
    if width == 1:
        return [value & 0xFFFF]
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]   # high word first


def _from_words(regs: list[int], width: int) -> int:
    if width == 1:
        return regs[0] & 0xFFFF
    return ((regs[0] & 0xFFFF) << 16) | (regs[1] & 0xFFFF)


def _sign(raw: int, width: int) -> int:
    bits = 16 * width
    return raw - (1 << bits) if raw >= (1 << (bits - 1)) else raw


def encode_string(text: str, chars: int) -> list[int]:
    """ASCII -> registers, two characters per word, first char in the high byte."""
    padded = text[:chars].ljust(chars, "\0").encode("ascii", errors="replace")
    return [(padded[i] << 8) | padded[i + 1] for i in range(0, chars - 1, 2)] + (
        [padded[-1] << 8] if chars % 2 else []
    )


def decode_string(regs: list[int]) -> str:
    """Registers -> ASCII, trimming null and space padding."""
    out = bytearray()
    for word in regs:
        out.append((word >> 8) & 0xFF)
        out.append(word & 0xFF)
    return out.decode("ascii", errors="replace").rstrip("\0").rstrip()


def encode(point: Point, value: float | str | list[str]) -> list[int]:
    """Engineering value -> raw register words."""
    if point.type == STRING:
        return encode_string(str(value), point.chars)

    if point.kind == "enum":
        if isinstance(value, str):
            inverse = {v: k for k, v in point.enum.items()}
            raw = inverse[value]
        else:
            raw = int(value)
    elif point.kind == "bitfield":
        if isinstance(value, (list, tuple, set)):
            inverse = {v: k for k, v in point.bits.items()}
            raw = 0
            for flag in value:
                raw |= 1 << inverse[flag]
        else:
            raw = int(value)
    else:
        raw = int(round(float(value) * point.scale))

    width = point.width
    if point.type in SIGNED:
        bits = 16 * width
        lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
        raw = max(lo, min(hi, raw))
        if raw < 0:
            raw += 1 << bits
    else:
        raw = max(0, min((1 << (16 * width)) - 1, raw))

    return _to_words(raw, width)


def decode(point: Point, regs: list[int], *, check_range: bool = True):
    """Raw register words -> engineering value."""
    if len(regs) != point.width:
        raise ValueError(
            f"{point.key}: expected {point.width} register(s), got {len(regs)}"
        )
    if point.type == STRING:
        return decode_string(list(regs))

    raw = _from_words(list(regs), point.width)

    if point.kind == "enum":
        if raw not in point.enum:
            raise RangeError(f"{point.key}: undefined enum value {raw}")
        return point.enum[raw]

    if point.kind == "bitfield":
        return [name for bit, name in sorted(point.bits.items()) if raw & (1 << bit)]

    if point.type in SIGNED:
        raw = _sign(raw, point.width)

    value = raw / point.scale
    if check_range and point.range is not None:
        lo, hi = point.range
        if not (lo <= value <= hi):
            raise RangeError(
                f"{point.key}: decoded {value}{point.unit or ''} outside "
                f"declared range [{lo}, {hi}] (raw={raw})"
            )
    return value
