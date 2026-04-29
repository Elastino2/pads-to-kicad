#!/usr/bin/env python3
"""Generate COMPONENTS.md from PADS schematic text file.

Usage:
    python3 tools/list_components.py [--sch SCHEMATIC_FILE] [--out COMPONENTS.md]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Allow running from repo root
_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from pads_parser import PadsParser


# Map prefix letters → human-readable category
_PREFIX_CATEGORY: dict[str, str] = {
    "R":  "Resistors",
    "C":  "Capacitors",
    "L":  "Inductors",
    "U":  "ICs / Semiconductors",
    "IC": "ICs / Semiconductors",
    "Q":  "Transistors",
    "D":  "Diodes",
    "F":  "Fuses",
    "JP": "Connectors / Jumpers",
    "J":  "Connectors / Jumpers",
    "CN": "Connectors / Jumpers",
    "P":  "Connectors / Jumpers",
    "X":  "Crystals / Oscillators",
    "Y":  "Crystals / Oscillators",
    "BT": "Batteries",
    "SW": "Switches",
    "TP": "Test Points",
    "FB": "Ferrite Beads",
    "T":  "Transformers",
    "Z":  "Zener Diodes",
    "LED":"LEDs",
}

# Map part_class codes → human-readable type
_CLASS_LABEL: dict[str, str] = {
    "RES": "Resistor",
    "CAP": "Capacitor",
    "IND": "Inductor",
    "TTL": "IC",
    "U":   "IC",
    "UND": "Undefined",
    "PWR": "Power",
    "GND": "Ground",
}


def _ref_prefix(ref: str) -> str:
    """Extract letter prefix from reference designator (e.g. 'R12' → 'R', 'IC5' → 'IC')."""
    m = re.match(r"([A-Za-z]+)", ref)
    return m.group(1).upper() if m else "?"


def _category(ref: str) -> str:
    prefix = _ref_prefix(ref)
    # Longest-match first
    for p in sorted(_PREFIX_CATEGORY, key=len, reverse=True):
        if prefix.startswith(p):
            return _PREFIX_CATEGORY[p]
    return "Other"


def _extract_value(part_type: str) -> str:
    """Try to extract a short human-readable value from a PADS part_type string."""
    # e.g. "RXFX-1005-10K-F", "CAP-1608-0.1UF-25V", "MP2482_MPS", "MMBT3904_ONSEMI"
    # Strategy: take the longest token that looks like a value
    parts = re.split(r"[_\-]", part_type)
    value_tokens = [
        t for t in parts
        if re.search(r"\d", t) and any(
            c.isalpha() for c in t
        )
    ]
    return value_tokens[0] if value_tokens else part_type.split("_")[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--sch",
        type=Path,
        default=Path("SCHEMATIC/Rev0.1/GLX7.Schematic.260413.txt"),
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("COMPONENTS.md"),
    )
    args = ap.parse_args()

    parser = PadsParser()
    sheet_results = parser.parse_sheet_results(args.sch)

    # Build: ref → (part_type, part_class, sheet_num, description, manufacturer, mpn)
    records: list[dict] = []
    for sheet_idx, (sheet_name, pr) in enumerate(sheet_results, start=1):
        for ref, part in sorted(pr.parts.items()):
            ptd = pr.part_types.get(part.part_type)
            part_class = ptd.part_class if ptd else "UND"
            pin_count = len(ptd.pins) if ptd else 0
            props = part.properties
            desc = props.get("Description", "")
            mfr  = props.get("Manufacturer_Name", "")
            mpn  = props.get("Manufacturer_Part_Number", "")
            records.append({
                "ref":        ref,
                "prefix":     _ref_prefix(ref),
                "category":   _category(ref),
                "part_type":  part.part_type,
                "part_class": part_class,
                "class_label":_CLASS_LABEL.get(part_class, part_class),
                "pin_count":  pin_count,
                "value":      _extract_value(part.part_type),
                "description":desc,
                "manufacturer":mfr,
                "mpn":        mpn,
                "sheet":      sheet_idx,
            })

    # Sort by natural ref order within category
    def _sort_key(r: dict) -> tuple:
        nums = re.findall(r"\d+", r["ref"])
        return (r["category"], r["prefix"], int(nums[0]) if nums else 0)

    records.sort(key=_sort_key)

    # Group by category
    from itertools import groupby

    categories: dict[str, list[dict]] = {}
    for rec in records:
        categories.setdefault(rec["category"], []).append(rec)

    # Build markdown
    lines: list[str] = []
    lines.append("# GLX7 Component List")
    lines.append("")
    lines.append("> Source: `SCHEMATIC/Rev0.1/GLX7.Schematic.260413.txt`  ")
    lines.append("> Generated: 2026-04-17  ")
    lines.append("> Reference PDF: `SCHEMATIC/Rev0.1/GLX7.Schematic.260415.pdf`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Category | Count |")
    lines.append("|---|---|")
    total = 0
    for cat, recs in sorted(categories.items()):
        lines.append(f"| {cat} | {len(recs)} |")
        total += len(recs)
    lines.append(f"| **Total** | **{total}** |")
    lines.append("")

    for cat, recs in sorted(categories.items()):
        lines.append(f"## {cat}")
        lines.append("")
        lines.append("| Ref | Part Type | Value | Pins | Sheet | Description | Manufacturer | MPN |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in recs:
            def _esc(s: str) -> str:
                return s.replace("|", "\\|")
            lines.append(
                f"| {_esc(r['ref'])} "
                f"| {_esc(r['part_type'])} "
                f"| {_esc(r['value'])} "
                f"| {r['pin_count']} "
                f"| {r['sheet']} "
                f"| {_esc(r['description'])} "
                f"| {_esc(r['manufacturer'])} "
                f"| {_esc(r['mpn'])} |"
            )
        lines.append("")

    out_text = "\n".join(lines) + "\n"
    args.out.write_text(out_text, encoding="utf-8")
    print(f"Written {len(records)} components to {args.out}")
    print(f"Categories: {', '.join(f'{cat}({len(recs)})' for cat, recs in sorted(categories.items()))}")


if __name__ == "__main__":
    main()
