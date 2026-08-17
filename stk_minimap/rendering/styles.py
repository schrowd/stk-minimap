from __future__ import annotations

import argparse
import re

STYLES = {
    # 'composite' False = later layers overwrite pixels, which is what the GPU
    # does when STK renders the minimap mesh with an opaque material.
    # outline_px is the default stroke width for the style; --outline overrides.
    # 'clean' deliberately has none: an inward stroke thinner than the
    # antialiasing ramp never survives the downscale as its own colour, it just
    # stretches the ramp - so it reads as a blurred edge rather than an outline,
    # and on a thin driveline it eats the whole width.  'blueprint' needs one,
    # because its fill is translucent and would otherwise barely show.
    "exact":  dict(bg=(255, 255, 255, 0),  track=(255, 255, 255, 127),
                   outline=None,             lap=(255, 0, 0, 128), invis=None,
                   composite=False, outline_px=0.0),
    "clean":  dict(bg=(18, 22, 28, 255),    track=(232, 238, 245, 255),
                   outline=(126, 142, 163, 255), lap=(226, 74, 60, 255),
                   invis=(70, 78, 90, 255), composite=True, outline_px=0.0),
    "blueprint": dict(bg=(14, 32, 56, 255), track=(120, 190, 255, 70),
                      outline=(150, 210, 255, 255), lap=(255, 196, 84, 255),
                      invis=(60, 100, 150, 255), composite=True, outline_px=1.0),
}

# check lines: the gates that must be crossed in order, and the lap line itself
_CHECK_COLOURS = {"activate": (55, 201, 255, 255), "lap": (255, 59, 48, 255)}

# slow -> fast, used to colour the replay route
_SPEED_RAMP = ["#3b4cc0", "#5977e3", "#7b9ff9", "#c0d4f5",
               "#f2cbb7", "#f4a582", "#e26952", "#b40426"]
_KART_COLOURS = ["#ffe066", "#7ce38b", "#ff8fa3", "#8ab4ff",
                 "#d6a2ff", "#7fe3d4"]
# skid charge: 1 = skidding, 2 = yellow earned, 3 = red earned
_SKID_COLOURS = {1: "#101010", 2: "#ffd23f", 3: "#ff3b30"}
_SKID_NAMES = {0: "", 1: "skid", 2: "YELLOW SKID", 3: "RED SKID"}

# --replay / --compare on the CLI, where there's no GUI colour picker - same
# as _KART_COLOURS[:2]
_REPLAY_CLI_COLOURS = ("#ffe066", "#7ce38b")


def parse_color(s: str):
    s = s.strip()
    if s.lower() in ("none", "transparent"):
        return (0, 0, 0, 0)
    if s.startswith("#"):
        h = s[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            h += "ff"
        if len(h) != 8:
            raise argparse.ArgumentTypeError(f"bad colour {s!r}")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4, 6))
    parts = [int(p) for p in re.split(r"[\s,]+", s) if p]
    if len(parts) == 3:
        parts.append(255)
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(f"bad colour {s!r}")
    return tuple(parts)
