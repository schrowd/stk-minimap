from __future__ import annotations

import glob
import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    import numpy as np
except ImportError:
    np = None


def _draw_mask(size, polys, ss_framing) -> Image.Image:
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    for poly in polys:
        pts = [ss_framing.to_px(x, z) for (x, z) in poly]
        # outline as well as fill: adjacent quads share an edge, and a fill-only
        # rasteriser leaves a hairline seam between them
        d.polygon(pts, fill=255, outline=255)
    return m


def _downscale(img: Image.Image, size: tuple[int, int],
               bg_rgb=(0, 0, 0)) -> Image.Image:
    """
    Alpha-correct supersample resolve: premultiply, box-average, unpremultiply.

    A plain RGBA resize would bleed the (arbitrary) colour of fully transparent
    pixels into the edges, and Lanczos would ring past alpha 127, which is the
    one value the 'exact' style has to hit on the nose.
    """
    if img.size == size:
        return img
    w, h = size
    k = img.width // w
    if np is None or k < 1 or img.width != w * k or img.height != h * k:
        return img.resize(size, Image.LANCZOS)

    a = np.asarray(img).astype(np.float32)
    a[..., :3] *= a[..., 3:4] / 255.0                       # premultiply
    # box filter.  Reducing the two block axes one at a time is about twice as
    # fast as mean(axis=(1, 3)), which strides awkwardly over both at once.
    # The coverage masks are binary, so the premultiplied values are exact in
    # float32 and the summation order cannot change the result.
    a = a.reshape(h, k, w, k, 4).sum(axis=1).sum(axis=2) / (k * k)
    al = a[..., 3:4] / 255.0
    rgb = np.where(al > 0, a[..., :3] / np.maximum(al, 1e-6),
                   np.asarray(bg_rgb, dtype=np.float32))
    out = np.concatenate([rgb, a[..., 3:4]], axis=-1)
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8), "RGBA")


def find_title_font(px: int):
    """
    Pillow ships only a tiny bitmap font, so --title needs a real TTF off the
    system.  No single path is portable - even Arch has no DejaVu unless the
    package is pulled in - so try a list and degrade gracefully.
    """
    names: list[str] = []
    if os.name == "nt":
        root = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
        names += [os.path.join(root, f) for f in
                  ("segoeuib.ttf", "arialbd.ttf", "tahomabd.ttf",
                   "verdanab.ttf", "calibrib.ttf")]
    elif sys.platform == "darwin":
        names += ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                  "/Library/Fonts/Arial Bold.ttf",
                  "/System/Library/Fonts/Helvetica.ttc"]
    else:
        for d in ("/usr/share/fonts", "/usr/local/share/fonts",
                  os.path.expanduser("~/.local/share/fonts"),
                  os.path.expanduser("~/.fonts")):
            if not os.path.isdir(d):
                continue
            for stem in ("DejaVuSans-Bold", "LiberationSans-Bold", "NotoSans-Bold",
                         "FreeSansBold", "Ubuntu-B", "DejaVuSans"):
                names += sorted(glob.glob(os.path.join(d, "**", stem + ".tt[fc]"),
                                          recursive=True))
    for n in names:
        try:
            return ImageFont.truetype(n, px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(px)      # Pillow >= 10.1 scales its builtin
    except TypeError:
        return ImageFont.load_default()


def _disk_offsets(r: int) -> list[tuple[int, int]]:
    return [(dx, dy) for dy in range(-r, r + 1) for dx in range(-r, r + 1)
            if dx * dx + dy * dy <= r * r]


def _morph(img: Image.Image, r: int, erode: bool) -> Image.Image:
    """
    Erode or dilate with a *disk*.

    Pillow only offers a square Min/MaxFilter, and a square structuring element
    eats sqrt(2) times further into a 45-degree edge than into a horizontal one.
    That lands as an outline whose width visibly changes with direction - 44%
    fatter on the diagonals, measured on a circle - which reads as a wobble
    along curves.  A disk is isotropic, so the outline keeps one width.

    Falls back to the square filter when numpy is missing.
    """
    if r < 1:
        return img
    if np is None:
        f = ImageFilter.MinFilter if erode else ImageFilter.MaxFilter
        return img.filter(f(2 * r + 1))

    a = np.asarray(img)
    h, w = a.shape
    fill = 255 if erode else 0
    pad = np.pad(a, r, mode="constant", constant_values=fill)
    out = np.full_like(a, fill)
    op = np.minimum if erode else np.maximum
    for dx, dy in _disk_offsets(r):
        out = op(out, pad[r + dy:r + dy + h, r + dx:r + dx + w])
    return Image.fromarray(out, "L")
