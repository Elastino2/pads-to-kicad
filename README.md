# PADS to KiCad

## Overview

The PADS to KiCad converter is a Python-based tool that transforms PADS ASCII schematic files (.sch) into KiCad multi-sheet schematic projects (.kicad_sch). The converter handles complex multi-sheet architectures, maintains electrical connectivity, and ensures cross-platform filename safety.

**Key Features:**
- Multi-sheet decomposition with per-sheet context isolation
- Deterministic part header detection (no heuristic pattern matching)
- Cross-platform filename sanitization
- Multi-encoding support (UTF-8, CP949, EUC-KR, Latin-1)
- Comprehensive connectivity reconstruction (nets, references, pin mappings)

**Dependencies: Python3**

**Architecture:** [ARCHITECTURE.md](ARCHITECTURE.md)

**License:** [GNU General Public License v3.0](LICENSE)

---

## Usage

```bash
# Single-sheet output
python tools/pads_pipeline.py exported_from_pads_to_ascii.txt

# Multi-sheet output (recommended)
python tools/pads_pipeline.py \
  --kicad-sch-multi-dir output/ \
  --project-name KiCad \
  exported_from_pads_to_ascii.txt
```

**Output:**
```
output/
├── KiCad.pro                          ← KiCad project file
├── KiCad.kicad_sch                    ← Root multi-sheet schematic
├── KiCad_01_POWER.kicad_sch
├── KiCad_02_INPUT_HDMI_PORT2_3.kicad_sch   ← '/' sanitized to '_'
└── ...
```

## Module Overview

| File | Role |
|------|------|
| `pads_model.py` | Data classes: `ParseResult`, `Part`, `Segment`, `PartTypeDef` |
| `pads_parser.py` | Core PADS ASCII parser; SHT-block subsection dispatcher |
| `pads_pipeline.py` | Multi-sheet orchestration; filename sanitization |
| `pads_to_kicad.py` | KiCad S-expression schematic writer |
| `list_components.py` | BOM / component listing utility |

See [ARCHITECTURE.md](../ARCHITECTURE.md) for detailed module design, parsing rules, and data flow diagrams.