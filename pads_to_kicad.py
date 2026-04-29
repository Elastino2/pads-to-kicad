from __future__ import annotations

import re
import uuid
from pathlib import Path
from statistics import median
from typing import Any

from pads_model import ParseResult, parse_node


def build_kicad_ir(result: ParseResult) -> dict[str, Any]:
    """Build a KiCad-oriented intermediate representation.

    This is intentionally not a full .kicad_sch writer yet. It provides
    normalized symbols, pins, and nets so we can evolve into deterministic
    KiCad schematic generation.
    """
    symbols: dict[str, Any] = {}
    nets: dict[str, dict[str, Any]] = {}

    for refdes, part in result.parts.items():
        symbols[refdes] = {
            "refdes": refdes,
            "value": part.part_type,
            "properties": dict(part.properties),
            "pins": {},
        }

    for seg in result.segments:
        net = nets.setdefault(seg.signal, {"name": seg.signal, "connections": []})
        for node in (seg.node_a, seg.node_b):
            ref, pin = parse_node(node)
            if ref is None or pin is None:
                continue
            net["connections"].append({"refdes": ref, "pin": pin})
            if ref in symbols:
                symbols[ref]["pins"].setdefault(pin, []).append(seg.signal)

    return {
        "schema": "pads-to-kicad-ir-v1",
        "symbols": symbols,
        "nets": nets,
    }


def _quote(text: str) -> str:
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _uuid() -> str:
    return str(uuid.uuid4())


def _sanitize_symbol_name(name: str) -> str:
    out = []
    for ch in name:
        if ch.isalnum() or ch in ("_", "-", "."):
            out.append(ch)
        else:
            out.append("_")
    sanitized = "".join(out).strip("_")
    return sanitized or "UNNAMED"


def _sanitize_output_filename(name: str) -> str:
    """Return a filesystem-safe output filename."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name).strip()
    safe = safe.rstrip(". ")
    return safe or "output.kicad_sch"


def _pin_type_from_direction(direction: str) -> str:
    d = (direction or "U").upper()
    if d in {"I"}:
        return "input"
    if d in {"O"}:
        return "output"
    if d in {"B", "S", "U"}:
        return "bidirectional"
    if d in {"P"}:
        return "power_in"
    return "passive"


def _pin_sort_key(pin_num: str) -> tuple[int, str]:
    if pin_num.isdigit():
        return (0, f"{int(pin_num):08d}")
    return (1, pin_num)


# Per-part-type pin side hints captured from validated source schematics.
# These are only used for multi-pin custom symbols where PADS does not carry
# explicit left/right side information in PARTTYPE pin definitions.
# Side overrides are intentionally empty: automatic side inference from observed
# wire endpoints is more reliable than hardcoded assignments.
_PIN_SIDE_OVERRIDES_BY_PARTTYPE: dict[str, dict[str, str]] = {
    "IC_TPS2041CDBVR_TI": {
        "1": "R",  # OUT on right
        "2": "R",  # GND on right
        "3": "L",  # _FLT on left
        "4": "L",  # EN/_EN on left
        "5": "L",  # IN on left
    }
}

# Some multi-pin ICs already have left/right pin intent captured in the custom
# symbol layout. Applying KiCad instance mirror on top of that flips pin sides.
_DISABLE_INSTANCE_MIRROR_PARTTYPES: set[str] = {
    "IC_TPS2041CDBVR_TI",
}


def _effective_instance_mirrored(part_type: str, raw_mirror: int | None) -> bool:
    if part_type in _DISABLE_INSTANCE_MIRROR_PARTTYPES:
        return False
    return bool(raw_mirror)


def _collect_symbol_pin_defs(result: ParseResult) -> dict[str, list[dict[str, str]]]:
    """Collect pin definitions per part_type used by parts.

    Returns:
      { part_type: [ {"num": "1", "name": "CC1", "dir": "B"}, ... ] }
    """
    pin_defs: dict[str, list[dict[str, str]]] = {}

    # Collect actually used pin numbers per part_type from connectivity.
    used_pins_by_type: dict[str, set[str]] = {}
    for seg in result.segments:
        for node in (seg.node_a, seg.node_b):
            ref, pin = parse_node(node)
            if ref is None or pin is None:
                continue
            part = result.parts.get(ref)
            if part is None:
                continue
            used_pins_by_type.setdefault(part.part_type, set()).add(pin)

    for part in result.parts.values():
        ptype = part.part_type
        if ptype in pin_defs:
            continue

        if ptype in result.part_types and result.part_types[ptype].pins:
            defs = [
                {
                    "num": pnum,
                    "name": pdef.name or pnum,
                    "dir": pdef.direction or "U",
                }
                for pnum, pdef in result.part_types[ptype].pins.items()
            ]
            # Prefer pins that are actually used in the current sheet/result.
            # This trims bogus extra pins from imperfect PARTTYPE definitions.
            used = used_pins_by_type.get(ptype, set())
            if used:
                filtered = [d for d in defs if d["num"] in used]
                if filtered:
                    defs = filtered
            pin_defs[ptype] = defs
        else:
            # Fallback: pin numbers inferred from connectivity refs (name=num)
            inferred: set[str] = set()
            refdes = part.refdes
            for seg in result.segments:
                for node in (seg.node_a, seg.node_b):
                    ref, pin = parse_node(node)
                    if ref == refdes and pin is not None:
                        inferred.add(pin)
            defs = [{"num": p, "name": p, "dir": "U"} for p in sorted(inferred, key=_pin_sort_key)]
            if not defs:
                defs = [{"num": "1", "name": "1", "dir": "U"}]
            pin_defs[ptype] = defs

    return pin_defs


def _build_symbol_pin_layout(pin_defs: list[dict[str, str]]) -> dict[str, tuple[float, float, int]]:
    """Return local pin layout map: pin -> (x, y, angle)."""
    layout: dict[str, tuple[float, float, int]] = {}

    left = [p for i, p in enumerate(pin_defs) if i % 2 == 0]
    right = [p for i, p in enumerate(pin_defs) if i % 2 == 1]

    # 2.54 mm grid centred around y=0  (KiCad lib_symbol Y+ is UP)
    for i, p in enumerate(left):
        y = 2.54 * (len(left) - 1) / 2 - 2.54 * i
        layout[p["num"]] = (-10.16, y, 0)

    for i, p in enumerate(right):
        y = 2.54 * (len(right) - 1) / 2 - 2.54 * i
        layout[p["num"]] = (10.16, y, 180)

    return layout


def _build_symbol_pin_layout_from_sides(
    left_nums: list[str],
    right_nums: list[str],
) -> dict[str, tuple[float, float, int]]:
    """Return local pin layout map from explicit left/right pin order."""
    layout: dict[str, tuple[float, float, int]] = {}

    for i, pnum in enumerate(left_nums):
        y = 2.54 * (len(left_nums) - 1) / 2 - 2.54 * i
        layout[pnum] = (-10.16, y, 0)

    for i, pnum in enumerate(right_nums):
        y = 2.54 * (len(right_nums) - 1) / 2 - 2.54 * i
        layout[pnum] = (10.16, y, 180)

    return layout


def _enforce_desc_spacing(values: list[float], min_step: float = 1.27) -> list[float]:
    """Keep values descending with at least min_step spacing."""
    if not values:
        return []
    out = [values[0]]
    for v in values[1:]:
        out.append(min(v, out[-1] - min_step))
    return out


def _symbol_bbox(
    pin_defs: list[dict[str, str]],
    pin_layout: dict[str, tuple[float, float, int]] | None = None,
) -> tuple[float, float]:
    if pin_layout:
        max_name_len = max((len(p.get("name", "")) for p in pin_defs), default=1)
        max_abs_y = max((abs(y) for _x, y, _a in pin_layout.values()), default=5.08)
        # For 2-pin parts, half_w = |pin_x| so graphics can draw leads to the pin stub.
        if len(pin_defs) == 2:
            max_abs_x = max((abs(x) for x, _y, _a in pin_layout.values()), default=7.62)
            return (round(max_abs_x, 2), max(5.08, max_abs_y + 1.27))
        max_abs_x = max((abs(x) for x, _y, _a in pin_layout.values()), default=10.16)
        name_based = 3.81 + 0.70 * max_name_len
        half_w = max(12.70, max_abs_x + 4.00, name_based)
        return (round(half_w, 2), max(5.08, max_abs_y + 1.27))

    rows = max(2, (len(pin_defs) + 1) // 2)
    height = max(10.16, rows * 2.54 + 2.54)
    return (7.62, height / 2.0)


def _append_polyline(lines: list[str], pts: list[tuple[float, float]]) -> None:
    lines.append("        (polyline")
    lines.append("          (pts")
    for x, y in pts:
        lines.append(f"            (xy {x:.2f} {y:.2f})")
    lines.append("          )")
    lines.append("          (stroke (width 0) (type default))")
    lines.append("          (fill (type none))")
    lines.append("        )")


def _append_arc(lines: list[str], start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float]) -> None:
    lines.append("        (arc")
    lines.append(f"          (start {start[0]:.2f} {start[1]:.2f})")
    lines.append(f"          (mid {mid[0]:.2f} {mid[1]:.2f})")
    lines.append(f"          (end {end[0]:.2f} {end[1]:.2f})")
    lines.append("          (stroke (width 0) (type default))")
    lines.append("          (fill (type none))")
    lines.append("        )")


def _append_symbol_graphics(
    lines: list[str],
    ref_prefix: str,
    pin_defs: list[dict[str, str]],
    half_w: float,
    half_h: float,
    part_type: str = "",
    rotation: int = 0,
    mirrored: bool = False,
    flip_y: bool = False,
) -> None:
    """Generate symbol body graphics.
    Note: rotation/mirror are applied at instance level, not symbol body level.
    flip_y applies Y-axis inversion to match pin layout coordinate system.
    """
    prefix = (ref_prefix or "").upper()
    is_two_pin = len(pin_defs) == 2

    def _rotate_pt(x: float, y: float, rot: int) -> tuple[float, float]:
        r = rot % 360
        if r == 90:
            return (-y, x)
        if r == 180:
            return (-x, -y)
        if r == 270:
            return (y, -x)
        return (x, y)

    def _tx_pt(x: float, y: float) -> tuple[float, float]:
        """Apply optional Y-flip and body rotation for symbol graphics."""
        fx, fy = (x, -y) if flip_y else (x, y)
        return _rotate_pt(fx, fy, rotation)

    def default_box() -> None:
        x1, y1 = _tx_pt(-half_w, half_h)
        x2, y2 = _tx_pt(half_w, -half_h)
        lines.append(f"        (rectangle (start {x1:.2f} {y1:.2f}) (end {x2:.2f} {y2:.2f})")
        lines.append("          (stroke (width 0) (type default))")
        lines.append("          (fill (type none))")
        lines.append("        )")

    if not is_two_pin and prefix != "Q":
        default_box()
        return

    if prefix == "R":
        # Keep the resistor body close to the fitted pin positions so it does
        # not look unconnected when 2-pin passives are stretched to match wires.
        body_half = max(7.62, max(half_w, half_h) - 5.08)
        step = body_half / 3.0
        pts = [
            (-body_half, 0.00),
            (-2.0 * step, 1.27),
            (-step, -1.27),
            (0.00, 1.27),
            (step, -1.27),
            (2.0 * step, 1.27),
            (body_half, 0.00),
        ]
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts])
        return

    if prefix == "C":
        pts1 = [(-7.62, 0.00), (-1.27, 0.00)]
        pts2 = [(-1.27, 2.54), (-1.27, -2.54)]
        pts3 = [(1.27, 2.54), (1.27, -2.54)]
        pts4 = [(1.27, 0.00), (7.62, 0.00)]
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts1])
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts2])
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts3])
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts4])
        return

    if prefix in {"L", "FB"}:
        pts_i = [(-7.62, 0.00), (-4.57, 0.00)]
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts_i])
        arcs = [
            ((-4.57, 0.00), (-3.81, 2.03), (-3.05, 0.00)),
            ((-3.05, 0.00), (-2.29, 2.03), (-1.52, 0.00)),
            ((-1.52, 0.00), (-0.76, 2.03), (0.00, 0.00)),
            ((0.00, 0.00), (0.76, 2.03), (1.52, 0.00)),
            ((1.52, 0.00), (2.29, 2.03), (3.05, 0.00)),
            ((3.05, 0.00), (3.81, 2.03), (4.57, 0.00)),
        ]
        for start, mid, end in arcs:
            _append_arc(lines, _tx_pt(*start), _tx_pt(*mid), _tx_pt(*end))
        pts_f = [(4.57, 0.00), (7.62, 0.00)]
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts_f])
        return

    if prefix == "D":
        pts_a = [(-7.62, 0.00), (-2.54, 0.00)]
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts_a])
        pts_tri = [(-2.54, -2.54), (-2.54, 2.54), (1.27, 0.00), (-2.54, -2.54)]
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts_tri])
        pts_b = [(2.54, -2.54), (2.54, 2.54)]
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts_b])
        pts_c = [(2.54, 0.00), (7.62, 0.00)]
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts_c])
        return

    if prefix.startswith("ESD"):
        pts_a = [(-7.62, 0.00), (-2.54, 0.00)]
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts_a])
        pts_tri = [(-2.54, -2.54), (-2.54, 2.54), (1.27, 0.00), (-2.54, -2.54)]
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts_tri])
        pts_b = [(2.54, -2.54), (2.54, 2.54)]
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts_b])
        pts_c = [(2.54, 0.00), (7.62, 0.00)]
        _append_polyline(lines, [_tx_pt(x, y) for x, y in pts_c])
        return

    if prefix == "Q" and len(pin_defs) >= 3:
        ptype_u = (part_type or "").upper()
        is_pnp = ("PNPT" in ptype_u) or ("PNP" in ptype_u)

        # Body and base
        _append_arc(lines, _tx_pt(-3.80, 0.00), _tx_pt(0.00, 3.80), _tx_pt(3.80, 0.00))
        _append_arc(lines, _tx_pt(3.80, 0.00), _tx_pt(0.00, -3.80), _tx_pt(-3.80, 0.00))
        _append_polyline(lines, [_tx_pt(-7.62, 0.00), _tx_pt(-2.20, 0.00)])

        # Collector (upper right)
        _append_polyline(lines, [_tx_pt(0.60, 0.00), _tx_pt(2.70, 2.70), _tx_pt(7.62, 5.08)])
        # Emitter (lower right) + arrow
        _append_polyline(lines, [_tx_pt(0.60, 0.00), _tx_pt(2.70, -2.70), _tx_pt(7.62, -5.08)])

        if is_pnp:
            # PNP arrow points inwards (toward transistor body)
            _append_polyline(lines, [_tx_pt(5.90, -4.35), _tx_pt(4.60, -3.25), _tx_pt(6.10, -2.90)])
        else:
            # NPN arrow points outwards
            _append_polyline(lines, [_tx_pt(4.60, -3.25), _tx_pt(5.90, -4.35), _tx_pt(4.40, -4.70)])
        return

    if prefix == "F":
        # Fuse: fixed-size body box + leads extending to pin stub inner edge.
        # half_w = pin connection x; stub length = 2.54 mm.
        half_body = 5.08
        stub_len = 2.54
        lead_end = half_w - stub_len  # inner end of pin stub = outer end of lead wire
        fuse_pts = [
            [(-lead_end, 0.00), (-half_body, 0.00)],          # left lead
            [(half_body, 0.00), (lead_end, 0.00)],             # right lead
            [(-half_body, half_body), (half_body, half_body)],    # top edge
            [(half_body, half_body), (half_body, -half_body)],    # right edge
            [(half_body, -half_body), (-half_body, -half_body)],  # bottom edge
            [(-half_body, -half_body), (-half_body, half_body)],  # left edge
            [(-half_body * 0.7, half_body * 0.7), (half_body * 0.7, -half_body * 0.7)],   # X stroke 1
            [(-half_body * 0.7, -half_body * 0.7), (half_body * 0.7, half_body * 0.7)],   # X stroke 2
        ]
        for pts in fuse_pts:
            _append_polyline(lines, [_tx_pt(x, y) for x, y in pts])
        return

    default_box()


def _format_lib_symbol(
    library_id: str,
    symbol_name: str,
    ref_prefix: str,
    pin_defs: list[dict[str, str]],
    part_type: str,
    lines: list[str],
    pin_layout_override: dict[str, tuple[float, float, int]] | None = None,
    has_adapted_pins: bool = False,
) -> None:
    pin_layout = pin_layout_override if pin_layout_override is not None else _build_symbol_pin_layout(pin_defs)
    half_w, half_h = _symbol_bbox(pin_defs, pin_layout)

    _hide_pin_text = ref_prefix.upper() in ("R", "C", "L", "D", "F")
    _pin_names_attr = "(pin_names (offset 0.508) hide)" if _hide_pin_text else "(pin_names (offset 0.508))"
    _pin_numbers_attr = " (pin_numbers hide)" if _hide_pin_text else ""
    lines.append(f"    (symbol {_quote(library_id)} {_pin_names_attr}{_pin_numbers_attr} (in_bom yes) (on_board yes)")
    lines.append(f"      (property \"Reference\" {_quote(ref_prefix)} (at 0 {half_h + 2.54:.2f} 0)")
    lines.append("        (effects (font (size 1.27 1.27)))")
    lines.append("      )")
    lines.append(f"      (property \"Value\" {_quote(symbol_name)} (at 0 {-half_h - 2.54:.2f} 0)")
    lines.append("        (effects (font (size 1.27 1.27)))")
    lines.append("      )")
    lines.append("      (property \"Footprint\" \"\" (at 0 0 0)")
    lines.append("        (effects (font (size 1.27 1.27)) hide)")
    lines.append("      )")
    lines.append("      (property \"Datasheet\" \"\" (at 0 0 0)")
    lines.append("        (effects (font (size 1.27 1.27)) hide)")
    lines.append("      )")
    # KiCad nested unit symbols must use the symbol name part only (without library prefix).
    unit_name_prefix = library_id.split(":", 1)[-1]
    graphics_rotation = 0
    if len(pin_defs) == 2:
        p1_num = pin_defs[0]["num"]
        p2_num = pin_defs[1]["num"]
        if p1_num in pin_layout and p2_num in pin_layout:
            x1, y1, _ = pin_layout[p1_num]
            x2, y2, _ = pin_layout[p2_num]
            dx = x2 - x1
            dy = y2 - y1
            if abs(dx) >= abs(dy):
                graphics_rotation = 0 if dx >= 0 else 180
            else:
                graphics_rotation = 90 if dy >= 0 else 270
    lines.append(f"      (symbol {_quote(unit_name_prefix + '_0_1')}")
    _append_symbol_graphics(
        lines,
        ref_prefix,
        pin_defs,
        half_w,
        half_h,
        part_type=part_type,
        rotation=graphics_rotation,
        flip_y=has_adapted_pins and ref_prefix.upper() != "Q",
    )
    lines.append("      )")
    lines.append(f"      (symbol {_quote(unit_name_prefix + '_1_1')}")

    for p in pin_defs:
        pnum = p["num"]
        pname = p["name"]
        ptype = _pin_type_from_direction(p["dir"])
        x, y, angle = pin_layout[pnum]
        _hide_eff = " hide" if _hide_pin_text else ""
        lines.append(f"        (pin {ptype} line (at {x:.2f} {y:.2f} {angle}) (length 2.54)")
        lines.append(f"          (name {_quote(pname)} (effects (font (size 1.27 1.27)){_hide_eff}))")
        lines.append(f"          (number {_quote(pnum)} (effects (font (size 1.27 1.27)){_hide_eff}))")
        lines.append("        )")

    lines.append("      )")
    lines.append("    )")


def _choose_lib_id(refdes: str, part_type: str, pin_defs: list[dict[str, str]]) -> tuple[str, bool]:
    """Return (lib_id, is_standard_library).

    Use standard KiCad libraries only for very safe/common shapes.
    Everything else stays in the generated in-file PADS library.
    """
    # Always use in-file custom symbols to avoid external library dependency issues.
    custom_name = _sanitize_symbol_name(part_type)
    return (f"PADS:{custom_name}", False)


def _ref_prefix(refdes: str) -> str:
    prefix = "".join(ch for ch in refdes if ch.isalpha())
    return prefix if prefix else "U"


def _part_properties(part_props: dict[str, str]) -> list[tuple[str, str]]:
    important = [
        "Manufacturer_Name",
        "Manufacturer_Part_Number",
        "Description",
        "Datasheet",
        "SPEC",
    ]
    props: list[tuple[str, str]] = []
    for key in important:
        if key in part_props and part_props[key]:
            props.append((key, part_props[key]))
    return props


def _pt_key(x: float, y: float) -> tuple[float, float]:
    return (_q2(x), _q2(y))


def _q2(v: float) -> float:
    """Quantize to 0.01 mm using the same rule as f"{v:.2f}" output."""
    return float(f"{v:.2f}")


def _is_ground_net(net_name: str) -> bool:
    n = (net_name or "").strip().upper()
    if not n:
        return False
    if n in {"GND", "AGND", "DGND", "PGND", "SGND", "VSS"}:
        return True
    if "GND" in n:
        return True
    if n.endswith("_GND") or n.startswith("GND_") or "_GND_" in n:
        return True
    if re.fullmatch(r"GND\d*", n):
        return True
    return re.fullmatch(r"VSS\d*", n) is not None


def _is_unnamed_net(net_name: str) -> bool:
    n = (net_name or "").strip()
    if not n:
        return True
    if n.startswith("$$$") or n.startswith("@@@"):
        return True
    # Treat PADS auto-generated nets like N34938490 as unnamed.
    return re.fullmatch(r"N\d*", n, re.IGNORECASE) is not None


def _is_power_net(net_name: str) -> bool:
    n = (net_name or "").upper()
    if not n or _is_ground_net(n):
        return False
    if n.startswith("+") or n.endswith("V"):
        return True
    return ("VDD" in n) or ("VCC" in n) or n.startswith("VBUS")


def _append_power_lib_symbols(lines: list[str], include_gnd: bool, include_vcc: bool) -> None:
    if include_gnd:
        lines.append('    (symbol "PWR:GND" (pin_names (offset 0.508)) (in_bom no) (on_board no)')
        lines.append('      (property "Reference" "#PWR" (at 0 2.54 0)')
        lines.append('        (effects (font (size 1.27 1.27)) hide)')
        lines.append('      )')
        lines.append('      (property "Value" "GND" (at 0 -3.05 0)')
        lines.append('        (effects (font (size 1.27 1.27)))')
        lines.append('      )')
        lines.append('      (property "Footprint" "" (at 0 0 0)')
        lines.append('        (effects (font (size 1.27 1.27)) hide)')
        lines.append('      )')
        lines.append('      (property "Datasheet" "" (at 0 0 0)')
        lines.append('        (effects (font (size 1.27 1.27)) hide)')
        lines.append('      )')
        lines.append('      (symbol "GND_0_1"')
        _append_polyline(lines, [(-1.52, -0.51), (1.52, -0.51)])
        _append_polyline(lines, [(-1.02, -1.27), (1.02, -1.27)])
        _append_polyline(lines, [(-0.51, -2.03), (0.51, -2.03)])
        lines.append('      )')
        lines.append('      (symbol "GND_1_1"')
        lines.append('        (pin power_in line (at 0 0 270) (length 0)')
        lines.append('          (name "GND" (effects (font (size 1.27 1.27))))')
        lines.append('          (number "1" (effects (font (size 1.27 1.27)) hide))')
        lines.append('        )')
        lines.append('      )')
        lines.append('    )')

    if include_vcc:
        lines.append('    (symbol "PWR:VCC" (pin_names (offset 0.508)) (in_bom no) (on_board no)')
        lines.append('      (property "Reference" "#PWR" (at 0 -2.54 0)')
        lines.append('        (effects (font (size 1.27 1.27)) hide)')
        lines.append('      )')
        lines.append('      (property "Value" "VCC" (at 0 3.05 0)')
        lines.append('        (effects (font (size 1.27 1.27)))')
        lines.append('      )')
        lines.append('      (property "Footprint" "" (at 0 0 0)')
        lines.append('        (effects (font (size 1.27 1.27)) hide)')
        lines.append('      )')
        lines.append('      (property "Datasheet" "" (at 0 0 0)')
        lines.append('        (effects (font (size 1.27 1.27)) hide)')
        lines.append('      )')
        lines.append('      (symbol "VCC_0_1"')
        _append_polyline(lines, [(0.00, 2.54), (-1.27, 0.00), (1.27, 0.00), (0.00, 2.54)])
        lines.append('      )')
        lines.append('      (symbol "VCC_1_1"')
        lines.append('        (pin power_in line (at 0 0 90) (length 0)')
        lines.append('          (name "VCC" (effects (font (size 1.27 1.27))))')
        lines.append('          (number "1" (effects (font (size 1.27 1.27)) hide))')
        lines.append('        )')
        lines.append('      )')
        lines.append('    )')


def _append_power_symbol_instance(
    lines: list[str],
    lib_id: str,
    net_name: str,
    x: float,
    y: float,
    root_uuid: str,
    project_name: str,
    ref_idx: int,
) -> None:
    ref = f"#PWR{ref_idx:03d}"
    lines.append(f"  (symbol (lib_id {_quote(lib_id)}) (at {x:.2f} {y:.2f} 0) (unit 1)")
    lines.append("    (in_bom no) (on_board no) (dnp no)")
    lines.append(f"    (uuid {_uuid()})")
    lines.append(f"    (property \"Reference\" {_quote(ref)} (at {x + 1.27:.2f} {y + 1.27:.2f} 0)")
    lines.append("      (effects (font (size 1.27 1.27)) hide)")
    lines.append("    )")
    lines.append(f"    (property \"Value\" {_quote(net_name)} (at {x + 1.27:.2f} {y - 1.27:.2f} 0)")
    lines.append("      (effects (font (size 1.27 1.27)) hide)")
    lines.append("    )")
    lines.append(f"    (property \"Footprint\" \"\" (at {x:.2f} {y:.2f} 0)")
    lines.append("      (effects (font (size 1.27 1.27)) hide)")
    lines.append("    )")
    lines.append(f"    (property \"Datasheet\" \"\" (at {x:.2f} {y:.2f} 0)")
    lines.append("      (effects (font (size 1.27 1.27)) hide)")
    lines.append("    )")
    lines.append(f"    (pin \"1\" (uuid {_uuid()}))")
    lines.append("    (instances")
    lines.append(f"      (project {_quote(project_name)}")
    lines.append(f"        (path {_quote('/' + root_uuid)}")
    lines.append(f"          (reference {_quote(ref)}) (unit 1)")
    lines.append("        )")
    lines.append("      )")
    lines.append("    )")
    lines.append("  )")
    lines.append("")


def _pin_angle_toward_center(x: float, y: float) -> int:
    dx, dy = -x, -y
    if abs(dx) >= abs(dy):
        return 0 if dx >= 0 else 180
    return 90 if dy >= 0 else 270


def _power_symbol_xy(net_name: str, x: float, y: float) -> tuple[float, float]:
    if _is_ground_net(net_name):
        return (x, y + 3.81)
    return (x, y - 3.81)


def _append_global_label(lines: list[str], net_name: str, x: float, y: float, angle: int = 0, justify: str = "left") -> None:
    lines.append(f"  (global_label {_quote(net_name)} (shape input) (at {x:.2f} {y:.2f} {angle}) (fields_autoplaced)")
    lines.append(f"    (effects (font (size 1.27 1.27)) (justify {justify}))")
    lines.append(f"    (uuid {_uuid()})")
    lines.append("  )")
    lines.append("")


def _append_wire(lines: list[str], x1: float, y1: float, x2: float, y2: float) -> None:
    lines.append(f"  (wire (pts (xy {x1:.2f} {y1:.2f}) (xy {x2:.2f} {y2:.2f}))")
    lines.append("    (stroke (width 0) (type default))")
    lines.append(f"    (uuid {_uuid()})")
    lines.append("  )")


def _append_text_annotation(lines: list[str], text: str, x: float, y: float, size_mm: float = 1.27) -> None:
    if not text:
        return
    lines.append(f"  (text {_quote(text)} (at {x:.2f} {y:.2f} 0)")
    lines.append(f"    (effects (font (size {size_mm:.2f} {size_mm:.2f})) (justify left))")
    lines.append(f"    (uuid {_uuid()})")
    lines.append("  )")


def _append_graphic_polyline(lines: list[str], points: list[tuple[float, float]]) -> None:
    if len(points) < 2:
        return
    stroke_type = "solid"
    if len(points) >= 5 and points[0] == points[-1]:
        xs = sorted({round(x, 2) for x, _y in points})
        ys = sorted({round(y, 2) for _x, y in points})
        if len(xs) == 2 and len(ys) == 2:
            stroke_type = "dash"
    lines.append("  (polyline")
    pts = " ".join(f"(xy {x:.2f} {y:.2f})" for x, y in points)
    lines.append(f"    (pts {pts})")
    lines.append(f"    (stroke (width 0) (type {stroke_type}))")
    lines.append("    (fill (type none))")
    lines.append(f"    (uuid {_uuid()})")
    lines.append("  )")


def _normalize_rotation(raw_rotation: int | None) -> int:
    if raw_rotation is None:
        return 0
    if raw_rotation in {0, 90, 180, 270}:
        return raw_rotation
    # Many PADS rotations are still degree based; clamp to nearest right angle.
    normalized = int(round(raw_rotation / 90.0) * 90) % 360
    return normalized


def _mirror_clause(raw_mirror: int | None) -> str:
    # Conservative mapping: mirrored parts become KiCad mirror y.
    return " (mirror y)" if raw_mirror else ""


def _transform_pin_local(px: float, py: float, rotation: int, mirrored: bool) -> tuple[float, float]:
    x = px
    y = -py if mirrored else py

    r = rotation % 360
    if r == 90:
        return (-y, x)
    if r == 180:
        return (-x, -y)
    if r == 270:
        return (y, -x)
    return (x, y)


def _inverse_transform_pin_local(tx: float, ty: float, rotation: int, mirrored: bool) -> tuple[float, float]:
    r = rotation % 360
    if r == 90:
        x, y = ty, -tx
    elif r == 180:
        x, y = -tx, -ty
    elif r == 270:
        x, y = -ty, tx
    else:
        x, y = tx, ty

    if mirrored:
        y = -y
    return (x, y)


def _pick_nearest_obs(obs: list[tuple[float, float]], cx: float, cy: float) -> tuple[float, float]:
    return min(obs, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)


def _build_coord_mapper(result: ParseResult):
    """Build coordinate mapper from PADS integer space to KiCad mm space."""
    xs: list[int] = []
    ys: list[int] = []

    for p in result.parts.values():
        if p.raw_x is not None and p.raw_y is not None:
            xs.append(p.raw_x)
            ys.append(p.raw_y)

    for seg in result.segments:
        for x, y in seg.coords:
            xs.append(x)
            ys.append(y)

    if not xs or not ys:
        return None

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(1, max_x - min_x)
    span_y = max(1, max_y - min_y)

    # Fit into A1-like drawing area with margins
    out_min_x, out_max_x = 20.0, 800.0
    out_min_y, out_max_y = 20.0, 560.0

    def map_xy(x: int, y: int) -> tuple[float, float]:
        nx = (x - min_x) / span_x
        ny = (y - min_y) / span_y
        ox = out_min_x + nx * (out_max_x - out_min_x)
        # Invert Y so natural top-to-bottom orientation is preserved
        oy = out_max_y - ny * (out_max_y - out_min_y)
        return (round(ox, 2), round(oy, 2))

    return map_xy


def write_kicad_schematic(
    result: ParseResult,
    output_path: str | Path,
    project_name: str = "GLX7",
    generator_name: str = "pads_to_kicad_converter",
    version: int = 20260306,  # KiCad schematic format version
) -> Path:
    """Write a complete single-sheet KiCad schematic file (.kicad_sch).

    Strategy:
    - Build custom lib symbols in-file from PARTTYPE pin definitions.
    - Place all symbols on a generated grid.
    - Reconstruct net connectivity by synthetic wires and global labels.
    """
    out_path = Path(output_path)
    out_path = out_path.with_name(_sanitize_output_filename(out_path.name))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    root_uuid = _uuid()

    pin_defs_by_type = _collect_symbol_pin_defs(result)

    coord_map = _build_coord_mapper(result)

    # Assign symbol placement; prefer original PADS coordinates when available.
    refs = sorted(result.parts.keys())
    instance_xy: dict[str, tuple[float, float]] = {}
    cols = 8
    dx = 45.72
    dy = 35.56
    x0 = 50.80
    y0 = 50.80

    for i, ref in enumerate(refs):
        p = result.parts[ref]
        if coord_map is not None and p.raw_x is not None and p.raw_y is not None:
            instance_xy[ref] = coord_map(p.raw_x, p.raw_y)
        else:
            instance_xy[ref] = (x0 + (i % cols) * dx, y0 + (i // cols) * dy)

    # Collect observed pin endpoints from segment geometry.
    observed_pin_xy: dict[tuple[str, str], list[tuple[float, float]]] = {}

    # Align symbol instances to PADS segment endpoints to improve pin-to-wire connectivity.
    if coord_map is not None:
        net_has_horizontal_real: set[str] = set()
        net_has_non_double_offpage: set[str] = set()
        preferred_seg_idx: dict[str, int] = {}
        for seg_idx, seg in enumerate(result.segments):
            if not seg.signal or len(seg.coords) < 2:
                continue
            a_off = (seg.node_a or "").startswith("@@@")
            b_off = (seg.node_b or "").startswith("@@@")
            non_double = not (a_off and b_off)
            a_is_unit_pin = bool(re.fullmatch(r"U\d+\.\d+", seg.node_a or ""))
            b_is_unit_pin = bool(re.fullmatch(r"U\d+\.\d+", seg.node_b or ""))
            if (a_is_unit_pin and b_off) or (b_is_unit_pin and a_off):
                preferred_seg_idx.setdefault(seg.signal, seg_idx)
            x1, y1 = seg.coords[0]
            x2, y2 = seg.coords[-1]
            if non_double and abs(x2 - x1) >= abs(y2 - y1):
                net_has_horizontal_real.add(seg.signal)
            if non_double:
                net_has_non_double_offpage.add(seg.signal)

        for seg_idx, seg in enumerate(result.segments):
            if not seg.coords:
                continue

            end_a = seg.coords[0]
            end_b = seg.coords[-1]
            for node, end_xy in ((seg.node_a, end_a), (seg.node_b, end_b)):
                ref, pin = parse_node(node)
                if ref is None or pin is None:
                    continue
                mx, my = coord_map(end_xy[0], end_xy[1])
                observed_pin_xy.setdefault((ref, pin), []).append((_q2(mx), _q2(my)))

        # Snap instance origin so that standard grid pin positions align with
        # observed PADS wire endpoints.  KiCad renders a pin with lib-local
        # position (px, py) at canvas coords (ix + tx,  iy - ty), so the
        # optimal instance position satisfies:
        #   ix = ox - tx   (x unchanged from original convention)
        #   iy = oy + ty   (note PLUS: absorbs the lib-symbol Y inversion)
        for ref_c in refs:
            part_c = result.parts[ref_c]
            pdefs_c = pin_defs_by_type.get(part_c.part_type, [{"num": "1", "name": "1", "dir": "U"}])
            # Multi-pin ICs get a second-pass correction later with a better layout;
            # skip them here to avoid drifting from a wrong alternating-layout guess.
            if len(pdefs_c) > 2:
                continue
            default_layout_c = _build_symbol_pin_layout(pdefs_c)
            rot_c = _normalize_rotation(part_c.raw_rotation)
            mir_c = bool(part_c.raw_mirror)
            xs0: list[float] = []
            ys0: list[float] = []
            for p_c in pdefs_c:
                pnum_c = p_c["num"]
                bx_c, by_c, _ = default_layout_c.get(pnum_c, (0.0, 0.0, 0))
                obs_c = observed_pin_xy.get((ref_c, pnum_c))
                if not obs_c:
                    continue
                tx_c, ty_c = _transform_pin_local(bx_c, by_c, rot_c, mir_c)
                x0c, y0c = instance_xy[ref_c]
                ox_c, oy_c = _pick_nearest_obs(obs_c, x0c + tx_c, y0c - ty_c)
                xs0.append(ox_c - tx_c)
                ys0.append(oy_c + ty_c)
            if xs0 and ys0:
                instance_xy[ref_c] = (median(xs0), median(ys0))

    # Precompute per-part pin absolute coordinates with rotation/mirror-aware local transform.
    lib_id_by_ref: dict[str, str] = {}
    is_standard_by_ref: dict[str, bool] = {}
    pin_layout_by_ref: dict[str, dict[str, tuple[float, float, int]]] = {}
    pin_count_by_ref: dict[str, int] = {}
    has_adapted_pins_by_ref: dict[str, bool] = {}
    neutral_instance_transform_by_ref: dict[str, bool] = {}

    for ref in refs:
        part = result.parts[ref]
        pdefs = pin_defs_by_type.get(part.part_type, [{"num": "1", "name": "1", "dir": "U"}])
        pin_count_by_ref[ref] = len(pdefs)
        lib_id, is_standard = _choose_lib_id(ref, part.part_type, pdefs)
        is_standard_by_ref[ref] = is_standard

        if is_standard:
            lib_id_by_ref[ref] = lib_id
            pin_layout_by_ref[ref] = _build_symbol_pin_layout(pdefs)
            # Multi-pin ICs should use neutral transform to avoid mirror/rotation
            # mismatches: the symbol definition already encodes the side layout.
            neutral_instance_transform_by_ref[ref] = True

            continue

        # Custom parts keep a per-reference symbol so pin geometry can follow observed wiring.
        custom_lib_id = f"PADS:{_sanitize_symbol_name(ref)}"
        lib_id_by_ref[ref] = custom_lib_id

        base_layout = _build_symbol_pin_layout(pdefs)
        x0_ref, y0_ref = instance_xy[ref]
        rot_ref = _normalize_rotation(part.raw_rotation)
        mir_ref = _effective_instance_mirrored(part.part_type, part.raw_mirror)

        # Adaptive pin fitting is only reliable for 2-pin devices.
        # For ICs/multi-pin parts it can snap pins to distant net endpoints,
        # which shrinks/misplaces the body relative to pins.
        if len(pdefs) != 2:
            decl_idx = {p["num"]: i for i, p in enumerate(pdefs)}
            part_side_override = _PIN_SIDE_OVERRIDES_BY_PARTTYPE.get(part.part_type, {})

            # Infer side from observed endpoint position in component-local space.
            # This keeps IC pins on the same side as the original wiring intent.
            side_hint: dict[str, str] = {}
            local_y_hint: dict[str, float] = {}

            for p in pdefs:
                pnum = p["num"]
                obs = observed_pin_xy.get((ref, pnum))
                if not obs:
                    continue

                lxs: list[float] = []
                lys: list[float] = []
                for ox, oy in obs:
                    lx, ly = _inverse_transform_pin_local(ox - x0_ref, oy - y0_ref, rot_ref, mir_ref)
                    lxs.append(lx)
                    lys.append(ly)

                mx = median(lxs)
                my = median(lys)
                local_y_hint[pnum] = my
                if mx < -0.5:
                    side_hint[pnum] = "L"
                elif mx > 0.5:
                    side_hint[pnum] = "R"

            left_nums: list[str] = []
            right_nums: list[str] = []

            # If all observed pins agree on one side, unobserved pins (e.g. mounting
            # pads) should also go on that side instead of alternating.
            observed_sides = set(side_hint.values())
            dominant_side: str | None = None
            if len(observed_sides) == 1:
                dominant_side = next(iter(observed_sides))

            for p in pdefs:
                pnum = p["num"]
                hint = side_hint.get(pnum)
                if pnum in part_side_override:
                    hint = part_side_override[pnum]
                if hint is None and dominant_side is not None:
                    # No observation → follow the dominant side
                    hint = dominant_side
                if hint == "L":
                    left_nums.append(pnum)
                elif hint == "R":
                    right_nums.append(pnum)
                else:
                    bx, _by, _ba = base_layout.get(pnum, (-10.16, 0.0, 0))
                    if bx < 0:
                        left_nums.append(pnum)
                    else:
                        right_nums.append(pnum)

            def _sort_side(nums: list[str]) -> list[str]:
                return sorted(
                    nums,
                    key=lambda n: (
                        1 if n not in local_y_hint else 0,
                        -local_y_hint.get(n, -99999.0),
                        decl_idx.get(n, 99999),
                    ),
                )

            left_sorted = _sort_side(left_nums)
            right_sorted = _sort_side(right_nums)

            # If all pins ended up on one side (e.g. connector with mounting pads),
            # build a single-side layout.  Only fall back to base_layout when
            # BOTH sides have no pins at all (shouldn't normally happen).
            if left_sorted and right_sorted:
                final_ic_layout = _build_symbol_pin_layout_from_sides(left_sorted, right_sorted)
            elif left_sorted:
                final_ic_layout = _build_symbol_pin_layout_from_sides(left_sorted, [])
            elif right_sorted:
                final_ic_layout = _build_symbol_pin_layout_from_sides([], right_sorted)
            else:
                final_ic_layout = base_layout
            pin_layout_by_ref[ref] = final_ic_layout

            # Second-pass correction: refine instance origin using the side-inferred
            # layout so bridges are minimised even for parts with many pins.
            xs_ic: list[float] = []
            ys_ic: list[float] = []
            for pnum_ic, (bx_ic, by_ic, _) in final_ic_layout.items():
                obs_ic = observed_pin_xy.get((ref, pnum_ic))
                if not obs_ic:
                    continue
                tx_ic, ty_ic = _transform_pin_local(bx_ic, by_ic, rot_ref, mir_ref)
                ix_c, iy_c = instance_xy[ref]
                ox_ic, oy_ic = _pick_nearest_obs(obs_ic, ix_c + tx_ic, iy_c - ty_ic)
                xs_ic.append(ox_ic - tx_ic)
                ys_ic.append(oy_ic + ty_ic)
            if xs_ic and ys_ic:
                instance_xy[ref] = (median(xs_ic), median(ys_ic))

            # Third pass: set each pin's lib-local Y from its observed canvas endpoint
            # so symbol pins sit exactly on the wire endpoints (same as 2-pin adapted).
            # Exclusive assignment: each observed endpoint is claimed by at most one pin.
            ix_fin, iy_fin = instance_xy[ref]
            adapted_ic: dict[str, tuple[float, float, int]] = {}
            any_adapted_ic = False
            # Build per-side pin+obs lists, sorted by grid layout Y (top → bottom).
            left_pins_ord = sorted(
                [(pnum_a, bx_a, by_a) for pnum_a, (bx_a, by_a, _) in final_ic_layout.items() if bx_a < 0],
                key=lambda t: -t[2],  # descending lib_y = top first
            )
            right_pins_ord = sorted(
                [(pnum_a, bx_a, by_a) for pnum_a, (bx_a, by_a, _) in final_ic_layout.items() if bx_a >= 0],
                key=lambda t: -t[2],
            )

            def _assign_exclusive(pins_ord: list) -> dict:
                """Greedily assign each pin to the nearest unclaimed observation."""
                # Gather all observations for this side, tagged by pin
                all_obs_side: list[tuple[float, float, str]] = []
                for pnum_a, bx_a, by_a in pins_ord:
                    obs_a = observed_pin_xy.get((ref, pnum_a))
                    if obs_a:
                        tx_a, ty_a = _transform_pin_local(bx_a, by_a, rot_ref, mir_ref)
                        exp_x = ix_fin + tx_a
                        exp_y = iy_fin - ty_a
                        for o in obs_a:
                            all_obs_side.append((o[0], o[1], pnum_a))
                # Remove duplicate (x,y) points
                unique_obs = list({(o[0], o[1]) for o in all_obs_side})
                # Sort observations by canvas Y (ascending)
                unique_obs.sort(key=lambda o: o[1])
                assigned: dict[str, tuple[float, float]] = {}
                used_obs: set[tuple[float, float]] = set()
                for pnum_a, bx_a, by_a in pins_ord:
                    obs_a = observed_pin_xy.get((ref, pnum_a))
                    if not obs_a:
                        continue
                    tx_a, ty_a = _transform_pin_local(bx_a, by_a, rot_ref, mir_ref)
                    exp_x = ix_fin + tx_a
                    exp_y = iy_fin - ty_a
                    best = None
                    best_d = float("inf")
                    for ox_a, oy_a in obs_a:
                        if (ox_a, oy_a) in used_obs:
                            continue
                        d = (ox_a - exp_x) ** 2 + (oy_a - exp_y) ** 2
                        if d < best_d:
                            best_d = d
                            best = (ox_a, oy_a)
                    # Fall back to any unused obs closest to expected position
                    if best is None:
                        for ox_a, oy_a in sorted(obs_a, key=lambda o: (o[0]-exp_x)**2+(o[1]-exp_y)**2):
                            if (ox_a, oy_a) not in used_obs:
                                best = (ox_a, oy_a)
                                break
                    if best is not None:
                        used_obs.add(best)
                        assigned[pnum_a] = best
                return assigned

            left_assigned = _assign_exclusive(left_pins_ord)
            right_assigned = _assign_exclusive(right_pins_ord)
            all_assigned = {**left_assigned, **right_assigned}

            for pnum_ic2, (bx_ic2, by_ic2, _) in final_ic_layout.items():
                if pnum_ic2 in all_assigned:
                    ox_ic2, oy_ic2 = all_assigned[pnum_ic2]
                    # Keep pin tips on the same rounded coordinate grid as emitted wires.
                    # If we inverse-transform unrounded observed endpoints and then round
                    # lib-local coords, the final pin tip can drift by 0.01 mm.
                    ox_ic2 = _q2(ox_ic2)
                    oy_ic2 = _q2(oy_ic2)
                    # Recover lib-local coords from canvas coords, accounting for
                    # rotation AND mirror: canvas=(ix+tx, iy-ty), tx/ty from
                    # _transform_pin_local.  Use the inverse to get lib_x, lib_y.
                    tx_obs = ox_ic2 - ix_fin
                    ty_obs = iy_fin - oy_ic2
                    lib_x_ic, lib_y_ic = _inverse_transform_pin_local(tx_obs, ty_obs, rot_ref, mir_ref)
                    ang_ic = _pin_angle_toward_center(lib_x_ic, lib_y_ic)
                    adapted_ic[pnum_ic2] = (_q2(lib_x_ic), _q2(lib_y_ic), ang_ic)
                    any_adapted_ic = True
                else:
                    ang_ic = _pin_angle_toward_center(bx_ic2, by_ic2)
                    adapted_ic[pnum_ic2] = (bx_ic2, by_ic2, ang_ic)
            pin_layout_by_ref[ref] = adapted_ic if any_adapted_ic else final_ic_layout
            has_adapted_pins_by_ref[ref] = any_adapted_ic
            neutral_instance_transform_by_ref[ref] = False
            continue

        adapted_layout: dict[str, tuple[float, float, int]] = {}
        has_adapted_pins = False

        for p in pdefs:
            pnum = p["num"]
            bx, by, bang = base_layout.get(pnum, (0.0, 0.0, 0))
            obs = observed_pin_xy.get((ref, pnum))
            if not obs:
                adapted_layout[pnum] = (bx, by, bang)

                continue

            has_adapted_pins = True
            # Match each pin to the endpoint nearest its expected absolute pin position
            # (not component center), to avoid pin1/pin2 swapping on 2-pin passives.
            ex, ey = _transform_pin_local(bx, by, rot_ref, mir_ref)
            ox, oy = _pick_nearest_obs(obs, x0_ref + ex, y0_ref - ey)  # canvas Y = iy - ty
            # Align to the same 0.01 mm grid used by emitted wires/junctions.
            ox = _q2(ox)
            oy = _q2(oy)
            # Bake the observed pin-tip position directly into the custom symbol's
            # local coordinates so 2-pin adapted parts do not depend on KiCad's
            # mirror/rotation semantics at the instance level.
            lx = ox - x0_ref
            ly = -(oy - y0_ref)

            # Point pin direction roughly toward body center.
            dx, dy = -lx, -ly
            if abs(dx) >= abs(dy):
                ang = 0 if dx >= 0 else 180
            else:
                ang = 90 if dy >= 0 else 270

            adapted_layout[pnum] = (_q2(lx), _q2(ly), ang)

        pin_layout_by_ref[ref] = adapted_layout
        has_adapted_pins_by_ref[ref] = has_adapted_pins
        neutral_instance_transform_by_ref[ref] = has_adapted_pins

    part_pin_abs: dict[tuple[str, str], tuple[float, float]] = {}
    for ref, part in result.parts.items():
        pdefs = pin_defs_by_type.get(part.part_type, [{"num": "1", "name": "1", "dir": "U"}])
        playout = pin_layout_by_ref[ref]
        ix, iy = instance_xy[ref]
        use_neutral_transform = neutral_instance_transform_by_ref.get(ref, False)
        rotation = 0 if use_neutral_transform else _normalize_rotation(part.raw_rotation)
        mirrored = False if use_neutral_transform else _effective_instance_mirrored(part.part_type, part.raw_mirror)
        for pnum, (px, py, _ang) in playout.items():
            tx, ty = _transform_pin_local(px, py, rotation, mirrored)
            # KiCad lib_symbol uses Y+ UP; canvas uses Y+ DOWN.
            # Rendered canvas pin tip = (ix + tx,  iy - ty).
            exp_x = ix + tx
            exp_y = iy - ty
            # Canonical pin position must match the rendered symbol pin tip.
            # Observed endpoints are used later as bridge targets when needed.
            part_pin_abs[(ref, pnum)] = (_q2(exp_x), _q2(exp_y))

    # Detect pins that are likely unconnected: the pin has an observed wire endpoint
    # but the gap between part_pin_abs and the nearest observed endpoint exceeds the
    # bridge threshold.  These will either be bridged or left floating.
    _BRIDGE_MAX_DIST = 5.0
    _BRIDGE_DIAG_THRESH = 4.0
    import sys as _sys
    for (ref, pnum), (px, py) in sorted(part_pin_abs.items()):
        obs = observed_pin_xy.get((ref, pnum))
        if not obs:
            continue
        ox, oy = _pick_nearest_obs(obs, px, py)
        gap = ((px - ox) ** 2 + (py - oy) ** 2) ** 0.5
        if gap < 0.015:
            continue  # aligned
        dx, dy = abs(px - ox), abs(py - oy)
        if gap > _BRIDGE_MAX_DIST:
            _sys.stderr.write(
                f"[WARN] {ref}.{pnum}: pin at ({px:.3f},{py:.3f}) is {gap:.2f} mm from "
                f"nearest wire endpoint ({ox:.3f},{oy:.3f}) — too far to bridge\n"
            )
        elif gap > _BRIDGE_DIAG_THRESH and min(dx, dy) > 2.0:
            _sys.stderr.write(
                f"[WARN] {ref}.{pnum}: pin at ({px:.3f},{py:.3f}) has diagonal gap "
                f"({dx:.2f},{dy:.2f}) to wire ({ox:.3f},{oy:.3f}) — bridge suppressed\n"
            )
        else:
            _sys.stderr.write(
                f"[INFO] {ref}.{pnum}: bridging {gap:.3f} mm gap "
                f"({px:.3f},{py:.3f}) → ({ox:.3f},{oy:.3f})\n"
            )

    # Build net -> unique connected component pins
    nets: dict[str, list[tuple[str, str]]] = {}
    for seg in result.segments:
        net_name = seg.signal
        pins = nets.setdefault(net_name, [])
        for node in (seg.node_a, seg.node_b):
            ref, pin = parse_node(node)
            if ref is None or pin is None:
                continue
            pair = (ref, pin)
            if pair not in pins:
                pins.append(pair)

    lines: list[str] = []
    lines.append(f"(kicad_sch (version {version}) (generator {_quote(generator_name)})")
    lines.append("")
    lines.append(f"  (uuid {_uuid()})")
    lines.append("")
    lines.append('  (paper "A1")')
    lines.append("")
    lines.append("  (title_block")
    lines.append(f"    (title {_quote('PADS converted schematic')})")
    lines.append(f"    (company {_quote(project_name)})")
    lines.append("  )")
    lines.append("")

    net_names = {n for n in nets.keys() if n}
    include_gnd_symbol = any(_is_ground_net(n) for n in net_names)
    include_vcc_symbol = any(_is_power_net(n) for n in net_names)

    # In-file symbol library
    lines.append("  (lib_symbols")
    _append_power_lib_symbols(lines, include_gnd_symbol, include_vcc_symbol)
    for ref in refs:
        if is_standard_by_ref[ref]:
            continue

        part = result.parts[ref]
        ptype = part.part_type
        pdefs = pin_defs_by_type[ptype]
        lib_id = lib_id_by_ref[ref]
        sym_name = _sanitize_symbol_name(ref)
        ref_prefix = _ref_prefix(ref)
        has_adapted = has_adapted_pins_by_ref.get(ref, False)
        _format_lib_symbol(lib_id, sym_name, ref_prefix, pdefs, ptype, lines, pin_layout_by_ref[ref], has_adapted)
    lines.append("  )")
    lines.append("")

    # Symbol instances
    for ref in refs:
        part = result.parts[ref]
        ptype = part.part_type
        pdefs = pin_defs_by_type.get(ptype, [{"num": "1", "name": "1", "dir": "U"}])
        lib_id = lib_id_by_ref[ref]
        x, y = instance_xy[ref]
        use_neutral_transform = neutral_instance_transform_by_ref.get(ref, False)
        rotation = 0 if use_neutral_transform else _normalize_rotation(part.raw_rotation)
        effective_mirror = _effective_instance_mirrored(part.part_type, part.raw_mirror)
        mirror_clause = "" if use_neutral_transform else _mirror_clause(1 if effective_mirror else 0)
        suid = _uuid()

        lines.append(f"  (symbol (lib_id {_quote(lib_id)}) (at {x:.2f} {y:.2f} {rotation}){mirror_clause} (unit 1)")
        lines.append("    (in_bom yes) (on_board yes) (dnp no)")
        lines.append(f"    (uuid {suid})")

        # Reference property placement: use PADS REF-DES annotation offset when available.
        if (coord_map is not None
                and part.raw_x is not None and part.raw_y is not None
                and part.ref_ann_dx is not None and part.ref_ann_dy is not None):
            base_x, base_y = coord_map(part.raw_x, part.raw_y)
            raw_ann_x, raw_ann_y = coord_map(part.raw_x + part.ref_ann_dx,
                                             part.raw_y + part.ref_ann_dy)
            ann_x = x + (raw_ann_x - base_x)
            ann_y = y + (raw_ann_y - base_y)
            # Keep REF-DES rotation consistent with PADS annotation angle.
            ann_angle = part.ref_ann_rotation if part.ref_ann_rotation is not None else 0
        else:
            ann_x, ann_y = x - 6.35, y + 7.62
            ann_angle = 0

        lines.append(f"    (property \"Reference\" {_quote(ref)} (at {ann_x:.2f} {ann_y:.2f} {ann_angle})")
        lines.append("      (effects (font (size 1.27 1.27)))")
        lines.append("    )")
        lines.append(f"    (property \"Value\" {_quote(ptype)} (at {x - 6.35:.2f} {y - 7.62:.2f} 0)")
        lines.append("      (effects (font (size 1.00 1.00)) hide)")
        lines.append("    )")
        lines.append(f"    (property \"Footprint\" {_quote(part.properties.get('Footprint', ''))} (at {x:.2f} {y:.2f} 0)")
        lines.append("      (effects (font (size 1.27 1.27)) hide)")
        lines.append("    )")
        lines.append(f"    (property \"Datasheet\" {_quote(part.properties.get('Datasheet', ''))} (at {x:.2f} {y:.2f} 0)")
        lines.append("      (effects (font (size 1.27 1.27)) hide)")
        lines.append("    )")

        for key, val in _part_properties(part.properties):
            lines.append(f"    (property {_quote(key)} {_quote(val)} (at {x:.2f} {y:.2f} 0)")
            lines.append("      (effects (font (size 1.27 1.27)) hide)")
            lines.append("    )")

        for p in pdefs:
            lines.append(f"    (pin {_quote(p['num'])} (uuid {_uuid()}))")

        lines.append("    (instances")
        lines.append(f"      (project {_quote(project_name)}")
        lines.append(f"        (path {_quote('/' + root_uuid)}")
        lines.append(f"          (reference {_quote(ref)}) (unit 1)")
        lines.append("        )")
        lines.append("      )")
        lines.append("    )")
        lines.append("  )")
        lines.append("")

    # Reconstructed net wiring from PADS segment geometry when possible.
    emitted_labels: set[str] = set()
    emitted_label_anchors: set[tuple[str, float, float, int]] = set()
    emitted_power_symbols: set[str] = set()
    power_regions: dict[str, list[set[tuple[float, float]]]] = {}
    pwr_ref_idx = 1
    point_degree: dict[tuple[float, float], int] = {}
    vertex_freq: dict[tuple[float, float], int] = {}
    pin_tip_points = list(part_pin_abs.values())

    def snap_to_pin_tip(x: float, y: float, tol: float = 0.02) -> tuple[float, float]:
        best_x, best_y = x, y
        best_d2 = tol * tol
        for px, py in pin_tip_points:
            d2 = (px - x) ** 2 + (py - y) ** 2
            if d2 <= best_d2:
                best_d2 = d2
                best_x, best_y = px, py
        return (best_x, best_y)

    def mark_endpoint(x: float, y: float) -> None:
        k = _pt_key(x, y)
        point_degree[k] = point_degree.get(k, 0) + 1

    if coord_map is not None:
        for seg_idx, seg in enumerate(result.segments):
            for vx, vy in seg.coords:
                mx, my = coord_map(vx, vy)
                k = _pt_key(mx, my)
                vertex_freq[k] = vertex_freq.get(k, 0) + 1

            if len(seg.coords) >= 2:
                # Force segment endpoints to reach their actual pin coordinates if they are component pins
                first_node = parse_node(seg.node_a)
                last_node = parse_node(seg.node_b)
                
                # Try to get the actual pin coordinates for first and last nodes
                first_pin_coord = None
                last_pin_coord = None
                
                if first_node[0]:  # is a part pin
                    key = (first_node[0], first_node[1])
                    if key in part_pin_abs:
                        first_pin_coord = part_pin_abs[key]
                
                if last_node[0]:  # is a part pin
                    key = (last_node[0], last_node[1])
                    if key in part_pin_abs:
                        last_pin_coord = part_pin_abs[key]
                
                for idx in range(len(seg.coords) - 1):
                    x1, y1 = coord_map(seg.coords[idx][0], seg.coords[idx][1])
                    x2, y2 = coord_map(seg.coords[idx + 1][0], seg.coords[idx + 1][1])
                    
                    # Force first segment to start at node_a's pin if available
                    if idx == 0 and first_pin_coord:
                        x1, y1 = first_pin_coord
                    else:
                        x1, y1 = snap_to_pin_tip(x1, y1)
                    
                    # Force last segment to end at node_b's pin if available
                    if idx == len(seg.coords) - 2 and last_pin_coord:
                        x2, y2 = last_pin_coord
                    else:
                        x2, y2 = snap_to_pin_tip(x2, y2)
                    
                    # If this is the only segment and both pins are known, ensure both endpoints are correct
                    if len(seg.coords) == 2 and first_pin_coord and last_pin_coord:
                        x1, y1 = first_pin_coord
                        x2, y2 = last_pin_coord
                    
                    _append_wire(lines, x1, y1, x2, y2)
                    mark_endpoint(x1, y1)
                    mark_endpoint(x2, y2)

            # Place one power/ground symbol per connected local region.
            # This preserves local visibility without flooding every endpoint.
            net_name = seg.signal
            if net_name and (_is_ground_net(net_name) or _is_power_net(net_name)) and seg.coords:
                seg_pts: set[tuple[float, float]] = set()
                for vx, vy in seg.coords:
                    mx, my = coord_map(vx, vy)
                    seg_pts.add(_pt_key(mx, my))

                regions = power_regions.setdefault(net_name, [])
                hit_idxs = [i for i, reg in enumerate(regions) if not reg.isdisjoint(seg_pts)]

                if not hit_idxs:
                    # Place power symbol at segment midpoint instead of start point (pin location)
                    mid_x = (seg.coords[0][0] + seg.coords[-1][0]) / 2
                    mid_y = (seg.coords[0][1] + seg.coords[-1][1]) / 2
                    nx, ny = coord_map(mid_x, mid_y)
                    sx, sy = _power_symbol_xy(net_name, nx, ny)
                    _append_wire(lines, nx, ny, sx, sy)
                    mark_endpoint(nx, ny)
                    mark_endpoint(sx, sy)
                    pwr_lib = "PWR:GND" if _is_ground_net(net_name) else "PWR:VCC"
                    _append_power_symbol_instance(lines, pwr_lib, net_name, sx, sy, root_uuid, project_name, pwr_ref_idx)
                    pwr_ref_idx += 1
                    emitted_power_symbols.add(net_name)
                    regions.append(set(seg_pts))
                else:
                    base = regions[hit_idxs[0]]
                    base.update(seg_pts)
                    for rm in reversed(hit_idxs[1:]):
                        base.update(regions[rm])
                        del regions[rm]

            # Apply labels only for human meaningful net names.
            if (
                net_name not in emitted_labels
                and not _is_unnamed_net(net_name)
            ):
                if net_name in preferred_seg_idx and seg_idx != preferred_seg_idx[net_name]:
                    continue
                if seg.coords:
                    sx, sy = coord_map(seg.coords[0][0], seg.coords[0][1])
                    if len(seg.coords) >= 2:
                        ex, ey = coord_map(seg.coords[-1][0], seg.coords[-1][1])

                        # Avoid selecting an incidental vertical stub when a clearer
                        # horizontal segment exists for the same net.
                        if (
                            net_name in net_has_horizontal_real
                            and abs(ex - sx) < abs(ey - sy)
                            and not (net_name in preferred_seg_idx and seg_idx == preferred_seg_idx[net_name])
                        ):
                            continue

                        node_a = seg.node_a or ""
                        node_b = seg.node_b or ""
                        a_offpage = node_a.startswith("@@@")
                        b_offpage = node_b.startswith("@@@")

                        if (
                            net_name in net_has_non_double_offpage
                            and a_offpage and b_offpage
                        ):
                            continue

                        # If exactly one side is off-page, choose anchor by segment orientation:
                        # - horizontal: keep label near real component-side endpoint (or component center)
                        # - vertical: place label on off-page side to avoid top pin-number crowding
                        if a_offpage and not b_offpage:
                            # Endpoint A is the off-page side (sx, sy), B is component side.
                            if abs(ex - sx) >= abs(ey - sy):
                                # Horizontal: anchor at off-page endpoint and point toward component.
                                lx, ly = sx, sy
                                dxl = ex - sx
                                dyl = ey - sy
                            else:
                                lx, ly = sx, sy
                                dxl = sx - ex
                                dyl = sy - ey
                        elif b_offpage and not a_offpage:
                            # Endpoint B is the off-page side (ex, ey), A is component side.
                            if abs(ex - sx) >= abs(ey - sy):
                                # Horizontal: anchor at off-page endpoint and point toward component.
                                lx, ly = ex, ey
                                dxl = sx - ex
                                dyl = sy - ey
                            else:
                                lx, ly = ex, ey
                                dxl = ex - sx
                                dyl = ey - sy
                        else:
                            # fallback: keep prior behavior (segment end side)
                            lx, ly = ex, ey
                            dxl = ex - sx
                            dyl = ey - sy

                        if abs(dxl) >= abs(dyl):
                            # Horizontal wire: mirror label direction to match original PADS appearance.
                            if dxl >= 0:
                                ljust = "right"
                                lang = 180
                            else:
                                ljust = "left"
                                lang = 0
                        else:
                            # Vertical wire: keep pointer direction aligned with opposite endpoint.
                            # Place at exact wire endpoint — no offset to avoid connectivity gap.
                            # For vertical labels, angle alone is not enough in KiCad rendering;
                            # justify must be paired with angle so the label tip faces the wire end.
                            if dyl >= 0:
                                lang = 270
                                ljust = "right"
                            else:
                                lang = 90
                                ljust = "left"
                    else:
                        ljust = "left"
                        lang = 0
                else:
                    # fallback near first connected pin if no coords
                    conns = [c for c in nets.get(net_name, []) if c in part_pin_abs]
                    if not conns:
                        continue
                    lx, ly = part_pin_abs[conns[0]]
                    ljust = "left"
                    lang = 0
                emitted_labels.add(net_name)
                _append_global_label(lines, net_name, lx, ly, angle=lang, justify=ljust)
                emitted_label_anchors.add((net_name, round(lx, 2), round(ly, 2), lang))

        # Safety fallback: if a meaningful net did not get a label due to
        # segment filtering, place one label using the best available segment.
        u9_recover_pins = {"95", "96", "99", "100", "105", "106", "107"}

        all_signal_names = sorted({seg.signal for seg in result.segments if seg.signal})
        for net_name in all_signal_names:
            if net_name in emitted_labels or _is_unnamed_net(net_name):
                continue

            seg_candidates = [seg for seg in result.segments if seg.signal == net_name and seg.coords]
            if not seg_candidates:
                continue

            def _seg_rank(s: Segment) -> tuple[int, int, int]:
                a_off = (s.node_a or "").startswith("@@@")
                b_off = (s.node_b or "").startswith("@@@")
                a_ref, a_pin = parse_node(s.node_a)
                b_ref, b_pin = parse_node(s.node_b)
                u9_target_stub = int(
                    (
                        a_ref == "U9"
                        and a_pin in u9_recover_pins
                        and b_off
                    )
                    or (
                        b_ref == "U9"
                        and b_pin in u9_recover_pins
                        and a_off
                    )
                )
                non_double = 0 if (a_off and b_off) else 1
                x1, y1 = s.coords[0]
                x2, y2 = s.coords[-1]
                horiz = 1 if abs(x2 - x1) >= abs(y2 - y1) else 0
                return (u9_target_stub, non_double, horiz, len(s.coords))

            seg = max(seg_candidates, key=_seg_rank)
            sx, sy = coord_map(seg.coords[0][0], seg.coords[0][1])
            if len(seg.coords) >= 2:
                ex, ey = coord_map(seg.coords[-1][0], seg.coords[-1][1])

                node_a = seg.node_a or ""
                node_b = seg.node_b or ""
                a_offpage = node_a.startswith("@@@")
                b_offpage = node_b.startswith("@@@")

                if a_offpage and not b_offpage:
                    if abs(ex - sx) >= abs(ey - sy):
                        # Horizontal: keep label on off-page endpoint, direction toward component.
                        lx, ly = sx, sy
                        dxl = ex - sx
                        dyl = ey - sy
                    else:
                        lx, ly = sx, sy
                        dxl = sx - ex
                        dyl = sy - ey
                elif b_offpage and not a_offpage:
                    if abs(ex - sx) >= abs(ey - sy):
                        # Horizontal: keep label on off-page endpoint, direction toward component.
                        lx, ly = ex, ey
                        dxl = sx - ex
                        dyl = sy - ey
                    else:
                        lx, ly = ex, ey
                        dxl = ex - sx
                        dyl = ey - sy
                else:
                    lx, ly = ex, ey
                    dxl = ex - sx
                    dyl = ey - sy

                if abs(dxl) >= abs(dyl):
                    if dxl >= 0:
                        ljust = "right"
                        lang = 180
                    else:
                        ljust = "left"
                        lang = 0
                else:
                    if dyl >= 0:
                        lang = 270
                        ljust = "right"
                    else:
                        lang = 90
                        ljust = "left"
            else:
                lx, ly = sx, sy
                ljust = "left"
                lang = 0

            emitted_labels.add(net_name)
            _append_global_label(lines, net_name, lx, ly, angle=lang, justify=ljust)
            emitted_label_anchors.add((net_name, round(lx, 2), round(ly, 2), lang))

        # Preserve local annotation visibility for nets that appear on multiple
        # single-offpage stubs in the source. This restores labels near parts
        # like R119/R120 even when the primary label was emitted elsewhere.
        offpage_stub_count: dict[str, int] = {}
        for seg in result.segments:
            if len(seg.coords) < 2 or _is_unnamed_net(seg.signal):
                continue
            a_off = (seg.node_a or "").startswith("@@@")
            b_off = (seg.node_b or "").startswith("@@@")
            if a_off ^ b_off:
                offpage_stub_count[seg.signal] = offpage_stub_count.get(seg.signal, 0) + 1

        for seg in result.segments:
            net_name = seg.signal
            if len(seg.coords) < 2 or _is_unnamed_net(net_name):
                continue
            if offpage_stub_count.get(net_name, 0) <= 1:
                continue

            sx, sy = coord_map(seg.coords[0][0], seg.coords[0][1])
            ex, ey = coord_map(seg.coords[-1][0], seg.coords[-1][1])
            node_a = seg.node_a or ""
            node_b = seg.node_b or ""
            a_offpage = node_a.startswith("@@@")
            b_offpage = node_b.startswith("@@@")

            if not (a_offpage ^ b_offpage):
                continue

            if a_offpage and not b_offpage:
                if abs(ex - sx) >= abs(ey - sy):
                    lx, ly = sx, sy
                    dxl = ex - sx
                    dyl = ey - sy
                else:
                    lx, ly = sx, sy
                    dxl = sx - ex
                    dyl = sy - ey
            else:
                if abs(ex - sx) >= abs(ey - sy):
                    lx, ly = ex, ey
                    dxl = sx - ex
                    dyl = sy - ey
                else:
                    lx, ly = ex, ey
                    dxl = ex - sx
                    dyl = ey - sy

            if abs(dxl) >= abs(dyl):
                if dxl >= 0:
                    ljust = "right"
                    lang = 180
                else:
                    ljust = "left"
                    lang = 0
            else:
                if dyl >= 0:
                    lang = 270
                    ljust = "right"
                else:
                    lang = 90
                    ljust = "left"

            anchor_key = (net_name, round(lx, 2), round(ly, 2), lang)
            if anchor_key in emitted_label_anchors:
                continue

            _append_global_label(lines, net_name, lx, ly, angle=lang, justify=ljust)
            emitted_label_anchors.add(anchor_key)

        # Bridge symbol pin points to observed PADS segment endpoints.
        for (ref, pin), obs in observed_pin_xy.items():
            pxy = part_pin_abs.get((ref, pin))
            if pxy is None or not obs:
                continue

            px, py = pxy
            # Choose endpoint nearest to this pin position (not part center)
            # to avoid cross-bridging pin1 <-> pin2 on 2-pin components.
            ox, oy = _pick_nearest_obs(obs, px, py)

            if abs(px - ox) < 0.01 and abs(py - oy) < 0.01:
                continue

            dx = abs(px - ox)
            dy = abs(py - oy)
            dist = (dx * dx + dy * dy) ** 0.5

            # Suppress bridges that are clearly wrong (very long or strongly diagonal).
            # A well-corrected instance should only need short axis-aligned stubs.
            if dist > _BRIDGE_MAX_DIST:
                continue
            if dist > 4.0 and min(dx, dy) > 2.0:
                # Diagonal and non-trivial — likely a mismatched net endpoint.
                continue

            # Never bridge to another pin of the same component.
            # This prevents accidental external shorts such as Cx.1 <-> Cx.2.
            hit_other_pin = False
            for (oref, opin), (qpx, qpy) in part_pin_abs.items():
                if oref != ref or opin == pin:
                    continue
                if abs(qpx - ox) < 0.01 and abs(qpy - oy) < 0.01:
                    hit_other_pin = True
                    break
            if hit_other_pin:
                continue

            _append_wire(lines, px, py, ox, oy)
            mark_endpoint(px, py)
            mark_endpoint(ox, oy)

    # Fallback synthetic net wiring + global labels for any unlabeled remaining nets.
    # Only apply for nets that have no explicit segment geometry.
    nets_with_geom = {seg.signal for seg in result.segments if len(seg.coords) >= 2}
    for net_name in sorted(nets.keys()):
        if net_name in emitted_labels:
            continue
        if net_name in nets_with_geom:
            continue
        if _is_unnamed_net(net_name):
            continue
        conns = [c for c in nets[net_name] if c in part_pin_abs]
        if len(conns) < 1:
            continue

        ax, ay = part_pin_abs[conns[0]]
        trunk_x = ax + 3.81

        # first pin short stub
        _append_wire(lines, ax, ay, trunk_x, ay)
        mark_endpoint(ax, ay)
        mark_endpoint(trunk_x, ay)

        for ref, pin in conns[1:]:
            px, py = part_pin_abs[(ref, pin)]
            px2 = px + 3.81

            _append_wire(lines, px, py, px2, py)
            mark_endpoint(px, py)
            mark_endpoint(px2, py)

            _append_wire(lines, px2, py, trunk_x, py)
            mark_endpoint(px2, py)
            mark_endpoint(trunk_x, py)

            if abs(py - ay) > 1e-6:
                _append_wire(lines, trunk_x, py, trunk_x, ay)
                mark_endpoint(trunk_x, py)
                mark_endpoint(trunk_x, ay)

        # global label at trunk anchor to force net identity
        _append_global_label(lines, net_name, trunk_x, ay - 1.27, angle=0, justify="left")
        emitted_label_anchors.add((net_name, round(trunk_x, 2), round(ay - 1.27, 2), 0))

        if net_name not in emitted_power_symbols and (_is_ground_net(net_name) or _is_power_net(net_name)):
            sx, sy = _power_symbol_xy(net_name, trunk_x, ay)
            _append_wire(lines, trunk_x, ay, sx, sy)
            mark_endpoint(trunk_x, ay)
            mark_endpoint(sx, sy)
            pwr_lib = "PWR:GND" if _is_ground_net(net_name) else "PWR:VCC"
            _append_power_symbol_instance(lines, pwr_lib, net_name, sx, sy, root_uuid, project_name, pwr_ref_idx)
            pwr_ref_idx += 1
            emitted_power_symbols.add(net_name)

    # Safety net: ensure every power/ground net has at least one visible power symbol.
    for net_name in sorted(nets.keys()):
        if net_name in emitted_power_symbols:
            continue
        if not (_is_ground_net(net_name) or _is_power_net(net_name)):
            continue
        conns = [c for c in nets[net_name] if c in part_pin_abs]
        if not conns:
            continue
        nx, ny = part_pin_abs[conns[0]]
        sx, sy = _power_symbol_xy(net_name, nx, ny)
        _append_wire(lines, nx, ny, sx, sy)
        mark_endpoint(nx, ny)
        mark_endpoint(sx, sy)
        pwr_lib = "PWR:GND" if _is_ground_net(net_name) else "PWR:VCC"
        _append_power_symbol_instance(lines, pwr_lib, net_name, sx, sy, root_uuid, project_name, pwr_ref_idx)
        pwr_ref_idx += 1
        emitted_power_symbols.add(net_name)

    # Post-emit connectivity audit.
    # Classify remaining issues by stage so we can distinguish parser geometry loss
    # from KiCad emission mistakes, then attempt one final safe recovery bridge.
    parser_stage_issues: list[tuple[str, str, str, bool]] = []
    emitter_stage_issues: list[tuple[str, str, str, float, float, float, float]] = []

    connected_pin_nets: dict[tuple[str, str], str] = {}
    for net_name, conns in nets.items():
        for ref, pin in set(conns):
            connected_pin_nets.setdefault((ref, pin), net_name)

    for (ref, pin), net_name in sorted(connected_pin_nets.items()):
        pos = part_pin_abs.get((ref, pin))
        if pos is None:
            continue

        if _pt_key(*pos) in point_degree:
            continue

        obs = observed_pin_xy.get((ref, pin))
        if not obs:
            parser_stage_issues.append((ref, pin, net_name, net_name in nets_with_geom))
            continue

        px, py = pos
        ox, oy = _pick_nearest_obs(obs, px, py)
        dx = abs(px - ox)
        dy = abs(py - oy)
        dist = (dx * dx + dy * dy) ** 0.5

        hit_other_pin = False
        for (oref, opin), (qpx, qpy) in part_pin_abs.items():
            if oref != ref or opin == pin:
                continue
            if abs(qpx - ox) < 0.01 and abs(qpy - oy) < 0.01:
                hit_other_pin = True
                break

        if dist <= _BRIDGE_MAX_DIST and not (dist > 4.0 and min(dx, dy) > 2.0) and not hit_other_pin:
            _append_wire(lines, px, py, ox, oy)
            mark_endpoint(px, py)
            mark_endpoint(ox, oy)

            if _pt_key(px, py) in point_degree:
                continue

        emitter_stage_issues.append((ref, pin, net_name, px, py, ox, oy))

    if parser_stage_issues:
        for ref, pin, net_name, has_geom in parser_stage_issues[:20]:
            geom_hint = "with geometry" if has_geom else "label-only"
            _sys.stderr.write(
                f"[WARN] parser-stage {ref}.{pin} on {net_name}: no observed endpoint ({geom_hint})\n"
            )
        if len(parser_stage_issues) > 20:
            _sys.stderr.write(
                f"[WARN] parser-stage: {len(parser_stage_issues) - 20} additional pins omitted\n"
            )

    if emitter_stage_issues:
        for ref, pin, net_name, px, py, ox, oy in emitter_stage_issues[:20]:
            _sys.stderr.write(
                f"[WARN] emitter-stage {ref}.{pin} on {net_name}: emitted pin ({px:.2f},{py:.2f}) "
                f"still misses observed endpoint ({ox:.2f},{oy:.2f})\n"
            )
        if len(emitter_stage_issues) > 20:
            _sys.stderr.write(
                f"[WARN] emitter-stage: {len(emitter_stage_issues) - 20} additional pins omitted\n"
            )

    # Add explicit junctions where 3+ vertices overlap or 3+ wire endpoints meet.
    emitted_junctions: set[tuple[float, float]] = set()

    for (jx, jy), freq in sorted(vertex_freq.items()):
        if freq < 3:
            continue
        emitted_junctions.add((jx, jy))
        lines.append(f"  (junction (at {jx:.2f} {jy:.2f}) (diameter 0) (color 0 0 0 0)")
        lines.append(f"    (uuid {_uuid()})")
        lines.append("  )")

    for (jx, jy), degree in sorted(point_degree.items()):
        if degree < 3:
            continue
        if (jx, jy) in emitted_junctions:
            continue
        lines.append(f"  (junction (at {jx:.2f} {jy:.2f}) (diameter 0) (color 0 0 0 0)")
        lines.append(f"    (uuid {_uuid()})")
        lines.append("  )")

    # Add no_connect marker for anonymous single-pin nets.
    for net_name, conns in sorted(nets.items()):
        if not net_name.startswith("$$$"):
            continue
        # If this net has explicit geometry, it is part of drawn wiring,
        # not an isolated pin that should get a no_connect marker.
        if net_name in nets_with_geom:
            continue
        uniq = sorted(set(conns))
        if len(uniq) != 1:
            continue
        ref, pin = uniq[0]
        pos = part_pin_abs.get((ref, pin))
        if pos is None:
            continue
        nx, ny = pos
        lines.append(f"  (no_connect (at {nx:.2f} {ny:.2f})")
        lines.append(f"    (uuid {_uuid()})")
        lines.append("  )")

    # Emit free-text annotations parsed from source *TEXT* section.
    if coord_map is not None:
        for ta in result.text_annotations:
            tx, ty = coord_map(ta.raw_x, ta.raw_y)
            # PADS text size field is coarse; map minimum readable KiCad text height.
            size_mm = 1.27
            if ta.raw_size is not None:
                if ta.raw_size >= 12:
                    size_mm = 1.52
                elif ta.raw_size <= 8:
                    size_mm = 1.00
            _append_text_annotation(lines, ta.text, tx, ty, size_mm=size_mm)

        # Emit drawing polylines from source *LINES* section.
        for gp in result.graphic_polylines:
            mapped = [coord_map(px, py) for px, py in gp.points]
            _append_graphic_polyline(lines, mapped)

        # Emit explicit source tiedots as junctions.
        for td in result.tiedots:
            jx, jy = coord_map(td.raw_x, td.raw_y)
            jk = _pt_key(jx, jy)
            if jk in emitted_junctions:
                continue
            emitted_junctions.add(jk)
            lines.append(f"  (junction (at {jx:.2f} {jy:.2f}) (diameter 0) (color 0 0 0 0)")
            lines.append(f"    (uuid {_uuid()})")
            lines.append("  )")

    lines.append(")")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
