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
    timestamp: int
    caedecal_name: str | None = None
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
    sheet_no: int
    signal: str
    node_a: str
    node_b: str
    coords: list[tuple[int, int]]
    node_a_ref: str | None = None
    node_a_pin: str | None = None
    node_b_ref: str | None = None
    node_b_pin: str | None = None


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
class CaeDecalPrimitive:
    kind: str
    point_count: int
    width: int | None = None
    style: int | None = None
    points: list[tuple[int, int]] = field(default_factory=lambda: [])


@dataclass
class CaeDecalPinMap:
    raw_index: int
    raw_x: int
    raw_y: int
    raw_rotation: int | None = None
    raw_side: int | None = None
    symbol: str | None = None
    pin_number_hint: str | None = None
    raw_line_t: str | None = None
    raw_line_p: str | None = None


@dataclass
class CaeDecalDef:
    name: str
    timestamp: int
    raw_x: int | None = None
    raw_y: int | None = None
    raw_width: int | None = None
    raw_height: int | None = None
    raw_width2: int | None = None
    raw_height2: int | None = None
    number_of_text: int | None = None
    number_of_drawing_nodes: int | None = None
    header_unknown1: int | None = None
    number_of_pinmap: int | None = None
    count_of_nodes: int | None = None
    header_unknown3: int | None = None
    header_tokens: list[str] = field(default_factory=lambda: [])
    primitives: list[CaeDecalPrimitive] = field(default_factory=lambda: [])
    pinmaps: list[CaeDecalPinMap] = field(default_factory=lambda: [])
    raw_lines: list[str] = field(default_factory=lambda: [])


@dataclass
class ParseResult:
    Sheets: dict[str, ParseResult] = field(default_factory=lambda: {})
    parts: dict[str, Part] = field(default_factory=lambda: {})
    part_types: dict[str, PartTypeDef] = field(default_factory=lambda: {})
    caedecals: dict[str, CaeDecalDef] = field(default_factory=lambda: {})
    text_annotations: list[TextAnnotation] = field(default_factory=lambda: [])
    graphic_polylines: list[GraphicPolyline] = field(default_factory=lambda: [])
    tiedots: list[TieDot] = field(default_factory=lambda: [])
    signal_lines: dict[str, list[Segment]] = field(default_factory=lambda: defaultdict(list))

