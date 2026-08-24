"""Contiguous register read planning.

A naive collector issues one Modbus request per point. At 30 devices x ~30
points x 1 Hz that is roughly a thousand requests per second and it falls over.
Modbus reads up to 125 contiguous 16-bit registers per request, so points are
grouped into contiguous windows -- typically two or three requests per device
per poll cycle instead of thirty.
"""
from __future__ import annotations

from dataclasses import dataclass

from sim.regmap import Point

MAX_READ_REGISTERS = 125


@dataclass(frozen=True)
class ReadBlock:
    space: str
    address: int
    count: int
    points: tuple[Point, ...]

    @property
    def end(self) -> int:
        return self.address + self.count - 1


def plan_reads(
    points: list[Point],
    max_regs: int = MAX_READ_REGISTERS,
    max_gap: int = 8,
) -> list[ReadBlock]:
    """Group points into as few contiguous read windows as possible.

    `max_gap` allows bridging small holes in the address space: pulling a few
    unused registers costs bytes, whereas a second request costs a round trip.
    """
    if not points:
        return []

    blocks: list[ReadBlock] = []
    for space in ("input", "holding"):
        in_space = sorted((p for p in points if p.space == space), key=lambda p: p.offset)
        if not in_space:
            continue

        current: list[Point] = [in_space[0]]
        start = in_space[0].offset

        for point in in_space[1:]:
            span = point.last_offset - start + 1
            gap = point.offset - (current[-1].last_offset + 1)
            if span <= max_regs and gap <= max_gap:
                current.append(point)
                continue
            blocks.append(_block(space, start, current))
            current = [point]
            start = point.offset

        blocks.append(_block(space, start, current))
    return blocks


def _block(space: str, start: int, points: list[Point]) -> ReadBlock:
    end = max(p.last_offset for p in points)
    return ReadBlock(space=space, address=start, count=end - start + 1,
                     points=tuple(points))
