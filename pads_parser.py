from __future__ import annotations

import re
import warnings
from pathlib import Path

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

    def _parse_quoted_property_line(self, text: str) -> tuple[str, str] | None:
        """Parse a quoted property line from *PART* body.

        Supports both forms:
        - "KEY" VALUE
        - "KEY"              (empty value)
        """
        m = re.match(r'^"([^"]+)"(?:\s+(.*))?$', text)
        if not m:
            return None

        key = m.group(1)
        raw_value = m.group(2)
        if raw_value is None:
            return key, ""

        value = raw_value.strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        return key, value

    def _parse_parttype_timestamp(self, text: str) -> int:
        """Convert `TIMESTAMP yyyy.mm.dd.hh.mm.ss` to integer yyyymmddhhmmss.

        Returns 0 when timestamp token is missing or malformed.
        """
        toks = text.split(maxsplit=1)
        if len(toks) < 2:
            return 0

        raw = toks[1].strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) != 14 or not digits.isdigit():
            return 0
        return int(digits)

    
    
    
    # ID CLASS unknown1 unknown2 unknown3 unknown4
    # TIMESTAMP yyyy.mm.dd.hh.mm.ss
    # GATE number_of_typename pin_count unknown2 # warn if unknown2 != 0
    # or
    # [GND|PWF|OFF] pin_count unknown2 # warn if unknown2 != 0 
    # EXAMPLE:
    # R_7 RES  1   0   0     0
    # TIMESTAMP 1970.01.01.00.00.00
    # GATE 1 2 0
    # RE
    # 1 0 L 1
    # 2 0 L 2
    def _parse_parttype_section(self, lines: list[str], start: int, end: int, result: ParseResult) -> ParseResult:
        """Parse *PARTTYPE* entries split by blank-line separators."""

        # PARTTYPE records are separated by blank lines in the source file.
        blocks: list[list[str]] = []
        cur: list[str] = []
        for i in range(start + 1, end):
            st = lines[i].strip()
            if not st:
                if cur:
                    blocks.append(cur)
                    cur = []
                continue
            if self._is_section_token(st):
                break
            cur.append(st)
        if cur:
            blocks.append(cur)

        for block in blocks:
            hdr_toks = block[0].split()
            if not hdr_toks:
                continue

            type_name = hdr_toks[0]
            part_class = hdr_toks[1] if len(hdr_toks) > 1 else "UND"
            timestamp = 0

            # Pin payload typically starts after TIMESTAMP; if missing, parse from line 2.
            payload_start = 1
            for bi in range(1, len(block)):
                if block[bi].startswith("TIMESTAMP"):
                    timestamp = self._parse_parttype_timestamp(block[bi])
                    payload_start = bi + 1
                    break
            ptd = PartTypeDef(name=type_name, part_class=part_class, timestamp=timestamp)

            bi = payload_start
            while bi < len(block):
                st_toks = block[bi].split()
                first = st_toks[0] if st_toks else ""

                if first in {"GATE", "PWR", "GND", "OFF"}:
                    if first == "GATE":
                        pin_count = int(st_toks[2]) if len(st_toks) > 2 and self.is_int(st_toks[2]) else 0
                    else:
                        pin_count = int(st_toks[1]) if len(st_toks) > 1 and self.is_int(st_toks[1]) else 0

                    bi += 1
                    # Optional gate label row (ex: RE) before pin rows.
                    if bi < len(block):
                        label_toks = block[bi].split()
                        if not label_toks or not self.is_int(label_toks[0]):
                            bi += 1

                    for _ in range(pin_count):
                        if bi >= len(block):
                            break
                        pk = block[bi].split()
                        if len(pk) >= 1 and self.is_int(pk[0]):
                            pnum = pk[0]
                            pdir = pk[2] if len(pk) > 2 else "U"
                            pname = " ".join(pk[3:]) if len(pk) > 3 else pnum
                            ptd.pins[pnum] = PinDef(number=pnum, name=pname, direction=pdir)
                        bi += 1
                    continue
                else:
                    warnings.warn(
                        f"Unrecognized line in PARTTYPE body for type {type_name!r}: {block[bi]!r}",
                        RuntimeWarning,
                        stacklevel=2,
                    )

                bi += 1

            result.part_types[type_name] = ptd
        return result

    def _parse_signal_header(self, header: str) -> tuple[str, int, int]:
        """Parse `*SIGNAL* <name> <unknown> <unknown2>` header tuple."""
        stoks = header.split()
        signal_name = stoks[1] if len(stoks) > 1 else "UNKNOWN"
        unknown = int(stoks[2]) if len(stoks) > 2 and self.is_int(stoks[2]) else 0
        unknown2 = int(stoks[3]) if len(stoks) > 3 and self.is_int(stoks[3]) else 0
        return signal_name, unknown, unknown2

    def _try_parse_signal_segment_header(self, text: str) -> tuple[str, str, int] | None:
        """Parse `node_a node_b coord_count unknown3` row from SIGNAL body."""
        toks = text.split()
        if len(toks) < 4:
            return None
        if not (self.is_node(toks[0]) and self.is_node(toks[1]) and self.is_int(toks[2])):
            return None
        if toks[3] != "0":
            warnings.warn(
                f"Not implemented: SIGNAL segment header with nonzero unknown3 field: {text!r}",
                RuntimeWarning,
                stacklevel=2,
            )
        return toks[0], toks[1], int(toks[2])

    def _parse_signal_coords(
        self,
        lines: list[str],
        start: int,
        end: int,
        coord_count: int,
    ) -> tuple[list[tuple[int, int]], int]:
        """Read `coord_count` coordinate lines starting at `start`."""
        i = start
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
        return coords, i

    def _parse_signal_section(self, sheet_no: int, lines: list[str], start: int, end: int, result: ParseResult) -> ParseResult:
        # *SIGNAL* SIGNAL_NAME unknown1 unknown2
        # node_a node_b coord_count unknown3
        # x1 y1
        # ...
        #
        # Example:
        # *SIGNAL* N39362253 0 0
        #  R5.2         U2.6         4 0
        #  12400  13900 
        #  12400  13600 
        #  12700  13600 
        #  12700  14300 
        i = start
        while i < end:
            while i < end and not lines[i].strip():
                i += 1
            if i >= end:
                break

            header = lines[i].strip()
            if self._is_section_token(header) and not header.startswith("*SIGNAL*"):
                break
            if not header.startswith("*SIGNAL*"):
                warnings.warn(
                    f"Expected SIGNAL header at line {i + 1}, got: {header!r}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                i += 1
                continue

            signal_name, unknown, unknown2 = self._parse_signal_header(header)
            result.signal_lines[signal_name].append(i + 1)

            if unknown != 0 or unknown2 != 0:
                warnings.warn(
                    (
                        f"Not implemented: *SIGNAL* header nonzero unknown fields for {signal_name!r} "
                        f"at line {i + 1} (unknown={unknown}, unknown2={unknown2})"
                    ),
                    RuntimeWarning
                )

            i += 1

            while i < end:
                st = lines[i].strip()
                if not st:
                    # Signal records are separated by blank lines.
                    break

                seg_hdr = self._try_parse_signal_segment_header(st)
                if seg_hdr is not None:
                    node_a, node_b, coord_count = seg_hdr
                    i += 1
                    coords, i = self._parse_signal_coords(lines, i, end, coord_count)
                    result.segments.append(
                        Segment(sheet_no=sheet_no, signal=signal_name, node_a=node_a, node_b=node_b, coords=coords)
                    )
                    continue

                warnings.warn(
                    f"Unrecognized line in SIGNAL section for signal {signal_name!r}: {st!r}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                i += 1

            while i < end and not lines[i].strip():
                i += 1
        return result

    def _parse_part_section(
        self,
        sheet_no: int,
        lines: list[str],
        start: int,
        end: int,
        result: ParseResult,
    ) -> ParseResult:
        i = start + 1
        while i < end:
            text = lines[i].strip()
            if not text:
                i += 1
                continue

            if self._is_part_header_line(text):
                hdr = text.split()
                refdes, part_type = hdr[0], hdr[1]
                raw_x = int(hdr[2]) if len(hdr) > 2 and self.is_int(hdr[2]) else None
                raw_y = int(hdr[3]) if len(hdr) > 3 and self.is_int(hdr[3]) else None
                raw_rotation = int(hdr[4]) if len(hdr) > 4 and self.is_int(hdr[4]) else None
                raw_mirror = int(hdr[5]) if len(hdr) > 5 and self.is_int(hdr[5]) else None

                part = Part(
                    refdes=refdes,
                    part_type=part_type,
                    raw_x=raw_x,
                    raw_y=raw_y,
                    raw_rotation=raw_rotation,
                    raw_mirror=raw_mirror,
                    sheet_no=sheet_no
                )
                i += 1
                while i < end:
                    st = lines[i].strip()
                    if st and self._is_section_token(st):
                        break
                    if st and self._is_part_header_line(st):
                        break
                    # Detect REF-DES annotation offset line (numeric tokens, next line == "REF-DES")
                    if (
                        re.match(r"^-?\d+\s+-?\d+", st)
                        and i + 1 < end
                        and lines[i + 1].strip().upper() == "REF-DES"
                    ):
                        toks = st.split()
                        if len(toks) >= 3 and self.is_int(toks[0]) and self.is_int(toks[1]) and self.is_int(toks[2]):
                            part.ref_ann_dx = int(toks[0])
                            part.ref_ann_dy = int(toks[1])
                            part.ref_ann_rotation = int(toks[2])
                    prop = self._parse_quoted_property_line(st)
                    if prop:
                        key, value = prop
                        part.properties[key] = value
                    i += 1
                result.parts[refdes] = part
                continue

            i += 1
        return result

    def _dispatch_section(
        self,
        sec_name: str,
        lines: list[str],
        start: int,
        end: int,
        result: ParseResult,
        sheet_no: int
    ) -> ParseResult:
        if sec_name.startswith("*PADS-"):
            return result
        if sec_name == "*SIGNAL*":
            return self._parse_signal_section(sheet_no, lines, start, end, result)
        if sec_name == "*PARTTYPE*":
            return self._parse_parttype_section(lines, start, end, result)
        if sec_name == "*PART*":
            return self._parse_part_section(sheet_no, lines, start, end, result )
        if sec_name == "*TEXT*":
            return self._parse_text_section(sheet_no, lines, start, end, result )
        if sec_name == "*LINES*":
            return self._parse_lines_section(sheet_no, lines, start, end, result)
        if sec_name == "*TIEDOTS*":
            return self._parse_tiedots_section(sheet_no, lines, start, end, result)
        if sec_name in (
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
            return result

        loc = f"line {start + 1}"
        warnings.warn(
            f"Unhandled section header {sec_name} at {loc}",
            RuntimeWarning,
            stacklevel=2,
        )
        return result

    def _parse_text_section(self, sheet_no: int, lines: list[str], start: int, end: int, result: ParseResult) -> ParseResult:
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
            raw_x = int(toks[0])
            raw_y = int(toks[1])
            if(int(toks[6])!=0):
                warnings.warn(
                    f"Not implemented: text annotation with nonzero rotation/mirror at line {i + 1}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            raw_rotation = int(toks[2])
            raw_mirror = int(toks[3])
            raw_style = int(toks[4])
            raw_size = int(toks[5])
            raw_fontname = toks[7]

            text_line = ""
            if i + 1 < end:
                nxt = lines[i + 1].rstrip("\r\n")
                if nxt.strip() and not self._is_section_token(nxt.strip()):
                    text_line = nxt.strip()
                    i += 1

            result.text_annotations.append(
                TextAnnotation(
                    sheet_no=sheet_no,
                    raw_x=raw_x,
                    raw_y=raw_y,
                    raw_rotation=raw_rotation,
                    raw_mirror=raw_mirror,
                    raw_style=raw_style,
                    raw_size=raw_size,
                    raw_fontname=raw_fontname,
                    text=text_line
                )
            )

            i += 1
        return result

    def _parse_lines_section(self, sheet_no: int, lines: list[str], start: int, end: int, result: ParseResult) -> ParseResult:
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
                i += 1

                if i >= end:
                    continue
                st = lines[i].strip().split()

                point_count = int(st[1])
                i += 1
                pts: list[tuple[int, int]] = []
                for _ in range(point_count):
                    if i >= end:
                        break
                    pt = lines[i].strip().split()
                    pts.append((base_x + int(pt[0]), base_y + int(pt[1])))
                    i += 1
                    continue

                result.graphic_polylines.append(GraphicPolyline(sheet_no=sheet_no, points=pts))
                continue

            i += 1
        return result

    def _parse_tiedots_section(self, sheet_no: int, lines: list[str], start: int, end: int, result: ParseResult) -> ParseResult:
        i = start + 1
        while i < end:
            text = lines[i].strip()
            if not text:
                i += 1
                continue

            toks = text.split()
            if toks[0].startswith("@@@D") and self.is_int(toks[1]) and self.is_int(toks[2]):
                result.tiedots.append(TieDot(sheet_no=sheet_no, raw_x=int(toks[1]), raw_y=int(toks[2])))
            i += 1
        return result

    def _parse_sheets(self, file_path: Path) -> ParseResult:
        """Parse PADS source file and return a sheet-centric ParseResult."""
        lines = self._read_lines(file_path)
        self._handle_file_signature(lines)

        markers = self._sheet_markers(lines)
        # raise runtime error if any *SHT* is found
        if len(markers) == 0:
            raise RuntimeError("No *SHT* sheet markers found in the input file, cannot proceed with parsing.")

        boundaries = markers + [len(lines)]
        out = ParseResult()

        for idx in range(len(markers)):
            start = boundaries[idx]
            end = boundaries[idx + 1]
            sheet_lines = lines[start:end]
            _sht_toks = lines[start].strip().split()
            if _sht_toks[0] != "*SHT*" or len(_sht_toks) != 5:
                raise RuntimeError(f"Invalid *SHT* entry at line {start + 1}: {lines[start]!r}")
            sheet_no = int(_sht_toks[1])
            sheet_title = _sht_toks[2]
            if _sht_toks[3] != "-1":
                warnings.warn(
                    f"Not implemented: *SHT* tuple[3]={_sht_toks[3]!r} at line {start + 1} (expected '-1')",
                    RuntimeWarning,
                    stacklevel=2,
                )
            if _sht_toks[4] != "$$$NONE":
                warnings.warn(
                    f"Not implemented: *SHT* tuple[4]={_sht_toks[4]!r} at line {start + 1} (expected '$$$NONE')",
                    RuntimeWarning,
                    stacklevel=2,
                )

            sheet_result = ParseResult()
            sheet_sections = self._split_sections(sheet_lines)
            for sec_name, sec_start, sec_end in sheet_sections:
                if sec_start == 0:
                    continue
                abs_start = start + sec_start
                abs_end = start + sec_end
                self._dispatch_section(sec_name, lines, abs_start, abs_end, sheet_result, sheet_no)
            out.Sheets[sheet_title] = sheet_result
            out.parts.update(sheet_result.parts)
            out.part_types.update(sheet_result.part_types)
            out.segments.extend(sheet_result.segments)
            out.text_annotations.extend(sheet_result.text_annotations)
            out.graphic_polylines.extend(sheet_result.graphic_polylines)
            out.tiedots.extend(sheet_result.tiedots)
            for signal_name, line_nums in sheet_result.signal_lines.items():
                out.signal_lines[signal_name].extend(line_nums)

        return out

    def parse(self, file_path: Path) -> ParseResult:
        """Parse PADS source file and return sheet-centric ParseResult."""
        return self._parse_sheets(file_path)

# TODO : remove it after refactoring pads_pipeline to not depend on this heuristic
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
