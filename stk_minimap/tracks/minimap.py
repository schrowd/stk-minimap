from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw

from ..replay.parser import Replay, load_replay, replay_header
from ..replay.playback import _runs_by, lap_frame_range
from ..rendering.raster import _downscale, _draw_mask, _morph, find_title_font
from ..rendering.styles import (STYLES, _CHECK_COLOURS, _REPLAY_CLI_COLOURS,
                                _SKID_COLOURS, _SPEED_RAMP, parse_color)
from .discovery import TrackInfo, load_graph_for_track, read_track_info
from .framing import Framing, expand_framing_for_replay, make_framing
from .graph import Graph, add, length, mul, sub
from .scene import load_checklines

# --------------------------------------------------------------------------
# geometry -> 2-D polygons
# --------------------------------------------------------------------------

def node_polygons(g: Graph, show_invisible: bool, invert_x_z: bool):
    """Yields (node, [(x, z), ...]) in world space, mirroring createMesh."""
    for n in g.nodes:
        if n.invisible and not show_invisible:
            continue
        vs = n.vertices()
        if invert_x_z:
            vs = [(-v[0], v[1], -v[2]) for v in vs]
        yield n, [(v[0], v[2]) for v in vs]


def lap_line_polygon(g: Graph, invert_x_z: bool):
    """createMesh's lap line: node 0, shortened to 3% of the track's Z extent."""
    if not g.has_lap_line or not g.nodes:
        return None
    vs = g.nodes[0].vertices()
    if invert_x_z:
        vs = [(-v[0], v[1], -v[2]) for v in vs]
    v0, v1, v2, v3 = vs
    ln = (g.bb_max[2] - g.bb_min[2]) * 0.03

    dl = sub(v3, v0)
    v3 = add(v0, (0.0, 0.0, 1.0)) if length(dl) ** 2 < 0.001 else add(v0, mul(dl, ln / length(dl)))
    dr = sub(v2, v1)
    v2 = add(v1, (0.0, 0.0, 1.0)) if length(dr) ** 2 < 0.001 else add(v1, mul(dr, ln / length(dr)))

    return [(v[0], v[2]) for v in (v0, v1, v2, v3)]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render(g: Graph, fr: Framing, style: str, ss: int, show_invisible: bool,
           invert_x_z: bool, outline_px: float, title: str | None,
           background: str | None, seal: bool = True,
           checklines: list | None = None,
           replay: ReplayOverlay | None = None) -> Image.Image:
    pal = dict(STYLES[style])
    if background:
        pal["bg"] = parse_color(background)

    ss_fr = Framing(fr.origin_x, fr.origin_z, fr.scaling * ss,
                    fr.width * ss, fr.height * ss, fr.angle, fr.cx, fr.cz)
    big = (fr.width * ss, fr.height * ss)

    visible, hidden = [], []
    for n, poly in node_polygons(g, show_invisible=True, invert_x_z=invert_x_z):
        (hidden if n.invisible else visible).append(poly)

    img = Image.new("RGBA", big, pal["bg"])

    def put(colour, mask):
        if colour is None:
            return
        if pal.get("composite"):
            layer = Image.new("RGBA", big, (0, 0, 0, 0))
            layer.paste(colour, mask=mask)
            img.alpha_composite(layer)
        else:
            img.paste(colour, mask=mask)

    if show_invisible and hidden:
        put(pal.get("invis"), _draw_mask(big, hidden, ss_fr))

    track_mask = _draw_mask(big, visible, ss_fr)
    if seal:
        # Quad::getVertices nudges every quad along its own normal, so on sloped
        # ground neighbouring quads can miss each other by a sliver.  A
        # morphological close welds those hairlines shut before we outline.
        r = max(1, ss // 2)
        track_mask = _morph(_morph(track_mask, r, erode=False), r, erode=True)
    put(pal["track"], track_mask)

    width_px = pal.get("outline_px", 1.0) if outline_px is None else outline_px
    if pal.get("outline") and width_px > 0:
        w = max(1, int(round(width_px * ss)))
        eroded = _morph(track_mask, w, erode=True)
        edge = Image.composite(track_mask, Image.new("L", big, 0),
                               Image.eval(eroded, lambda v: 255 - v))
        put(pal["outline"], edge)

    lap = lap_line_polygon(g, invert_x_z)
    if lap is not None:
        put(pal.get("lap"), _draw_mask(big, [lap], ss_fr))

    # check lines sit on top of everything: they are an annotation, not part of
    # the game's texture, and only appear when asked for
    if checklines:
        width = max(1, int(round(1.6 * ss)))
        for kind in ("activate", "lap"):
            group = [c for c in checklines if (c.kind or "activate") == kind]
            if not group:
                continue
            mask = Image.new("L", big, 0)
            d = ImageDraw.Draw(mask)
            for c in group:
                a, b = c.p1, c.p2
                if invert_x_z:
                    a, b = (-a[0], -a[1]), (-b[0], -b[1])
                d.line([ss_fr.to_px(*a), ss_fr.to_px(*b)], fill=255,
                       width=width)
            put(_CHECK_COLOURS.get(kind, _CHECK_COLOURS["activate"]), mask)

    # the replay route goes on top of everything else, same reasoning as
    # check lines: it is an annotation over the map, not part of it
    if replay:
        draw_replay_overlay(img, ss_fr, replay, ss, invert_x_z)

    out = _downscale(img, (fr.width, fr.height), pal["bg"][:3])

    if title:
        d = ImageDraw.Draw(out)
        font = find_title_font(max(11, fr.height // 26))
        pad = max(8, fr.height // 40)
        col = pal["outline"] or (255, 255, 255, 200)
        d.text((pad + 1, fr.height - pad + 1), title, font=font,
               fill=(0, 0, 0, 140), anchor="ls")
        d.text((pad, fr.height - pad), title, font=font, fill=col, anchor="ls")

    return out


@dataclass
class ReplayOverlay:
    """What --replay / --compare draw onto a static render."""
    entries: list[tuple[str, object, str]]   # label, ReplayKart, base colour
    colour_mode: str = "speed"      # "speed" | "nitro" | "plain"
    lap: int | None = None          # None = every lap
    show_items: bool = True


def draw_replay_overlay(img: Image.Image, fr: Framing, overlay: ReplayOverlay,
                        ss: int, invert_x_z: bool = False) -> None:
    """
    The static route for --replay / --compare, drawn straight into the
    supersampled buffer the map itself uses, so line weight and antialiasing
    match everything else in the image.  Mirrors the GUI's live rp_draw_static
    minus the moving marker - a still image has no "now".

    invert_x_z has to be threaded through here too, or a replay rendered with
    --invert-x-z would show the track mirrored but the route not - checklines
    and the map itself already apply it, so the route can't be the odd one
    out.  Moot for every ghost .replay that actually exists today (they're
    all time-trial; the mirror only ever applies in soccer), but the render
    path should stay consistent regardless of what's plugged into it.
    """
    d = ImageDraw.Draw(img)

    def seg(pts, i0, i1, lo, hi, colour, width):
        i0, i1 = max(i0, lo), min(i1, hi)
        if i1 > i0:
            d.line(pts[i0:i1 + 1], fill=colour,
                   width=max(1, int(round(width * ss))))

    for _label, kart, base in overlay.entries:
        if len(kart) < 2:
            continue
        if invert_x_z:
            pts = [fr.to_px(-x, -z) for x, z in zip(kart.x, kart.z)]
        else:
            pts = [fr.to_px(x, z) for x, z in zip(kart.x, kart.z)]
        a, b = lap_frame_range(kart, overlay.lap)

        if overlay.colour_mode == "speed":
            top = max(kart.speed) or 1.0
            for i0, i1, bucket in _runs_by(kart.speed, lambda s:
                                          min(7, int(8 * s / top))):
                seg(pts, i0, i1, a, b, _SPEED_RAMP[bucket], 3)
        elif overlay.colour_mode == "nitro":
            seg(pts, a, b, a, b, "#4a5568", 2)
            levels = [kart.skid_level(i) for i in range(len(kart))]
            for i0, i1, lv in _runs_by(levels, lambda v: v):
                if lv >= 2:
                    seg(pts, i0, i1, a, b, _SKID_COLOURS[lv], 3)
            for i0, i1, on in _runs_by(kart.nitro_use, bool):
                if on:
                    seg(pts, i0, i1, a, b, "#39e0ff", 3)
        else:
            seg(pts, a, b, a, b, base, 2)

        if overlay.show_items:
            r = 4 * ss
            for i0, i1, on in _runs_by(kart.zipper, bool):
                if on and a <= i0 <= b:
                    x, y = pts[i0]
                    d.ellipse([x - r, y - r, x + r, y + r],
                             outline="#ffd23f", width=max(1, round(2 * ss)))
            r = 5 * ss
            for i in kart.item_uses():
                if a <= i <= b:
                    x, y = pts[i]
                    d.polygon([(x, y - r), (x + r, y), (x, y + r), (x - r, y)],
                             fill="#ff4fd8")


def _read_replay_or_die(path: str, header_only: bool = False) -> Replay:
    """A bad --replay/--compare path is a command-line typo, not a crash."""
    try:
        return replay_header(path) if header_only else load_replay(path)
    except OSError as exc:
        raise SystemExit(f"{path}: {exc.strerror or exc}")


def build(track_dir: str, args,
         extra_karts: list | None = None) -> tuple[Image.Image, Framing, Graph, TrackInfo]:
    """
    extra_karts: karts to grow the canvas for, whether or not they're drawn
    by this call.  The GUI's live view draws replay overlays itself, straight
    onto the Tk canvas, rather than through render()'s replay= parameter - but
    it still needs the same "don't clip a shortcut" framing expansion this
    function already does for --replay, so it passes the loaded replay's
    karts in here to get it without duplicating the graph/checkline loading.
    """
    ti = read_track_info(track_dir)
    g = load_graph_for_track(ti, args.reverse, args.full_polys)
    if not g.nodes:
        raise SystemExit(f"{ti.ident}: graph is empty")
    fr = make_framing(g, args.size, args.fit, args.margin,
                      getattr(args, "rotate", 0.0) or 0.0)
    title = ti.name if args.title else None
    checks = load_checklines(track_dir) if getattr(args, "checklines", False) \
        else None

    replay_overlay = None
    rp_path = getattr(args, "replay", None)
    if rp_path:
        rp = _read_replay_or_die(rp_path)
        if rp.track != ti.ident:
            raise SystemExit(f"{rp_path}: recorded on {rp.track!r}, not "
                             f"{ti.ident!r}")
        entries = [(rp.karts[0].name or rp.karts[0].ident or "A",
                   rp.karts[0], _REPLAY_CLI_COLOURS[0])]
        cmp_path = getattr(args, "compare", None)
        if cmp_path:
            rp2 = _read_replay_or_die(cmp_path)
            if rp2.track != ti.ident:
                raise SystemExit(f"{cmp_path}: recorded on {rp2.track!r}, "
                                 f"not {ti.ident!r}")
            entries.append((rp2.karts[0].name or rp2.karts[0].ident or "B",
                           rp2.karts[0], _REPLAY_CLI_COLOURS[1]))
        lap_arg = str(getattr(args, "replay_lap", "all") or "all")
        if lap_arg.lower() == "all":
            lap = None
        else:
            try:
                lap = int(lap_arg) - 1
            except ValueError:
                raise SystemExit(f"--replay-lap: {lap_arg!r} is not 'all' "
                                 f"or a lap number")
        replay_overlay = ReplayOverlay(
            entries, getattr(args, "replay_colour", "speed") or "speed",
            lap, True)

    # a shortcut can leave the driveline graph's bounding box - grow the
    # canvas to fit the whole recorded path rather than silently clip it
    expand_karts = list(extra_karts or [])
    if replay_overlay:
        expand_karts += [k for _l, k, _c in replay_overlay.entries]
    if expand_karts:
        fr = expand_framing_for_replay(fr, expand_karts)

    img = render(g, fr, args.style, args.supersample, args.show_invisible,
                 args.invert_x_z, args.outline, title, args.background,
                 seal=not args.no_seal and args.style != "exact",
                 checklines=checks, replay=replay_overlay)
    return img, fr, g, ti
