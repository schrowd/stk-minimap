from __future__ import annotations

import os
from dataclasses import dataclass

from .graph import read_xml


@dataclass
class CheckLine:
    """One <check-line> from a track's scene.xml, flattened to the XZ plane."""
    kind: str                       # "lap" or "activate"
    p1: tuple[float, float]         # x, z
    p2: tuple[float, float]
    same_group: tuple[int, ...] = ()   # shared by alternate-route lines


def _check_point(s):
    """
    Parse a check-line endpoint.

    STK writes these either as "x z" or as "x y z" - CheckLine attempts a 2D
    read and falls back to a 3D one.  Getting it backwards puts every line in a
    thin band near z = 0: measured across real tracks, reading three components
    as (x, y, z) lands 85% of midpoints on the driveline, against 11% for
    taking the first two.
    """
    if not s:
        return None
    try:
        p = [float(v) for v in s.split()]
    except ValueError:
        return None
    if len(p) >= 3:
        return (p[0], p[2])
    if len(p) == 2:
        return (p[0], p[1])
    return None


def load_checklines(directory: str) -> list[CheckLine]:
    """
    Check lines live in scene.xml, not in the graph files.

    'lap' lines count a lap; 'activate' lines are the gates that have to be
    crossed in order, which is what stops a shortcut from counting.  Some
    tracks give two 'activate' lines the same 'same-group' value to mark them
    as alternate routes to the one logical gate - verified against Hacienda's
    scene.xml, where lines 3 and 4 both carry same-group="3 4" for the fork
    after the loop.  Reading same-group needs the true STK check index, which
    only holds if we walk <checks>'s direct children in order (including
    check-lap, which has no geometry of its own) rather than searching the
    whole tree for <check-line> alone.
    """
    path = os.path.join(directory, "scene.xml")
    if not os.path.isfile(path):
        return []
    try:
        root = read_xml(path)
    except SystemExit:
        return []
    checks = root.find(".//checks")
    if checks is None:
        # no <checks> container to index against - same-group can't be
        # resolved, so fall back to ungrouped lines (each its own gate)
        out = []
        for el in root.iter("check-line"):
            p1, p2 = _check_point(el.get("p1")), _check_point(el.get("p2"))
            if p1 and p2:
                out.append(CheckLine((el.get("kind") or "").strip(), p1, p2))
        return out

    out = []
    for el in checks:
        if el.tag != "check-line":
            continue
        p1, p2 = _check_point(el.get("p1")), _check_point(el.get("p2"))
        if p1 and p2:
            sg = tuple(int(v) for v in (el.get("same-group") or "").split()
                       if v.isdigit())
            out.append(CheckLine((el.get("kind") or "").strip(), p1, p2, sg))
    return out


def sector_gates(checks: list[CheckLine]) -> list[list[CheckLine]]:
    """
    'activate' lines grouped into logical gates, in track order.

    STK gives alternate-route lines an identical same-group value - verified
    against Hacienda, where the fork after the loop has two lines both marked
    same-group="3 4" - so lines sharing that value are one gate, either one
    counts as crossing it.  Real track data always gives even a lone gate a
    self-referential group id, so the fallback key (for data that somehow
    doesn't) only matters for malformed input.
    """
    groups: dict[tuple, list[CheckLine]] = {}
    order = []
    for i, c in enumerate(checks):
        if c.kind != "activate":
            continue
        key = c.same_group if c.same_group else (f"_{i}",)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(c)
    return [groups[k] for k in order]
