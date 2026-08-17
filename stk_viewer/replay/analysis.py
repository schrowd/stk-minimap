from __future__ import annotations

import csv
import math
from dataclasses import dataclass

from ..tracks.scene import CheckLine, sector_gates
from .playback import lap_frame_range


def _seg_intersect_frac(p, q, a, b) -> float | None:
    """
    Where segment p->q crosses segment a->b, as a fraction of p->q, or None.

    Standard 2D line-segment intersection by solving p + t(q-p) = a + u(b-a);
    both t and u have to land in [0, 1] for the segments to actually meet, not
    just their infinite extensions.
    """
    rx, rz = q[0] - p[0], q[1] - p[1]
    sx, sz = b[0] - a[0], b[1] - a[1]
    den = rx * sz - rz * sx
    if abs(den) < 1e-12:
        return None
    qpx, qpz = a[0] - p[0], a[1] - p[1]
    t = (qpx * sz - qpz * sx) / den
    u = (qpx * rz - qpz * rx) / den
    return t if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0 else None


@dataclass
class LapSplit:
    lap: int                            # 0-based
    sectors: list[float | None]         # per gate; None if that gate was missed
    total: float | None                 # None if the lap never finished


def compute_splits(kart: "ReplayKart", checks: list[CheckLine]) -> list[LapSplit]:
    """
    Sector times from the track's own 'activate' check lines.

    The lap line itself turns out not to be usable for this: on Hacienda its
    two segments sit at x roughly -5 and +5 astride z=0, while the actual
    racing line crosses z=0 near x=0 - between them, touching neither.  A
    normal line down the middle of the track never intersects it.  So instead
    of a fresh crossing test for lap boundaries, this uses the frame range
    that kart.lap / lap_frame_range already give (the distance-based split,
    good to roughly 0.6s), and finds each gate's first genuine geometric
    crossing inside that range.  Gate-to-gate sectors are exact; only the
    first sector (lap start) and the last (to lap end) inherit that ~0.6s.

    A gate can still be legitimately missed - a replay predating a track
    edit, a frame landing exactly on the boundary - so a miss produces None
    for that sector rather than raising or misaligning the rest of the row.
    """
    gates = sector_gates(checks)
    n = len(kart)
    if not gates or not kart.lap or n < 2:
        return []

    def first_crossing(lines, start, end):
        for i in range(start, end):
            p = (kart.x[i], kart.z[i])
            q = (kart.x[i + 1], kart.z[i + 1])
            for c in lines:
                f = _seg_intersect_frac(p, q, c.p1, c.p2)
                if f is not None:
                    return kart.time[i] + (kart.time[i + 1] - kart.time[i]) * f, i
        return None, None

    laps: list[LapSplit] = []
    for lap_no in range(max(kart.lap) + 1):
        a, b = lap_frame_range(kart, lap_no)
        if b <= a:
            continue
        lap_start_t, lap_end_t = kart.time[a], kart.time[b]

        # len(gates) + 1 sectors: start->gate0, gate0->gate1, ..., and the
        # trailing leg from the last gate to the lap boundary, which is real
        # track and has to be accounted for or the sectors undercount the lap
        sectors: list[float | None] = []
        prev_t, cursor = lap_start_t, a
        for group in gates:
            t, gi = first_crossing(group, cursor, b)
            if t is None:
                sectors.append(None)
                continue
            sectors.append(t - prev_t)
            prev_t, cursor = t, gi
        sectors.append(lap_end_t - prev_t)

        laps.append(LapSplit(lap_no, sectors, lap_end_t - lap_start_t))
    return laps


def format_splits(laps: list[LapSplit]) -> str:
    """A console-friendly splits table, with a theoretical best row."""
    if not laps:
        return "(no splits - this track has no check lines, or nothing to split)"
    n = len(laps[0].sectors)
    lines = ["Lap   " + "".join(f"{'S' + str(i + 1):<7}" for i in range(n)) + "Total"]
    best: list[float | None] = [None] * n
    for ls in laps:
        cells = []
        for i, s in enumerate(ls.sectors):
            cells.append(f"{s:6.2f} " if s is not None else " MISS  ")
            if s is not None and (best[i] is None or s < best[i]):
                best[i] = s
        tot = f"{ls.total:6.2f}" if ls.total is not None else "  ??? "
        lines.append(f"{ls.lap + 1:<4}  " + "".join(cells) + tot)
    if len(laps) > 1 and all(b is not None for b in best):
        lines.append("")
        lines.append("Theoretical best: " + "  ".join(f"{b:5.2f}" for b in best) +
                     f"   =  {sum(best):.2f}")
    return "\n".join(lines)


def write_replay_csv(path: str, entries: list[tuple[str, "ReplayKart"]]) -> None:
    """Per-frame telemetry for one or more karts, one row per kart per frame."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["kart", "time", "lap", "x", "y", "z", "speed_kmh",
                    "heading_deg", "nitro_amount", "nitro_use", "skid_level",
                    "zipper", "item_amount", "item_type", "distance"])
        for label, kart in entries:
            for i in range(len(kart)):
                w.writerow([
                    label, f"{kart.time[i]:.3f}",
                    kart.lap[i] + 1 if kart.lap else "",
                    f"{kart.x[i]:.3f}", f"{kart.y[i]:.3f}", f"{kart.z[i]:.3f}",
                    f"{kart.speed[i] * 3.6:.2f}",
                    f"{math.degrees(kart.heading[i]):.1f}" if kart.heading else "",
                    f"{kart.nitro_amount[i]:.0f}", int(kart.nitro_use[i]),
                    kart.skid_level(i), int(kart.zipper[i]),
                    kart.item_amount[i], kart.item_type[i],
                    f"{kart.distance[i]:.2f}"])


def write_splits_csv(path: str, laps: list[LapSplit]) -> None:
    if not laps:
        raise SystemExit("no splits to write - this track has no check lines, "
                         "or the replay has no completed laps")
    n = len(laps[0].sectors)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["lap"] + [f"sector_{i + 1}" for i in range(n)] + ["total"])
        for ls in laps:
            w.writerow([ls.lap + 1] +
                       [f"{s:.3f}" if s is not None else "" for s in ls.sectors] +
                       [f"{ls.total:.3f}" if ls.total is not None else ""])
