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
    is_int,
    is_node,
    is_section_header,
    looks_like_part_header,
    looks_like_parttype_header,
    parse_node,
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
        return [
            i
            for i, line in enumerate(lines)
            if line.startswith("*CAE*") and "GENERAL PARAMETERS FOR THE SHEET" in line
        ]

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

    def _is_section_token(self, text: str) -> bool:
        tok = text.split()[0] if text.split() else ""
        return bool(re.fullmatch(r"\*\S+\*", tok))

    def _parse_sht_entry(self, line: str, line_no: int) -> tuple[int | None, str | None]:
        """Parse one SHT tuple line.

        Expected format example:
            *SHT*   7 USB-C -1 $$$NONE
        """
        toks = line.strip().split()
        if len(toks) < 3 or toks[0] != "*SHT*":
            return None, None

        sheet_no = int(toks[1]) if is_int(toks[1]) else None
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

            tok = st.split()[0]
            if cur_name is not None and cur_start is not None:
                sections.append((cur_name, cur_start, i))
            cur_name = tok
            cur_start = i

        if cur_name is not None and cur_start is not None:
            sections.append((cur_name, cur_start, len(lines)))

        return sections

    def _parse_parttype_section(self, lines: list[str], start: int, end: int, result: ParseResult) -> None:
        i = start + 1
        while i < end:
            text = lines[i].strip()
            if not text or text.startswith("*"):
                i += 1
                continue
            if looks_like_parttype_header(text):
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
                    if looks_like_parttype_header(st):
                        break
                    i += 1

                while i < end:
                    st = lines[i].strip()
                    if not st:
                        i += 1
                        continue
                    if looks_like_parttype_header(st):
                        break
                    first = st.split()[0] if st.split() else ""
                    if first in {"GATE", "PWR", "GND", "OFF"}:
                        gate_toks = st.split()
                        pin_count = int(gate_toks[2]) if len(gate_toks) > 2 and is_int(gate_toks[2]) else 0
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
            if len(toks) >= 4 and is_node(toks[0]) and is_node(toks[1]) and is_int(toks[2]):
                node_a, node_b = toks[0], toks[1]
                coord_count = int(toks[2])
                seg_line = i + 1
                i += 1
                coords: list[tuple[int, int]] = []
                for _ in range(coord_count):
                    if i >= end:
                        break
                    ct = lines[i].strip().split()
                    if len(ct) >= 2 and is_int(ct[0]) and is_int(ct[1]):
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
        sht_entries: list[tuple[int, int | None, str | None]],
    ) -> None:
        i = start + 1
        active_idx = 0
        active_sheet_no: int | None = None
        active_sheet_name: str | None = None
        while i < end:
            text = lines[i].strip()
            if not text:
                i += 1
                continue

            if looks_like_part_header(text):
                hdr = text.split()
                refdes, part_type = hdr[0], hdr[1]
                raw_x = int(hdr[2]) if len(hdr) > 3 and is_int(hdr[2]) else None
                raw_y = int(hdr[3]) if len(hdr) > 3 and is_int(hdr[3]) else None
                raw_rotation = int(hdr[4]) if len(hdr) > 4 and is_int(hdr[4]) else None
                raw_mirror = int(hdr[5]) if len(hdr) > 5 and is_int(hdr[5]) else None

                part_line = i + 1
                while active_idx < len(sht_entries) and sht_entries[active_idx][0] <= part_line:
                    _ln, s_no, s_name = sht_entries[active_idx]
                    active_sheet_no = s_no
                    active_sheet_name = s_name
                    active_idx += 1

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
                    if st and looks_like_part_header(st):
                        break
                    # Detect REF-DES annotation offset line (numeric tokens, next line == "REF-DES")
                    if (
                        re.match(r"^-?\d+\s+-?\d+", st)
                        and i + 1 < end
                        and lines[i + 1].strip() == "REF-DES"
                    ):
                        toks = st.split()
                        if len(toks) >= 3 and is_int(toks[0]) and is_int(toks[1]) and is_int(toks[2]):
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

    def _parse_text_section(self, lines: list[str], start: int, end: int, result: ParseResult) -> None:
        i = start + 1
        while i < end:
            text = lines[i].strip()
            if not text:
                i += 1
                continue

            toks = text.split()
            if len(toks) >= 2 and is_int(toks[0]) and is_int(toks[1]):
                raw_x = int(toks[0])
                raw_y = int(toks[1])
                raw_style = int(toks[4]) if len(toks) > 4 and is_int(toks[4]) else None
                raw_size = int(toks[5]) if len(toks) > 5 and is_int(toks[5]) else None

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
            if len(toks) >= 4 and toks[0].startswith("$$DRW") and is_int(toks[2]) and is_int(toks[3]):
                base_x = int(toks[2])
                base_y = int(toks[3])
                entry_line = i + 1
                i += 1

                if i >= end:
                    continue
                st = lines[i].strip().split()
                if not st or st[0] not in {"OPEN", "CLOSED"}:
                    continue

                point_count = int(st[1]) if len(st) > 1 and is_int(st[1]) else 0
                i += 1
                pts: list[tuple[int, int]] = []
                for _ in range(point_count):
                    if i >= end:
                        break
                    pt = lines[i].strip().split()
                    if len(pt) >= 2 and is_int(pt[0]) and is_int(pt[1]):
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
            if len(toks) >= 3 and toks[0].startswith("@@@D") and is_int(toks[1]) and is_int(toks[2]):
                result.tiedots.append(TieDot(raw_x=int(toks[1]), raw_y=int(toks[2]), line=i + 1))
            i += 1

    def _parse_lines(self, lines: list[str]) -> ParseResult:
        result = ParseResult()
        sections = self._split_sections(lines)

        # Build ordered sheet context markers from *SHT* tuples.
        sht_entries: list[tuple[int, int | None, str | None]] = []
        for sec_name, start, end in sections:
            if sec_name != "*SHT*":
                continue

            s_no, s_name = self._parse_sht_entry(lines[start], start + 1)
            if s_no is not None or s_name is not None:
                sht_entries.append((start + 1, s_no, s_name))

            j = start + 1
            while j < end:
                st = lines[j].strip()
                if not st:
                    j += 1
                    continue
                s_no, s_name = self._parse_sht_entry(st, j + 1)
                if s_no is not None or s_name is not None:
                    sht_entries.append((j + 1, s_no, s_name))
                j += 1

        sht_entries.sort(key=lambda x: x[0])

        for sec_name, start, end in sections:
            if sec_name.startswith("*PADS-"):
                # File signature block already handled by _handle_file_signature().
                continue
            if sec_name == "*SIGNAL*":
                self._parse_signal_section(lines, start, end, result)
            elif sec_name == "*PARTTYPE*":
                self._parse_parttype_section(lines, start, end, result)
            elif sec_name == "*PART*":
                self._parse_part_section(lines, start, end, result, sht_entries)
            elif sec_name == "*TEXT*":
                self._parse_text_section(lines, start, end, result)
            elif sec_name == "*LINES*":
                self._parse_lines_section(lines, start, end, result)
            elif sec_name == "*TIEDOTS*":
                self._parse_tiedots_section(lines, start, end, result)
            elif sec_name == "*SHT*":
                # Sheet context already consumed into sht_entries.
                continue
            elif sec_name in ("*SCH*", "*REMARK*", "*MISC*", "*CAM*", "*FIELDS*", "*CAE*", "*CAEDECAL*", "*BUSSES*", "*END*"):
                continue
            else:
                warnings.warn(
                    f"Unhandled section header {sec_name} at line {start + 1}",
                    RuntimeWarning,
                    stacklevel=2,
                )

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
            sheet_name = f"sheet_{idx + 1}"
            out.append((sheet_name, self._parse_lines(sheet_lines)))

        return out


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
