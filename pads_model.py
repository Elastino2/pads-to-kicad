from __future__ import annotations

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
    pins: dict[str, PinDef] = field(default_factory=lambda: {})


@dataclass
class Part:
    refdes: str
    part_type: str
    sheet_no: int
    raw_x: int | None = None
    raw_y: int | None = None
    raw_rotation: int | None = None
    raw_mirror: int | None = None
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


@dataclass
class TextAnnotation:
    sheet_no: int
    raw_x: int
    raw_y: int
    raw_rotation: int
    raw_mirror: int 
    raw_size: int
    raw_style: int
    raw_fontname: str
    text: str


@dataclass
class GraphicPolyline:
    sheet_no: int
    points: list[tuple[int, int]]


@dataclass
class TieDot:
    sheet_no: int
    raw_x: int
    raw_y: int


@dataclass
class ParseResult:
    Sheets: dict[str, ParseResult] = field(default_factory=lambda: {})
    parts: dict[str, Part] = field(default_factory=lambda: {})
    part_types: dict[str, PartTypeDef] = field(default_factory=lambda: {})
    segments: list[Segment] = field(default_factory=lambda: [])
    text_annotations: list[TextAnnotation] = field(default_factory=lambda: [])
    graphic_polylines: list[GraphicPolyline] = field(default_factory=lambda: [])
    tiedots: list[TieDot] = field(default_factory=lambda: [])
    signal_lines: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))

