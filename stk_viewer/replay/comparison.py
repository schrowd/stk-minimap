from __future__ import annotations


def ghost_gap(kart_a: "ReplayKart", kart_b: "ReplayKart", t: float) -> float | None:
    """
    Time difference between two runs at the same point on track, positive
    meaning B is behind A.

    Comparing positions at the same *time* only says who is ahead; what a run
    is actually worth is the time difference at the same *place*, which is
    what a ghost shows you.  Returns None if B never reaches the point A has
    reached at time t.
    """
    i = kart_a.frame_at(t)
    here = kart_a.distance[i]
    t_b = kart_b.time_at_distance(here)
    if t_b is None:
        return None
    return t_b - kart_a.time[i]
