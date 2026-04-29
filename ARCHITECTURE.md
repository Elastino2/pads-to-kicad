# PADS to KiCad Converter - Architecture Guide

## Module Architecture

### 1. **pads_model.py** — Data Structures & Utilities

**Purpose:** Defines all core data classes and utility functions for PADS parsing.

**Key Data Classes:**
- `Part` — Represents a schematic component (refdes, part_type, coordinates, properties)
- `PartTypeDef` — Part type definition (name, pins, package info)
- `Segment` — Electrical connection segment (from_node, to_node, signal_name)
- `TieDot` — Net tie indicator (node reference)
- `TextAnnotation` — Text labels in schematic (text, location, properties)
- `ParseResult` — Container for all parsed data from a single sheet:
  - `sheet_no` — Sheet identifier (0-based)
  - `sheet_name` — Display name of sheet
  - `parts` — List of Part objects
  - `segments` — List of Segment objects (electrical connections)
  - `tie_dots` — List of TieDot objects
  - `text_annotations` — List of TextAnnotation objects
  - `parttype_defs` — Dictionary of PartTypeDef objects

**Key Utility Functions:**
- `is_int(s) → bool` — Validates if string is integer-like
- `is_node(s) → bool` — Validates if string is valid electrical node reference
- `parse_node(s) → tuple[str, str]` — Extracts node components (e.g., "N$5" → ("N", "5"))
- `looks_like_parttype_header(line) → bool` — Detects *PARTTYPE* section start

**Dependencies:** None (foundational layer)

---

### 2. **pads_parser.py** — Core PADS Schematic Parser

**Purpose:** Implements deterministic, section-based parsing of PADS ASCII schematics with per-sheet isolation.

#### **Architecture Pattern: Section-Based Tokenization**

The parser uses a three-phase approach:

```
Phase 1: Section Identification
├─ Regex: ^\*[^*]+\*  (e.g., *CAE*, *PART*, *SIGNAL*)
└─ Output: list[(section_name, start_idx, end_idx)]

Phase 2: SHT Block Decomposition
├─ Identify sheet boundaries via *SHT* markers
├─ For each sheet: extract subsections within block boundary
└─ Pass sheet_no/sheet_name context to subsection handlers

Phase 3: Subsection Dispatch
├─ Route each section to appropriate handler (_parse_*_section)
└─ Accumulate results into per-sheet ParseResult
```

#### **Core Methods:**

**`_split_sections(lines: list[str]) → list[tuple[str, int, int]]`** (L127-154)
- Tokenizes ALL section headers in entire file
- Uses regex to find `*SECTION_NAME*` patterns
- Returns sorted list of (section_name, start_idx, end_idx) tuples
- **Critical:** Boundaries are determined by next section start, not heuristics
- **Example output:**
  ```
  [('CAE', 0, 100), ('TEXT', 100, 200), ('PART', 200, 500), ...]
  ```

**`_is_part_header_line(text: str) → bool`** (L156-172) — **DETERMINISTIC PART DETECTION**
- **Purpose:** Replace fragile heuristic matching with explicit rules
- **Rules:**
  1. Must have 6+ space-separated tokens
  2. First token (refdes) must start with alphanumeric or `_{$}`
  3. First token must NOT start with `@@@` (indicator line)
  4. Second token (part_type) must not be quoted (no `"` or `'` prefix)
  5. Tokens 2-5 must be 4 consecutive integers (X, Y coordinates + 2 reserved fields)
- **Return:** `True` if all rules satisfied, `False` otherwise
- **Example:**
  ```python
  _is_part_header_line("R100 RES 1000 2000 500 600")  # → True
  _is_part_header_line("@@@TEXT stuff") # → False (@@@ prefix)
  _is_part_header_line("U5 IC 100 200")  # → False (only 4 tokens)
  ```

**`_parse_sht_entry(line: str) → tuple[int, str]`** (L99-125)
- Parses `*SHT* sheet_no sheet_name` entries
- **Example:** `*SHT* 0 "01_POWER"` → `(0, "01_POWER")`
- Handles quoted sheet names

**`_parse_sht_block(lines, sections, block_start, block_end, sheet_no, sheet_name, result)`** (L382-395) — **PER-SHEET CONTEXT HANDLER**
- **Purpose:** Parse all subsections within a single sheet boundary
- **Flow:**
  1. Find all sections that fall within [block_start, block_end]
  2. Call `_dispatch_section()` for each subsection with sheet context
  3. Accumulate parsed data into single ParseResult
- **Sheet Isolation:** Each sheet gets independent ParseResult with correct sheet_no/sheet_name

**`_dispatch_section(section_name, lines, sec_start, sec_end, sheet_no, sheet_name, result)`** (L326-380)
- Router function that maps section names to handlers:
  - `*PART*` → `_parse_part_section()`
  - `*SIGNAL*` → `_parse_signal_section()`
  - `*PARTTYPE*` → `_parse_parttype_section()`
  - `*TEXT*` → `_parse_text_section()`
  - `*LINES*` → `_parse_lines_section()`
  - `*TIEDOTS*` → `_parse_tiedots_section()`
  - Others: pass (ignored sections like *CAE*, *BUSSES*)
- Each handler receives sheet context for data isolation

**`_parse_part_section(lines, start, end, sheet_no, sheet_name, result)`** (L264-324)
- Extracts component definitions from *PART* section
- **For each part header line:**
  1. Extract refdes, part_type, X, Y coordinates
  2. Parse REF-DES annotation offset (optional, e.g., "(100, 200)")
  3. Parse property key-value pairs (quoted, space-separated)
  4. Create Part object with `sheet_no` context
- **Uses:** `_is_part_header_line()` to identify entry boundaries (no heuristics)

**`_parse_signal_section(lines, start, end, sheet_no, sheet_name, result)`** (L401-440)
- Extracts electrical connections from *SIGNAL* section
- **For each signal:**
  1. Parse signal name (quoted string on first line)
  2. Parse node list (one node per line, e.g., "N$5", "U5.2")
  3. Create Segment for each connection pair (node1→node2)
  4. Associate with sheet_no

**`_parse_parttype_section(lines, start, end, sheet_no, sheet_name, result)`** (L442-478)
- Extracts part type definitions from *PARTTYPE* section
- **For each parttype entry:**
  1. Identify entry start via `looks_like_parttype_header()`
  2. Parse part type name, package, pin count
  3. Parse pin definitions (pin_number, pin_name)
  4. Create PartTypeDef object

#### **Entry Points:**

**`_parse_lines(lines: list[str]) → ParseResult`** (L496-510)
- **Purpose:** Main parser method for single-sheet files
- Legacy entry point; still used for backward compatibility
- **Flow:**
  1. Call `_split_sections()` to identify all sections
  2. Call `_dispatch_section()` for each section
  3. Return accumulated ParseResult

**`parse_sheet_results(file_path: Path) → list[tuple[str, ParseResult]]`** (L517-545) — **MULTI-SHEET ENTRY POINT**
- **Purpose:** Parse multi-sheet PADS file with per-sheet isolation
- **Flow:**
  1. Detect *SHT* markers via `_sheet_markers()`
  2. For each SHT entry: parse sheet_no/sheet_name via `_parse_sht_entry()`
  3. Calculate sheet block boundary
  4. Call `_parse_sht_block()` to parse subsections within boundary
  5. Return list of (sheet_name, ParseResult) tuples
- **Result:** Each sheet has independent ParseResult with correct context
- **Example output:**
  ```python
  [
      ("01_POWER", ParseResult(sheet_no=0, parts=[...], segments=[...])),
      ("02_INPUT_HDMI", ParseResult(sheet_no=1, parts=[...], segments=[...])),
      ...
  ]
  ```

#### **Key Design Decisions:**

1. **Deterministic Boundaries:** Section start/end determined by regex position, not content heuristics
2. **Sheet Isolation:** Per-sheet ParseResult prevents cross-contamination in multi-sheet files
3. **Multi-Encoding Support:** `_read_lines()` tries UTF-8 → CP949 → EUC-KR → Latin-1 fallback

---

### 3. **pads_pipeline.py** — Multi-Sheet Orchestration & Filename Safety

**Purpose:** Orchestrates multi-sheet conversion and ensures cross-platform filename compatibility.

#### **Core Functions:**

**`write_legacy_pro_project_file(output_dir, project_name, sheet_results) → Path`** (L28-66)
- Generates KiCad `.pro` project file (legacy format)
- **Key step (L50):** Uses `_sheet_output_filename()` for [sheetnames] entries
- Ensures all sheet references have sanitized filenames

**`write_root_multisheet_schematic(output_dir, project_name, sheet_results) → Path`** (L69-127)
- Generates root `.kicad_sch` file for multi-sheet project
- **Sheet node generation (L93):** Uses `_sheet_output_filename()` for sheet_file references
- Example: `(reference "01_POWER") (SCHEMATIC.kicad_sch")`

#### **Workflow:**
```
Input: SCHEMATIC.txt
  ↓
parse_sheet_results() → list[(sheet_name, ParseResult)]
  ↓
[Multi-sheet loop]
├─ For each (sheet_name, result):
│  ├─ Sanitize filename: _sheet_output_filename()
│  ├─ Build KiCad IR: build_kicad_ir()
│  ├─ Write .kicad_sch: write_kicad_schematic()
│  └─ Store result
├─ write_legacy_pro_project_file()
└─ write_root_multisheet_schematic()
  ↓
Output: /output_dir/
├─ .pro
├─ .kicad_sch
├─ _01_POWER.kicad_sch
├─ _02_INPUT_HDMI_PORT2_3.kicad_sch
└─ ...
```

---

### 4. **pads_to_kicad.py** — KiCad Schematic Writer

**Purpose:** Converts internal ParseResult to KiCad 6.x schematic format (.kicad_sch) with hardened output path handling.

#### **Key Functions:**

**`build_kicad_ir(result: ParseResult, project_name) → dict`**
- Converts ParseResult into KiCad intermediate representation
- Handles symbol placement grid calculation
- Builds connectivity arrays (pins → net assignments)

**`_sanitize_output_filename(name: str) → str`** (L66-70)
- **Identical regex to pads_pipeline version:** `re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)`
- Provides defensive sanitization at write point
- Fallback: "output.kicad_sch"

**`write_kicad_schematic(result, output_path, project_name, generator_name, version) → Path`** (L806-855)
- **Line 821:** `out_path = out_path.with_name(_sanitize_output_filename(out_path.name))`
  - Re-sanitizes filename component at write time
  - Handles edge cases where filename wasn't pre-sanitized
- **Line 822:** `out_path.parent.mkdir(parents=True, exist_ok=True)`
  - Ensures all parent directories exist
  - Allows deep output paths without pre-creation requirement
- **Lines 825+:** Proceeds with KiCad schematic generation:
  - Symbol placement on grid
  - Connectivity reconstruction
  - Text annotation placement
  - File output as S-expression

#### **Defensive Sanitization Pattern:**
```python
# Pipeline side (pre-sanitization)
out_filename = _sheet_output_filename(project_name, sheet_name)
out_path = output_dir / out_filename
write_kicad_schematic(sheet_result, out_path, ...)

# Writer side (post-sanitization)
def write_kicad_schematic(result, output_path, ...):
    out_path = output_path.with_name(_sanitize_output_filename(output_path.name))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # ... generate file
```

This double-layer ensures safety even if pipeline passes unsanitized path.

---

## Data Flow Diagram

```
PADS .sch File
  ↓
PadsParser.parse_sheet_results()
  ├─ _sheet_markers() → [sheet_idx1, sheet_idx2, ...]
  ├─ _parse_sht_entry() → [(0, "01_POWER"), (1, "02_INPUT"), ...]
  ├─ _split_sections() → [("PART", s1, e1), ("SIGNAL", s2, e2), ...]
  ├─ _parse_sht_block() [for each sheet]
  │  ├─ _dispatch_section() [for each subsection in block]
  │  │  ├─ _parse_part_section() → Part[]
  │  │  ├─ _parse_signal_section() → Segment[]
  │  │  ├─ _parse_parttype_section() → PartTypeDef{}
  │  │  └─ _parse_text_section() → TextAnnotation[]
  │  └─ ParseResult(sheet_no=N, sheet_name=X, parts=[], ...)
  └─ [(sheet_name, ParseResult), ...]
  ↓
Pipeline Orchestration
  ├─ write_legacy_pro_project_file() → .pro
  ├─ write_root_multisheet_schematic() → .kicad_sch
  └─ [for each sheet]:
     ├─ build_kicad_ir() → {symbols: [...], pins: [...], ...}
     ├─ write_kicad_schematic() → _SHEET.kicad_sch
     └─ _sanitize_output_filename() [defensive]
  ↓
Output Files
├─ .pro (KiCad project file with sheet references)
├─ .kicad_sch (multi-sheet root with sheet hierarchy)
└─ _01_POWER.kicad_sch ... _10_USB_UPDATE.kicad_sch
```

---

## Parsing Rules & Constraints

### **Section Identification**
- **Pattern:** `^\*[^*]+\*` (line starts with `*`, contains non-`*` chars, ends with `*`)
- **Expected sections:** CAE, TEXT, LINES, CAEDECAL, PARTTYPE, PART, BUSSES, OFFPAGE REFS, TIEDOTS, CONNECTION, SIGNAL, NETNAMES
- **Processing order:** Determined by file position (not alphabetical)

### **Part Header Detection (Deterministic Rules)**
1. Space-separated tokens: must have ≥6 tokens
2. First token (refdes): must start with `[A-Za-z0-9_{$]` (no special chars except `_`, `{`, `}`, `$`)
3. First token: must NOT start with `@@@` (indicator lines)
4. Second token (part_type): must not begin with quote (`"` or `'`)
5. Tokens 2-5: must be 4 consecutive integers (coordinates + reserved fields)
6. Token 5: acts as boundary marker for multi-line part entries

### **Sheet Context Propagation**
- Each ParseResult carries `sheet_no` (0-based) and `sheet_name`
- Parts, Segments, TextAnnotations tagged with sheet context
- No cross-sheet data merging


---

## Multi-Encoding Support

The parser attempts sequential decoding with fallback:
```python
encodings = ["utf-8", "cp949", "euc-kr", "latin-1"]
for enc in encodings:
    try:
        return data.decode(enc).splitlines()
    except UnicodeDecodeError:
        continue
return data.decode("latin-1", errors="replace").splitlines()
```

**Rationale:** PADS files may originate from different regions:
- UTF-8: Standard international encoding
- CP949: Korean Windows (legacy PADS installations)
- EUC-KR: Korean Unix
- Latin-1: Fallback for Western encoding

---

## Dependencies & External APIs

**Python Standard Library:**
- `pathlib.Path` — Cross-platform file handling
- `re` — Regex section header detection
- `collections.defaultdict` — Efficient data aggregation
- `typing` — Type hints (Python 3.8+)

**Internal Dependencies:**
- `pads_model.ParseResult, Part, Segment, ...` — Data classes
- `pads_parser.PadsParser` — Parsing engine
- `pads_to_kicad.build_kicad_ir, write_kicad_schematic` — Output generation

**No external packages** (pure Python standard library + internal modules)

---

## Known Limitations & Future Enhancements

1. **Hierarchical Symbols:** Current parser flattens all parts; doesn't preserve symbol hierarchy
2. **Advanced Routing:** Electrical traces reconstructed as node connections; layout/trace geometry not preserved
3. **Analog/Spice Models:** Part value/model annotations not fully extracted
4. **Performance:** Large schematics (>5000 parts) may experience memory overhead during IR generation

---

## Version Information

- **Python:** 3.8+ (type hints, f-strings)
- **KiCad Format:** Schematic format version 20260306
- **PADS Format:** PADS-LOGIC ASCII (tested with V2007.0)
