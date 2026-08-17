from __future__ import annotations

import argparse
import math

from PIL import Image, ImageDraw

PREVIEW = 420


def _gui_args(**over):
    """build() takes the argparse namespace, so the GUI fakes one."""
    a = argparse.Namespace(reverse=False, full_polys=False, size=512, fit=False,
                           margin=0.02, style="exact", supersample=4,
                           show_invisible=False, invert_x_z=False, outline=None,
                           title=False, background=None, no_seal=False,
                           checklines=False, rotate=0.0)
    for k, v in over.items():
        setattr(a, k, v)
    return a


def arrow_points(x: float, y: float, angle: float, r: float) -> list[float]:
    """
    A kart-shaped arrow centred near (x, y), pointing along a *screen* angle.

    Four points rather than three: the notch in the tail is what makes the
    direction obvious at the ten-or-so pixels these are drawn at.
    """
    out = []
    for dist, off in ((1.75 * r, 0.0), (1.25 * r, 2.20),
                      (0.55 * r, math.pi), (1.25 * r, -2.20)):
        out += [x + math.cos(angle + off) * dist,
                y + math.sin(angle + off) * dist]
    return out


def _mmss(t: float) -> str:
    return f"{int(t) // 60}:{t % 60:04.1f}"


def _checkerboard(size, cell=8, a=(74, 78, 84), b=(58, 61, 66)):
    """'exact' minimaps are transparent; without this the preview looks empty."""
    img = Image.new("RGB", size, a)
    d = ImageDraw.Draw(img)
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            if (x // cell + y // cell) % 2:
                d.rectangle([x, y, x + cell - 1, y + cell - 1], fill=b)
    return img
