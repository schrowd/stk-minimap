from __future__ import annotations


def lap_frame_range(kart, lap: int | None) -> tuple[int, int]:
    """Frame range [a, b] for a lap, or the whole run when lap is None."""
    if lap is None or not kart.lap:
        return 0, len(kart) - 1
    idx = [i for i, l in enumerate(kart.lap) if l == lap]
    if not idx:
        return 0, len(kart) - 1
    return idx[0], idx[-1]


def _runs_by(values, key):
    """Group consecutive equal-keyed entries into (start, end, key) runs.

    Drawing one canvas line per frame would mean thousands of items; one per
    run of the same colour keeps it in the hundreds.
    """
    out = []
    if not len(values):
        return out
    start, cur = 0, key(values[0])
    for i in range(1, len(values)):
        k = key(values[i])
        if k != cur:
            out.append((start, i, cur))
            start, cur = i, k
    out.append((start, len(values) - 1, cur))
    return out
