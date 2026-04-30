from __future__ import annotations

import re
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from pads_model import (
    GraphicPolyline,
    ParseResult,
    Part,
    PartTypeDef,
    PinDef,
    Segment,
    TieDot,
    TextAnnotation,
)


class PadsParser:
    def __init__(self) -> None:
        self.encodings = ["utf-8", "cp949", "euc-kr", "latin-1"]
        # Captured from *PADS-LOGIC-Vxxxx.x-ENC* header for later decode policy.
        self.source_charset_hint: str | None = None

    def _read_lines(self, file_path: Path) -> list[str]:
        data = file_path.read_bytes()
        for enc in self.encodings:
            try:
                return data.decode(enc).splitlines()
            except UnicodeDecodeError:
                continue
        return data.decode("latin-1", errors="replace").splitlines()

    def _sheet_markers(self, lines: list[str]) -> list[int]:
        return [i for i, line in enumerate(lines) if line.strip().startswith("*SHT*")]

    def _handle_file_signature(self, lines: list[str]) -> None:
        """Validate the leading *PADS-LOGIC-V2007.0-CP949* style header.

        Rules:
        - split by '-'
        - quit program if tuple[0] != "PADS"
        - warn if tuple[1] != "LOGIC"
        - warn if tuple[2] != "V2007.0"
        - keep tuple[3] globally for later UTF-8 conversion phase
        """
        self.source_charset_hint = None

        first_non_empty = ""
        for line in lines:
            st = line.strip()
            if st:
                first_non_empty = st
                break

        if not first_non_empty:
            return

        first_tok = first_non_empty.split()[0]
        if not (first_tok.startswith("*") and first_tok.endswith("*")):
            return

        payload = first_tok[1:-1]
        parts = payload.split("-")
        if not parts:
            return

        if parts[0] != "PADS":
            raise SystemExit(f"Invalid PADS signature: tuple[0]={parts[0]!r}, expected 'PADS'")

        if len(parts) > 1 and parts[1] != "LOGIC":
            warnings.warn(
                f"Unexpected PADS signature tuple[1]={parts[1]!r}, expected 'LOGIC'",
                RuntimeWarning,
                stacklevel=2,
            )
        if len(parts) > 2 and parts[2] != "V2007.0":
            warnings.warn(
                f"Unexpected PADS signature tuple[2]={parts[2]!r}, expected 'V2007.0'",
                RuntimeWarning,
                stacklevel=2,
            )
        if len(parts) > 3:
            self.source_charset_hint = parts[3]

    def _extract_section_header(self, text: str) -> str | None:
        m = re.match(r"^\*[^*]+\*", text)
        return m.group(0) if m else None

    def _is_section_token(self, text: str) -> bool:
        return self._extract_section_header(text) is not None

    def _parse_sht_entry(self, line: str, line_no: int) -> tuple[int | None, str | None]:
        """Parse one SHT tuple line.

        Expected format example:
            *SHT*   7 USB-C -1 $$$NONE
        """
        toks = line.strip().split()
        if len(toks) < 3 or toks[0] != "*SHT*":
            return None, None

        sheet_no = int(toks[1]) if self.is_int(toks[1]) else None
        sheet_name = toks[2]

        if len(toks) > 3 and toks[3] != "-1":
            warnings.warn(
                f"Not implemented: *SHT* tuple[3]={toks[3]!r} at line {line_no} (expected '-1')",
                RuntimeWarning,
                stacklevel=2,
            )
        if len(toks) > 4 and toks[4] != "$$$NONE":
            warnings.warn(
                f"Not implemented: *SHT* tuple[4]={toks[4]!r} at line {line_no} (expected '$$$NONE')",
                RuntimeWarning,
                stacklevel=2,
            )

        return sheet_no, sheet_name

    def _split_sections(self, lines: list[str]) -> list[tuple[str, int, int]]:
        """Return section ranges as (header_token, start_idx, end_idx_exclusive).

        Section header is recognized by first token matching r"*\\S+*".
        """
        sections: list[tuple[str, int, int]] = []
        cur_name: str | None = None
        cur_start: int | None = None

        for i, line in enumerate(lines):
            st = line.strip()
            if not st:
                continue
            if not self._is_section_token(st):
                continue

            tok = self._extract_section_header(st)
            if tok is None:
                continue
            if cur_name is not None and cur_start is not None:
                sections.append((cur_name, cur_start, i))
            cur_name = tok
            cur_start = i

        if cur_name is not None and cur_start is not None:
            sections.append((cur_name, cur_start, len(lines)))

        return sections

    def is_int(self, token: str) -> bool:
        try:
            int(token)
            return True
        except ValueError:
            return False

    def is_node(self, token: str) -> bool:
        if token.startswith("@@@"):
            return True
        if "." in token:
            return True
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", token))

    def looks_like_part_header(self, text: str) -> bool:
        tokens = text.split()
        if len(tokens) < 6:
            return False
        if not (tokens[0][0].isalpha() or tokens[0][0] == "_"):
            return False
        if tokens[0].startswith("@@@") or "." in tokens[0]:
            return False
        if tokens[1].startswith('"') or tokens[1][0].isdigit():
            return False
        return any(self.is_int(tok) for tok in tokens[2:6])

    # PADS part-class tokens per format specification
    _PARTTYPE_CLASSES = frozenset({"RES", "CAP", "IND", "TTL", "UND", "U", "PWR", "GND"})

    def _is_part_header_line(self, text: str) -> bool:
        if self._is_section_token(text):
            return False

        toks = text.split()
        if len(toks) < 6:
            return False

        refdes, part_type = toks[0], toks[1]
        if not refdes or not (refdes[0].isalnum() or refdes[0] in {"_", "$"}):
            return False
        if refdes.startswith("@@@") or "." in refdes:
            return False
        if part_type.startswith('"'):
            return False

        return all(self.is_int(toks[i]) for i in range(2, 6))

    def _parse_parttype_section(self, lines: list[str], start: int, end: int, result: ParseResult) -> None:
        i = start + 1
        while i < end:
            text = lines[i].strip()
            if not text or text.startswith("*"):
                i += 1
                continue
            toks = text.split()
            is_parttype_header = (
                len(toks) >= 3
                and not self._is_section_token(text)
                and not toks[0].startswith("@@@")
                and (toks[0][0].isalpha() or toks[0][0] in ("_", "$"))
                and toks[1] in self._PARTTYPE_CLASSES
            )
            if is_parttype_header:
                hdr = text.split()
                type_name = hdr[0]
                part_class = hdr[1] if len(hdr) > 1 else "UND"
                ptd = PartTypeDef(name=type_name, part_class=part_class, line=i + 1)
                i += 1

                while i < end:
                    st = lines[i].strip()
                    if st.startswith("TIMESTAMP"):
                        i += 1
                        break
                    st_toks = st.split()
                    next_is_parttype_header = (
                        len(st_toks) >= 3
                        and not self._is_section_token(st)
                        and not st_toks[0].startswith("@@@")
                        and (st_toks[0][0].isalpha() or st_toks[0][0] in ("_", "$"))
                        and st_toks[1] in self._PARTTYPE_CLASSES
                    )
                    if next_is_parttype_header:
                        break
                    i += 1

                while i < end:
                    st = lines[i].strip()
                    if not st:
                        i += 1
                        continue
                    st_toks = st.split()
                    next_is_parttype_header = (
                        len(st_toks) >= 3
                        and not self._is_section_token(st)
                        and not st_toks[0].startswith("@@@")
                        and (st_toks[0][0].isalpha() or st_toks[0][0] in ("_", "$"))
                        and st_toks[1] in self._PARTTYPE_CLASSES
                    )
                    if next_is_parttype_header:
                        break
                    first = st.split()[0] if st.split() else ""
                    if first in {"GATE", "PWR", "GND", "OFF"}:
                        gate_toks = st.split()
                        pin_count = int(gate_toks[2]) if len(gate_toks) > 2 and self.is_int(gate_toks[2]) else 0
                        i += 1
                        if i < len(lines):
                            i += 1
                        for _ in range(pin_count):
                            if i >= len(lines):
                                break
                            pk = lines[i].strip().split()
                            if len(pk) >= 2:
                                pnum = pk[0]
                                pdir = pk[2] if len(pk) > 2 else "U"
                                pname = " ".join(pk[3:]) if len(pk) > 3 else pnum
                                ptd.pins[pnum] = PinDef(number=pnum, name=pname, direction=pdir)
                            i += 1
                        continue
                    i += 1

                result.part_types[type_name] = ptd
                continue
            i += 1

    def _parse_signal_section(self, lines: list[str], start: int, end: int, result: ParseResult) -> None:
        header = lines[start].strip()
        stoks = header.split()
        signal_name = stoks[1] if len(stoks) > 1 else "UNKNOWN"
        result.signal_lines[signal_name].append(start + 1)

        i = start + 1
        while i < end:
            st = lines[i].strip()
            if not st:
                i += 1
                continue

            toks = st.split()
            if len(toks) >= 4 and self.is_node(toks[0]) and self.is_node(toks[1]) and self.is_int(toks[2]):
                node_a, node_b = toks[0], toks[1]
                coord_count = int(toks[2])
                seg_line = i + 1
                i += 1
                coords: list[tuple[int, int]] = []
                for _ in range(coord_count):
                    if i >= end:
                        break
                    ct = lines[i].strip().split()
                    if len(ct) >= 2 and self.is_int(ct[0]) and self.is_int(ct[1]):
                        coords.append((int(ct[0]), int(ct[1])))
                        i += 1
                    else:
                        break
                result.segments.append(
                    Segment(signal=signal_name, node_a=node_a, node_b=node_b, coords=coords, line=seg_line)
                )
                continue

            i += 1

    def _parse_part_section(
        self,
        lines: list[str],
        start: int,
        end: int,
        result: ParseResult,
        active_sheet_no: int | None,
        active_sheet_name: str | None,
    ) -> None:
        i = start + 1
        while i < end:
            text = lines[i].strip()
            if not text:
                i += 1
                continue

            if self._is_part_header_line(text):
                hdr = text.split()
                refdes, part_type = hdr[0], hdr[1]
                raw_x = int(hdr[2]) if len(hdr) > 3 and self.is_int(hdr[2]) else None
                raw_y = int(hdr[3]) if len(hdr) > 3 and self.is_int(hdr[3]) else None
                raw_rotation = int(hdr[4]) if len(hdr) > 4 and self.is_int(hdr[4]) else None
                raw_mirror = int(hdr[5]) if len(hdr) > 5 and self.is_int(hdr[5]) else None

                part_line = i + 1
                part = Part(
                    refdes=refdes,
                    part_type=part_type,
                    line=part_line,
                    raw_x=raw_x,
                    raw_y=raw_y,
                    raw_rotation=raw_rotation,
                    raw_mirror=raw_mirror,
                    sheet_no=active_sheet_no,
                    sheet_name=active_sheet_name,
                )
                i += 1
                while i < end:
                    st = lines[i].strip()
                    if st and self._is_part_header_line(st):
                        break
                    # Detect REF-DES annotation offset line (numeric tokens, next line == "REF-DES")
                    if (
                        re.match(r"^-?\d+\s+-?\d+", st)
                        and i + 1 < end
                        and lines[i + 1].strip() == "REF-DES"
                    ):
                        toks = st.split()
                        if len(toks) >= 3 and self.is_int(toks[0]) and self.is_int(toks[1]) and self.is_int(toks[2]):
                            part.ref_ann_dx = int(toks[0])
                            part.ref_ann_dy = int(toks[1])
                            part.ref_ann_rotation = int(toks[2])
                    prop_m = re.match(r'^"([^"]+)"\s+(.*)$', st)
                    if prop_m:
                        key, value = prop_m.groups()
                        part.properties[key] = value.strip().strip('"')
                    i += 1
                result.parts[refdes] = part
                continue

            i += 1

    def _dispatch_section(
        self,
        sec_name: str,
        lines: list[str],
        start: int,
        end: int,
        result: ParseResult,
        sheet_no: int | None,
        sheet_name: str | None,
    ) -> None:
        if sec_name.startswith("*PADS-"):
            return
        if sec_name == "*SIGNAL*":
            self._parse_signal_section(lines, start, end, result)
            return
        if sec_name == "*PARTTYPE*":
            self._parse_parttype_section(lines, start, end, result)
            return
        if sec_name == "*PART*":
            self._parse_part_section(lines, start, end, result, sheet_no, sheet_name)
            return
        if sec_name == "*TEXT*":
            self._parse_text_section(lines, start, end, result)
            return
        if sec_name == "*LINES*":
            self._parse_lines_section(lines, start, end, result)
            return
        if sec_name == "*TIEDOTS*":
            self._parse_tiedots_section(lines, start, end, result)
            return
        if sec_name in (
            "*SHT*",
            "*SCH*",
            "*REMARK*",
            "*MISC*",
            "*CAM*",
            "*CONNECTION*",
            "*FIELDS*",
            "*CAE*",
            "*CAEDECAL*",
            "*BUSSES*",
            "*OFFPAGE REFS*",
            "*NETNAMES*",
            "*END*",
        ):
            return

        loc = f"line {start + 1}"
        if sheet_no is not None or sheet_name is not None:
            loc = f"sheet {sheet_name or '?'} ({sheet_no if sheet_no is not None else '?'}) line {start + 1}"
        warnings.warn(
            f"Unhandled section header {sec_name} at {loc}",
            RuntimeWarning,
            stacklevel=2,
        )

    def _parse_sht_block(
        self,
        lines: list[str],
        sections: list[tuple[str, int, int]],
        block_start: int,
        block_end: int,
        result: ParseResult,
    ) -> None:
        sheet_no, sheet_name = self._parse_sht_entry(lines[block_start], block_start + 1)

        for sec_name, start, end in sections:
            if start <= block_start or start >= block_end:
                continue
            self._dispatch_section(sec_name, lines, start, min(end, block_end), result, sheet_no, sheet_name)

    def _parse_text_section(self, lines: list[str], start: int, end: int, result: ParseResult) -> None:
        i = start + 1
        while i < end:
            text = lines[i].strip()
            if not text:
                i += 1
                continue
            # raw_x raw_y raw_rotation raw_mirror raw_style raw_size unknown "font name"
            # Split carefully to handle quoted font name at the end
            match = re.match(r'^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+"([^"]*)"', text)
            if not match:
                i += 1
                continue
            toks = list(match.groups())
            if len(toks) >= 2 and self.is_int(toks[0]) and self.is_int(toks[1]):
                raw_x = int(toks[0])
                raw_y = int(toks[1])
                if(int(toks[6])!=0):
                    warnings.warn(
                        f"Not implemented: text annotation with nonzero rotation/mirror at line {i + 1}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                raw_style = int(toks[4]) if len(toks) > 4 and self.is_int(toks[4]) else None
                raw_size = int(toks[5]) if len(toks) > 5 and self.is_int(toks[5]) else None

                text_line = ""
                if i + 1 < end:
                    nxt = lines[i + 1].rstrip("\r\n")
                    if nxt.strip() and not self._is_section_token(nxt.strip()):
                        text_line = nxt.strip()
                        i += 1

                result.text_annotations.append(
                    TextAnnotation(
                        text=text_line,
                        raw_x=raw_x,
                        raw_y=raw_y,
                        line=i + 1,
                        raw_size=raw_size,
                        raw_style=raw_style,
                    )
                )

            i += 1

    def _parse_lines_section(self, lines: list[str], start: int, end: int, result: ParseResult) -> None:
        i = start + 1
        while i < end:
            text = lines[i].strip()
            if not text:
                i += 1
                continue

            toks = text.split()
            if len(toks) >= 4 and toks[0].startswith("$$DRW") and self.is_int(toks[2]) and self.is_int(toks[3]):
                base_x = int(toks[2])
                base_y = int(toks[3])
                entry_line = i + 1
                i += 1

                if i >= end:
                    continue
                st = lines[i].strip().split()
                if not st or st[0] not in {"OPEN", "CLOSED"}:
                    continue

                point_count = int(st[1]) if len(st) > 1 and self.is_int(st[1]) else 0
                i += 1
                pts: list[tuple[int, int]] = []
                for _ in range(point_count):
                    if i >= end:
                        break
                    pt = lines[i].strip().split()
                    if len(pt) >= 2 and self.is_int(pt[0]) and self.is_int(pt[1]):
                        pts.append((base_x + int(pt[0]), base_y + int(pt[1])))
                        i += 1
                        continue
                    break

                if len(pts) >= 2:
                    result.graphic_polylines.append(GraphicPolyline(points=pts, line=entry_line))
                continue

            i += 1

    def _parse_tiedots_section(self, lines: list[str], start: int, end: int, result: ParseResult) -> None:
        i = start + 1
        while i < end:
            text = lines[i].strip()
            if not text:
                i += 1
                continue

            toks = text.split()
            if toks[0].startswith("@@@D") and self.is_int(toks[1]) and self.is_int(toks[2]):
                result.tiedots.append(TieDot(raw_x=int(toks[1]), raw_y=int(toks[2]), line=i + 1))
            i += 1

    def _parse_lines(self, lines: list[str]) -> ParseResult:
        result = ParseResult()
        sections = self._split_sections(lines)

        sht_sections = [(start, end) for sec_name, start, end in sections if sec_name == "*SHT*"]
        if not sht_sections:
            for sec_name, start, end in sections:
                self._dispatch_section(sec_name, lines, start, end, result, None, None)
            return result

        for idx, (sht_start, _sht_end) in enumerate(sht_sections):
            next_sht_start = sht_sections[idx + 1][0] if idx + 1 < len(sht_sections) else len(lines)
            self._parse_sht_block(lines, sections, sht_start, next_sht_start, result)

        return result

    def parse(self, file_path: Path) -> ParseResult:
        lines = self._read_lines(file_path)
        self._handle_file_signature(lines)
        return self._parse_lines(lines)

    def parse_sheet_results(self, file_path: Path) -> list[tuple[str, ParseResult]]:
        """Parse each original PADS sheet block independently.

        Returns list of (sheet_name, ParseResult), preserving source order.
        """
        lines = self._read_lines(file_path)
        self._handle_file_signature(lines)
        markers = self._sheet_markers(lines)

        if not markers:
            return [("sheet_1", self._parse_lines(lines))]

        boundaries = markers + [len(lines)]
        out: list[tuple[str, ParseResult]] = []

        for idx in range(len(markers)):
            start = boundaries[idx]
            end = boundaries[idx + 1]
            sheet_lines = lines[start:end]
            sheet_no, sheet_title = self._parse_sht_entry(lines[start], start + 1)
            if sheet_title:
                sheet_name = sheet_title
            elif sheet_no is not None:
                sheet_name = f"sheet_{sheet_no}"
            else:
                sheet_name = f"sheet_{idx + 1}"
            out.append((sheet_name, self._parse_lines(sheet_lines)))

        return out

def parse_node(node: str) -> tuple[str | None, str | None]:
    """Parse a node reference into component reference and pin number.
    
    Examples:
        "N$5" → (None, None)
        "U5.2" → ("U5", "2")
        "GND" → ("GND", None)
    """
    if node.startswith("@@@"):
        return None, None
    if "." in node:
        ref, pin = node.split(".", 1)
        return ref, pin
    return node, None

def build_connectivity(result: ParseResult) -> dict[str, Any]:
    signal_to_refs: dict[str, set[str]] = defaultdict(set)
    ref_pin_to_signals: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    component_signals: dict[str, set[str]] = defaultdict(set)
    component_lines: dict[str, list[int]] = defaultdict(list)
    node_adjacency: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    for seg in result.segments:
        na, nb = seg.node_a, seg.node_b
        node_adjacency[na][nb].add(seg.signal)
        node_adjacency[nb][na].add(seg.signal)
        for node in (na, nb):
            ref, pin = parse_node(node)
            if ref is None:
                continue
            signal_to_refs[seg.signal].add(ref)
            component_signals[ref].add(seg.signal)
            if pin is not None:
                ref_pin_to_signals[ref][pin].add(seg.signal)
                component_lines[ref].append(seg.line)

    return {
        "signal_to_refs": {k: sorted(v) for k, v in signal_to_refs.items()},
        "ref_pin_to_signals": {
            ref: {pin: sorted(sigs) for pin, sigs in pin_map.items()}
            for ref, pin_map in ref_pin_to_signals.items()
        },
        "component_signals": {k: sorted(v) for k, v in component_signals.items()},
        "component_lines": {k: sorted(set(v)) for k, v in component_lines.items()},
        "node_adjacency": {
            k: {kk: sorted(vv) for kk, vv in v.items()}
            for k, v in node_adjacency.items()
        },
    }


def extract_target_report(
    targets: list[str] | None,
    aliases: dict[str, str] | None,
    result: ParseResult,
    connectivity: dict[str, Any],
) -> dict[str, Any]:
    targets = targets or []
    canonical_targets = [aliases.get(t, t) if aliases else t for t in targets]
    ref_pin_to_signals: dict[str, dict[str, list[str]]] = connectivity["ref_pin_to_signals"]
    component_lines: dict[str, list[int]] = connectivity["component_lines"]
    signal_to_refs: dict[str, list[str]] = connectivity["signal_to_refs"]

    target_info: dict[str, Any] = {}
    for original, canonical in zip(targets, canonical_targets):
        part = result.parts.get(canonical)
        ptd = result.part_types.get(part.part_type) if part else None
        raw_pin_sigs = ref_pin_to_signals.get(canonical, {})
        enriched_pins: dict[str, Any] = {}
        for pnum, sigs in raw_pin_sigs.items():
            pname = ptd.pins[pnum].name if (ptd and pnum in ptd.pins) else None
            enriched_pins[pnum] = {"signals": sigs, "pin_name": pname}

        target_info[original] = {
            "canonical_refdes": canonical,
            "found": part is not None,
            "line": part.line if part else None,
            "part_type": part.part_type if part else None,
            "part_class": ptd.part_class if ptd else None,
            "properties": {
                k: v
                for k, v in (part.properties.items() if part is not None else cast(list[tuple[str, str]], []))
                if k in {"Manufacturer_Name", "Manufacturer_Part_Number", "Description", "Datasheet", "SPEC"}
            },
            "pin_to_signals": enriched_pins,
            "connection_lines": component_lines.get(canonical, []),
        }

    raw_interconnections: list[dict[str, Any]] = [
        {"signal": sig, "targets": sorted(set(canonical_targets) & set(refs))}
        for sig, refs in signal_to_refs.items()
        if len(set(canonical_targets) & set(refs)) >= 2
    ]
    interconnections = sorted(raw_interconnections, key=lambda x: x["signal"])

    return {
        "targets_requested": targets,
        "targets_canonical": canonical_targets,
        "target_details": target_info,
        "direct_interconnections": interconnections,
    }


def build_line_evidence(result: ParseResult, focus_signals: list[str]) -> dict[str, Any]:
    return {
        sig: {
            "signal_header_lines": result.signal_lines.get(sig, []),
            "segments": [
                {"line": seg.line, "node_a": seg.node_a, "node_b": seg.node_b}
                for seg in result.segments
                if seg.signal == sig
            ],
        }
        for sig in focus_signals
    }
