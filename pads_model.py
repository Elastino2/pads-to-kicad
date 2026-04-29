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


def is_int(token: str) -> bool:
    return bool(re.fullmatch(r"[-+]?\d+", token))


def is_node(token: str) -> bool:
    if token.startswith("@@@"):
        return True
    if "." in token:
        return True
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", token))


def is_section_header(text: str) -> bool:
    return bool(re.fullmatch(r"\*[A-Z0-9_]+\*(?:\s+.*)?", text))


def looks_like_part_header(text: str) -> bool:
    tokens = text.split()
    if len(tokens) < 6:
        return False
    if not (tokens[0][0].isalpha() or tokens[0][0] == "_"):
        return False
    if tokens[0].startswith("@@@") or "." in tokens[0]:
        return False
    if tokens[1].startswith('"') or tokens[1][0].isdigit():
        return False
    return any(is_int(tok) for tok in tokens[2:6])


def looks_like_parttype_header(text: str) -> bool:
    tokens = text.split()
    if len(tokens) < 3:
        return False
    if not (tokens[0][0].isalpha() or tokens[0][0] in ("_", "$")):
        return False
    if tokens[0].startswith("@@@"):
        return False
    return tokens[1] in {"RES", "CAP", "IND", "TTL", "UND", "U", "PWR", "GND"}


def parse_resistance_ohm(spec: str) -> float | None:
    m = re.search(r"([\d.]+)\s*([KkMmRr]?)\s*(?:OHM|ohm)?", spec)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2).upper()
    if unit == "K":
        val *= 1_000
    elif unit == "M":
        val *= 1_000_000
    return val


def parse_node(node: str) -> tuple[str | None, str | None]:
    if node.startswith("@@@"):
        return None, None
    if "." in node:
        ref, pin = node.split(".", 1)
        return ref, pin
    return node, None
