from __future__ import annotations

import math
from dataclasses import dataclass

from .graph import Graph

# --------------------------------------------------------------------------
# framing - Graph::makeMiniMap
# --------------------------------------------------------------------------

@dataclass
class Framing:
    origin_x: float       # world X at the left edge of the image
    origin_z: float       # world Z at the bottom edge of the image
    scaling: float        # pixels per world unit
    width: int
    height: int
    angle: float = 0.0    # radians, applied about (cx, cz) before projecting
    cx: float = 0.0
    cz: float = 0.0

    def spin(self, x: float, z: float) -> tuple[float, float]:
        """Rotate a world point about the pivot. Identity when angle is 0."""
        if not self.angle:
            return x, z
        dx, dz = x - self.cx, z - self.cz
        c, s = math.cos(self.angle), math.sin(self.angle)
        return self.cx + dx * c - dz * s, self.cz + dx * s + dz * c

    def to_px(self, x: float, z: float) -> tuple[float, float]:
        # everything that draws goes through here, so rotating here rotates the
        # track, the check lines and the replay overlay together
        x, z = self.spin(x, z)
        return ((x - self.origin_x) * self.scaling,
                self.height - (z - self.origin_z) * self.scaling)


def make_framing(g: Graph, size: int, fit: bool, margin: float,
                 rotate: float = 0.0) -> Framing:
    # negated so a positive angle turns the map clockwise on screen, which is
    # what "rotate right" means everywhere else; +z points up in the image, so
    # the raw maths would go the other way
    ang = math.radians(-(rotate or 0.0))
    cx = (g.bb_min[0] + g.bb_max[0]) / 2.0
    cz = (g.bb_min[2] + g.bb_max[2]) / 2.0

    if ang:
        # the rotated track needs its own extent, or it would be clipped; the
        # corners of the old box are not enough, so measure the real points
        c, s = math.cos(ang), math.sin(ang)
        xs, zs = [], []
        for n in g.nodes:
            for p in n.p:
                dx_, dz_ = p[0] - cx, p[2] - cz
                xs.append(cx + dx_ * c - dz_ * s)
                zs.append(cz + dx_ * s + dz_ * c)
        x0, x1 = min(xs), max(xs)
        z0, z1 = min(zs), max(zs)
    else:
        x0, x1 = g.bb_min[0], g.bb_max[0]
        z0, z1 = g.bb_min[2], g.bb_max[2]

    dx = x1 - x0
    dz = z1 - z0

    if fit:
        # crop to the track instead of STK's square, letterboxed view
        span = max(dx, dz, 1e-6)
        pad = span * margin
        w_world, h_world = dx + 2 * pad, dz + 2 * pad
        scaling = size / max(w_world, h_world)
        return Framing(x0 - pad, z0 - pad, scaling,
                       max(1, round(w_world * scaling)),
                       max(1, round(h_world * scaling)), ang, cx, cz)

    # STK: ortho box is range x range, anchored at bb_min on both axes
    rng = max(dx, dz, 1e-6)
    scaling = size / rng
    return Framing(x0, z0, scaling, size, size, ang, cx, cz)


def expand_framing_for_replay(fr: Framing, karts: list, margin_frac: float = 0.03,
                              ) -> Framing:
    """
    Grow the canvas to fit a replay's actual path, without touching the
    in-game-accurate part of the mapping.

    A shortcut can genuinely leave the driveline graph's bounding box - real
    example, the shipped Cocoa Temple world record dips 14.8 world units past
    the graph's min Z, which is 16.7px at the default size, enough that 7% of
    its recorded frames land outside a plain [0, height] canvas and silently
    vanish (PIL clips a Draw call that goes off-canvas rather than erroring,
    so nothing looks wrong until you look for the missing corner).

    The fix only ever adds canvas space; scaling, angle and pivot are
    untouched, so a point that was already on the map lands on the exact same
    pixel it always did - this is a translation of where "pixel (0, 0)" sits,
    not a rescale, so it doesn't reopen the "always matches the in-game
    minimap" guarantee the way --fit or --rotate legitimately do.  Returns
    the same object, unchanged, when nothing needs to grow - every replay
    that stays on the driveline, which is nearly all of them.
    """
    lo_x = fr.origin_x
    hi_x = fr.origin_x + fr.width / fr.scaling
    lo_z = fr.origin_z
    hi_z = fr.origin_z + fr.height / fr.scaling

    xs, zs = [], []
    for k in karts:
        for x, z in zip(k.x, k.z):
            xs.append(x)
            zs.append(z)
    if not xs:
        return fr
    if fr.angle:
        pts = [fr.spin(x, z) for x, z in zip(xs, zs)]
        xs = [p[0] for p in pts]
        zs = [p[1] for p in pts]

    left = max(0.0, lo_x - min(xs))
    right = max(0.0, max(xs) - hi_x)
    bottom = max(0.0, lo_z - min(zs))
    top = max(0.0, max(zs) - hi_z)
    if left == right == bottom == top == 0.0:
        return fr

    pad = margin_frac * max(fr.width, fr.height) / fr.scaling
    left, right, bottom, top = (v + pad for v in (left, right, bottom, top))

    new_origin_x = fr.origin_x - left
    new_origin_z = fr.origin_z - bottom
    new_width = max(1, round(fr.width + (left + right) * fr.scaling))
    new_height = max(1, round(fr.height + (top + bottom) * fr.scaling))
    return Framing(new_origin_x, new_origin_z, fr.scaling, new_width,
                   new_height, fr.angle, fr.cx, fr.cz)
