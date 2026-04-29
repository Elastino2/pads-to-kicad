from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field



@dataclass
class PinDef:
    number: str
    name: str
    direction: str


@dataclass
class PartTypeDef:
    name: str
    part_class: str
    line: int
    pins: dict[str, PinDef] = field(default_factory=lambda: {})


@dataclass
class Part:
    refdes: str
    part_type: str
    line: int
    raw_x: int | None = None
    raw_y: int | None = None
    raw_rotation: int | None = None
    raw_mirror: int | None = None
    sheet_no: int | None = None
    sheet_name: str | None = None
    # REF-DES annotation offset/rotation from PADS PART block
    ref_ann_dx: int | None = None
    ref_ann_dy: int | None = None
    ref_ann_rotation: int | None = None
    properties: dict[str, str] = field(default_factory=lambda: {})


@dataclass
class Segment:
    signal: str
    node_a: str
    node_b: str
    coords: list[tuple[int, int]]
    line: int


@dataclass
class TextAnnotation:
    text: str
    raw_x: int
    raw_y: int
    line: int
    raw_size: int | None = None
    raw_style: int | None = None


@dataclass
class GraphicPolyline:
    points: list[tuple[int, int]]
    line: int


@dataclass
class TieDot:
    raw_x: int
    raw_y: int
    line: int


@dataclass
class ParseResult:
    parts: dict[str, Part] = field(default_factory=lambda: {})
    part_types: dict[str, PartTypeDef] = field(default_factory=lambda: {})
    segments: list[Segment] = field(default_factory=lambda: [])
    text_annotations: list[TextAnnotation] = field(default_factory=lambda: [])
    graphic_polylines: list[GraphicPolyline] = field(default_factory=lambda: [])
    tiedots: list[TieDot] = field(default_factory=lambda: [])
    signal_lines: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))

