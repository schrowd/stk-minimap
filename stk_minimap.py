#!/usr/bin/env python3
"""
stk_minimap.py - render a SuperTuxKart minimap to a PNG.

STK does not ship minimap images. It builds them at track load time by taking
the track's *driveline graph* (race tracks) or *navmesh* (arenas / soccer
fields), pushing the quads into a mesh, and rendering that mesh with a
top-down orthographic camera into a render target texture.  See
`Graph::makeMiniMap` / `Graph::createMesh` in src/tracks/graph.cpp of stk-code.

This script reimplements that path in software:

  * quads.xml    -> race tracks   (DriveGraph)
  * navmesh.xml  -> arenas/soccer (ArenaGraph)

and reproduces the exact framing STK uses, so pixel coordinates in the output
match `Graph::mapPoint2MiniMap`:

    px = (world_x - bb_min_x) * scaling
    py = size - (world_z - bb_min_z) * scaling      # PNG rows count from top

Requires: Pillow.  numpy is used if present (nicer alpha-correct downscaling).

Examples
--------
    ./stk_minimap.py hacienda                    # look the track up in the STK data dirs
    ./stk_minimap.py ~/tracks/mytrack -o map.png
    ./stk_minimap.py cornfield_crossing --style clean --size 1024
    ./stk_minimap.py battleisland --style clean --fit
    ./stk_minimap.py --list
    ./stk_minimap.py --all -O ./minimaps --style clean
"""

from __future__ import annotations

import argparse
import glob
import math
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:
    sys.exit("This needs Pillow.  Arch:  sudo pacman -S python-pillow   "
             "(or: pip install --user pillow)")

try:
    import numpy as np
except ImportError:
    np = None


# --------------------------------------------------------------------------
# where SuperTuxKart keeps its tracks
# --------------------------------------------------------------------------

def default_track_dirs() -> list[str]:
    home = os.path.expanduser("~")
    xdg = os.environ.get("XDG_DATA_HOME", os.path.join(home, ".local", "share"))
    cands = [
        # system installs (Arch's `supertuxkart` package uses the first one)
        "/usr/share/supertuxkart/data/tracks",
        "/usr/local/share/supertuxkart/data/tracks",
        "/usr/share/games/supertuxkart/data/tracks",
        "/opt/supertuxkart/data/tracks",
        # addons downloaded in-game
        os.path.join(xdg, "supertuxkart", "addons", "tracks"),
        os.path.join(home, ".supertuxkart", "addons", "tracks"),
        # flatpak
        os.path.join(home, ".var/app/net.supertuxkart.SuperTuxKart/data/"
                           "supertuxkart/addons/tracks"),
        os.path.join(home, ".var/app/net.supertuxkart.SuperTuxKart/.local/share/"
                           "supertuxkart/addons/tracks"),
        "/var/lib/flatpak/app/net.supertuxkart.SuperTuxKart/current/active/"
        "files/share/supertuxkart/data/tracks",
        # snap
        os.path.join(home, "snap/supertuxkart/current/.local/share/"
                           "supertuxkart/addons/tracks"),
    ]
    # git/build checkouts and versioned prefixes
    for pat in ("/usr/share/supertuxkart*/data/tracks",
                "/usr/share/games/supertuxkart*/data/tracks",
                os.path.join(home, "*/stk-assets/tracks"),
                os.path.join(home, "*/supertuxkart*/data/tracks")):
        cands.extend(glob.glob(pat))

    env = os.environ.get("STK_TRACK_DIR") or os.environ.get("SUPERTUXKART_DATADIR")
    if env:
        for p in env.split(os.pathsep):
            cands.insert(0, p)
            cands.insert(1, os.path.join(p, "tracks"))
            cands.insert(2, os.path.join(p, "data", "tracks"))

    out, seen = [], set()
    for c in cands:
        c = os.path.normpath(c)
        if c not in seen and os.path.isdir(c):
            seen.add(c)
            out.append(c)
    return out


def is_track_dir(p: str) -> bool:
    return os.path.isfile(os.path.join(p, "track.xml")) or \
           os.path.isfile(os.path.join(p, "quads.xml")) or \
           os.path.isfile(os.path.join(p, "navmesh.xml"))


def find_tracks(extra_dirs: list[str]) -> dict[str, str]:
    """ident -> directory, first hit wins (addons shadow nothing, they add)."""
    found: dict[str, str] = {}
    for root in list(extra_dirs) + default_track_dirs():
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if os.path.isdir(p) and is_track_dir(p):
                found.setdefault(name, p)
    return found


# --------------------------------------------------------------------------
# tiny vector helpers (x, y, z) with y = up, matching STK
# --------------------------------------------------------------------------

Vec3 = tuple[float, float, float]


def sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def mul(a: Vec3, s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def length(a: Vec3) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def normalize(a: Vec3) -> Vec3:
    l = length(a)
    return (0.0, 1.0, 0.0) if l < 1e-9 else (a[0] / l, a[1] / l, a[2] / l)


# --------------------------------------------------------------------------
# XML helpers that mimic STK's XMLNode
# --------------------------------------------------------------------------

def read_xml(path: str) -> ET.Element:
    with open(path, "rb") as fh:
        data = fh.read()
    # a handful of (mostly addon) track files are latin-1 or have stray
    # ampersands; be forgiving, STK's parser is too.
    for enc in ("utf-8", "latin-1"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        text = re.sub(r"&(?!(?:#\d+|#x[0-9a-fA-F]+|amp|lt|gt|quot|apos);)", "&amp;", text)
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            last = exc
    raise SystemExit(f"could not parse {path}: {last}")


def get_bool(el: ET.Element, name: str, default: bool = False) -> bool:
    v = el.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "y", "yes", "t", "true")


def parse_vec3(s: str) -> Vec3:
    parts = [p for p in re.split(r"[\s,]+", s.strip()) if p]
    if len(parts) < 3:
        raise ValueError(f"bad vector {s!r}")
    return (float(parts[0]), float(parts[1]), float(parts[2]))


# --------------------------------------------------------------------------
# the graph
# --------------------------------------------------------------------------

@dataclass
class Node:
    p: list[Vec3]                 # p0..p3, STK's winding
    normal: Vec3
    invisible: bool = False
    ai_ignore: bool = False

    def vertices(self, flatten: bool = True) -> list[Vec3]:
        """Quad::getVertices - shifts the quad along its normal by 0.1."""
        eps = mul(self.normal, 0.1)
        vs = [add(q, eps) for q in self.p]
        if flatten:                              # createMesh(flatten=true)
            vs = [(v[0], 0.1, v[2]) for v in vs]
        return vs


@dataclass
class Graph:
    nodes: list[Node] = field(default_factory=list)
    bb_min: Vec3 = (1e9, 1e9, 1e9)
    bb_max: Vec3 = (-1e9, -1e9, -1e9)
    kind: str = "drive"           # "drive" | "arena"
    has_lap_line: bool = True

    def create_quad(self, p0, p1, p2, p3, invisible=False, ai_ignore=False):
        """Graph::createQuad - also the only place the bounding box grows."""
        n1 = cross(sub(p1, p0), sub(p2, p0))     # triangle3df(p0,p1,p2)
        n2 = cross(sub(p2, p0), sub(p3, p0))     # triangle3df(p0,p2,p3)
        normal = normalize(mul(add(n1, n2), -0.5))
        self.nodes.append(Node([p0, p1, p2, p3], normal, invisible, ai_ignore))
        for q in (p0, p1, p2, p3):
            self.bb_max = tuple(max(a, b) for a, b in zip(self.bb_max, q))
            self.bb_min = tuple(min(a, b) for a, b in zip(self.bb_min, q))


def load_drive_graph(quad_file: str, reverse: bool = False) -> Graph:
    """DriveGraph::load - the quads.xml half of it."""
    root = read_xml(quad_file)
    if root.tag != "quads":
        raise SystemExit(f"{quad_file}: root element is <{root.tag}>, expected <quads>")

    g = Graph(kind="drive", has_lap_line=True)

    def get_point(el: ET.Element, attr: str) -> Vec3:
        s = el.get(attr)
        if s is None:
            raise ValueError(f"quad without {attr}")
        s = s.strip()
        pos = s.find(":")
        if pos > 0:                              # "n:p" -> point p of quad n
            n_s, _, p_s = s.partition(":")
            n, p = int(float(n_s)), int(float(p_s))
            return g.nodes[n].p[p]
        return parse_vec3(s)

    for el in root:
        if el.tag == "height-testing":           # only affects physics
            continue
        if el.tag != "quad":
            continue
        try:
            pts = [get_point(el, f"p{i}") for i in range(4)]
        except (ValueError, IndexError, KeyError) as exc:
            print(f"  warning: skipping malformed quad: {exc}", file=sys.stderr)
            continue

        invisible = get_bool(el, "invisible")
        ai_ignore = get_bool(el, "ai-ignore")
        direction = (el.get("direction") or "").strip()
        if (direction == "forward" and reverse) or (direction == "reverse" and not reverse):
            invisible = True
            ai_ignore = True

        g.create_quad(*pts, invisible=invisible, ai_ignore=ai_ignore)

    return g


def load_arena_graph(navmesh_file: str, full_polys: bool = False) -> Graph:
    """ArenaGraph::loadNavmesh."""
    root = read_xml(navmesh_file)
    if root.tag != "navmesh":
        raise SystemExit(f"{navmesh_file}: root element is <{root.tag}>, expected <navmesh>")

    verts: list[Vec3] = []
    for vs in root.iter("vertices"):
        for v in vs:
            if v.tag != "vertex":
                continue
            verts.append((float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))))

    g = Graph(kind="arena", has_lap_line=False)
    for fs in root.iter("faces"):
        for f in fs:
            if f.tag != "face":
                continue
            idx = [int(i) for i in re.split(r"[\s,]+", (f.get("indices") or "").strip()) if i]
            if len(idx) < 3 or max(idx) >= len(verts):
                continue
            pts = [verts[i] for i in idx]
            if len(pts) == 3:
                pts.append(pts[2])               # degenerate 4th, as a triangle
            elif len(pts) > 4 and not full_polys:
                # STK only ever looks at the first four vertices of a face
                pts = pts[:4]
            if len(pts) == 4 or not full_polys:
                g.create_quad(*pts[:4])
            else:
                # --full-polys: fan the n-gon so nothing is clipped away
                for k in range(1, len(pts) - 1):
                    g.create_quad(pts[0], pts[k], pts[k + 1], pts[k + 1])
    return g


# --------------------------------------------------------------------------
# track directory -> graph
# --------------------------------------------------------------------------

@dataclass
class TrackInfo:
    ident: str
    directory: str
    name: str = ""
    is_arena: bool = False
    is_soccer: bool = False
    quad_name: str = "quads.xml"
    graph_name: str = "graph.xml"


def read_track_info(directory: str) -> TrackInfo:
    ident = os.path.basename(os.path.normpath(directory))
    ti = TrackInfo(ident=ident, directory=directory, name=ident)
    xml_path = os.path.join(directory, "track.xml")
    if not os.path.isfile(xml_path):
        ti.is_arena = os.path.isfile(os.path.join(directory, "navmesh.xml"))
        return ti
    try:
        root = read_xml(xml_path)
    except SystemExit:
        return ti
    ti.name = root.get("name") or ident
    ti.is_arena = get_bool(root, "arena")
    ti.is_soccer = get_bool(root, "soccer")
    for mode in root.findall("mode"):
        if (mode.get("name") or "default") in ("default", ""):
            ti.quad_name = mode.get("quads") or ti.quad_name
            ti.graph_name = mode.get("graph") or ti.graph_name
            break
    return ti


def load_graph_for_track(ti: TrackInfo, reverse: bool, full_polys: bool) -> Graph:
    navmesh = os.path.join(ti.directory, "navmesh.xml")
    quads = os.path.join(ti.directory, ti.quad_name)
    if (ti.is_arena or ti.is_soccer) and os.path.isfile(navmesh):
        return load_arena_graph(navmesh, full_polys)
    if os.path.isfile(quads):
        return load_drive_graph(quads, reverse)
    if os.path.isfile(navmesh):
        return load_arena_graph(navmesh, full_polys)
    raise SystemExit(f"{ti.directory}: no {ti.quad_name} and no navmesh.xml - "
                     f"nothing to build a minimap from")


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

    def to_px(self, x: float, z: float) -> tuple[float, float]:
        return ((x - self.origin_x) * self.scaling,
                self.height - (z - self.origin_z) * self.scaling)


def make_framing(g: Graph, size: int, fit: bool, margin: float) -> Framing:
    dx = g.bb_max[0] - g.bb_min[0]
    dz = g.bb_max[2] - g.bb_min[2]

    if fit:
        # crop to the track instead of STK's square, letterboxed view
        span = max(dx, dz, 1e-6)
        pad = span * margin
        w_world, h_world = dx + 2 * pad, dz + 2 * pad
        scaling = size / max(w_world, h_world)
        return Framing(g.bb_min[0] - pad, g.bb_min[2] - pad, scaling,
                       max(1, round(w_world * scaling)),
                       max(1, round(h_world * scaling)))

    # STK: ortho box is range x range, anchored at bb_min on both axes
    rng = max(dx, dz, 1e-6)
    scaling = size / rng
    return Framing(g.bb_min[0], g.bb_min[2], scaling, size, size)


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

STYLES = {
    # 'composite' False = later layers overwrite pixels, which is what the GPU
    # does when STK renders the minimap mesh with an opaque material.
    "exact":  dict(bg=(255, 255, 255, 0),  track=(255, 255, 255, 127),
                   outline=None,             lap=(255, 0, 0, 128), invis=None,
                   composite=False),
    "clean":  dict(bg=(18, 22, 28, 255),    track=(232, 238, 245, 255),
                   outline=(126, 142, 163, 255), lap=(226, 74, 60, 255),
                   invis=(70, 78, 90, 255), composite=True),
    "blueprint": dict(bg=(14, 32, 56, 255), track=(120, 190, 255, 70),
                      outline=(150, 210, 255, 255), lap=(255, 196, 84, 255),
                      invis=(60, 100, 150, 255), composite=True),
}


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
    a = a.reshape(h, k, w, k, 4).mean(axis=(1, 3))          # box filter
    al = a[..., 3:4] / 255.0
    rgb = np.where(al > 0, a[..., :3] / np.maximum(al, 1e-6),
                   np.asarray(bg_rgb, dtype=np.float32))
    out = np.concatenate([rgb, a[..., 3:4]], axis=-1)
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8), "RGBA")


def render(g: Graph, fr: Framing, style: str, ss: int, show_invisible: bool,
           invert_x_z: bool, outline_px: float, title: str | None,
           background: str | None, seal: bool = True) -> Image.Image:
    pal = dict(STYLES[style])
    if background:
        pal["bg"] = parse_color(background)

    ss_fr = Framing(fr.origin_x, fr.origin_z, fr.scaling * ss,
                    fr.width * ss, fr.height * ss)
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
        r = 2 * max(1, ss // 2) + 1
        track_mask = track_mask.filter(ImageFilter.MaxFilter(r)) \
                               .filter(ImageFilter.MinFilter(r))
    put(pal["track"], track_mask)

    if pal.get("outline"):
        w = max(1, int(round(outline_px * ss)))
        eroded = track_mask.filter(ImageFilter.MinFilter(2 * w + 1))
        edge = Image.composite(track_mask, Image.new("L", big, 0),
                               Image.eval(eroded, lambda v: 255 - v))
        put(pal["outline"], edge)

    lap = lap_line_polygon(g, invert_x_z)
    if lap is not None:
        put(pal.get("lap"), _draw_mask(big, [lap], ss_fr))

    out = _downscale(img, (fr.width, fr.height), pal["bg"][:3])

    if title:
        d = ImageDraw.Draw(out)
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", max(11, fr.height // 26))
        except OSError:
            font = ImageFont.load_default()
        pad = max(8, fr.height // 40)
        col = pal["outline"] or (255, 255, 255, 200)
        d.text((pad + 1, fr.height - pad + 1), title, font=font,
               fill=(0, 0, 0, 140), anchor="ls")
        d.text((pad, fr.height - pad), title, font=font, fill=col, anchor="ls")

    return out


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


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def resolve_track(arg: str, extra_dirs: list[str], tmpdirs: list[str]) -> str:
    if os.path.isdir(arg):
        return arg
    if os.path.isfile(arg) and zipfile.is_zipfile(arg):
        tmp = tempfile.mkdtemp(prefix="stk-minimap-")
        tmpdirs.append(tmp)
        with zipfile.ZipFile(arg) as z:
            z.extractall(tmp)
        if is_track_dir(tmp):
            return tmp
        for entry in sorted(os.listdir(tmp)):
            p = os.path.join(tmp, entry)
            if os.path.isdir(p) and is_track_dir(p):
                return p
        raise SystemExit(f"{arg}: no track found inside the archive")
    if os.path.isfile(arg) and arg.endswith(".xml"):
        return os.path.dirname(os.path.abspath(arg)) or "."

    tracks = find_tracks(extra_dirs)
    if arg in tracks:
        return tracks[arg]
    close = [k for k in tracks if arg.lower() in k.lower()]
    if len(close) == 1:
        return tracks[close[0]]
    msg = f"no track called {arg!r}."
    if close:
        msg += "  Did you mean: " + ", ".join(sorted(close)) + "?"
    elif tracks:
        msg += f"  {len(tracks)} tracks known - run with --list."
    else:
        msg += "  No STK data directory found; pass a path or use --data-dir."
    raise SystemExit(msg)


def build(track_dir: str, args) -> tuple[Image.Image, Framing, Graph, TrackInfo]:
    ti = read_track_info(track_dir)
    g = load_graph_for_track(ti, args.reverse, args.full_polys)
    if not g.nodes:
        raise SystemExit(f"{ti.ident}: graph is empty")
    fr = make_framing(g, args.size, args.fit, args.margin)
    title = ti.name if args.title else None
    img = render(g, fr, args.style, args.supersample, args.show_invisible,
                 args.invert_x_z, args.outline, title, args.background,
                 seal=not args.no_seal and args.style != "exact")
    return img, fr, g, ti


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Render a SuperTuxKart minimap to PNG the way the game does "
                    "at track load (from quads.xml / navmesh.xml).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples\n--------\n")[-1])
    ap.add_argument("track", nargs="?",
                    help="track id (e.g. hacienda), a track directory, or an addon .zip")
    ap.add_argument("-o", "--output", help="output PNG (default: <ident>_minimap.png)")
    ap.add_argument("-O", "--output-dir", default=".", help="output directory for --all")
    ap.add_argument("--all", action="store_true", help="render every track found")
    ap.add_argument("--list", action="store_true", help="list the tracks found and exit")
    ap.add_argument("--data-dir", action="append", default=[],
                    help="extra directory to search for tracks (repeatable)")

    ap.add_argument("-s", "--size", type=int, default=512,
                    help="output size in pixels, STK's dimension.Width (default 512)")
    ap.add_argument("--style", choices=sorted(STYLES), default="exact",
                    help="'exact' reproduces the game's texture (white on transparent, "
                         "alpha 127); 'clean' and 'blueprint' are readable presets")
    ap.add_argument("--background", help="override background colour, e.g. '#101418' or 'none'")
    ap.add_argument("--outline", type=float, default=1.0,
                    help="outline width in output pixels (clean/blueprint, default 1)")
    ap.add_argument("--title", action="store_true", help="draw the track name")
    ap.add_argument("--no-seal", action="store_true",
                    help="don't weld hairline gaps between quads (always off for "
                         "--style exact, which stays pixel-faithful)")

    ap.add_argument("--fit", action="store_true",
                    help="crop to the track instead of STK's square letterboxed view "
                         "(breaks mapPoint2MiniMap compatibility)")
    ap.add_argument("--margin", type=float, default=0.02,
                    help="padding as a fraction of the track size, --fit only")
    ap.add_argument("--supersample", type=int, default=4,
                    help="antialiasing factor (default 4; STK itself uses 2)")

    ap.add_argument("--reverse", action="store_true",
                    help="reverse mode: honours direction=\"forward|reverse\" on quads")
    ap.add_argument("--show-invisible", action="store_true",
                    help="also draw quads marked invisible (STK hides these)")
    ap.add_argument("--invert-x-z", action="store_true",
                    help="mirror X and Z, as STK does for the blue soccer team")
    ap.add_argument("--full-polys", action="store_true",
                    help="navmesh only: draw faces with >4 vertices in full "
                         "(STK truncates them to the first 4)")
    ap.add_argument("-q", "--quiet", action="store_true")

    args = ap.parse_args(argv)
    tmpdirs: list[str] = []

    try:
        if args.list:
            tracks = find_tracks(args.data_dir)
            if not tracks:
                print("No tracks found.  Searched:", file=sys.stderr)
                for d in default_track_dirs() or ["(nothing)"]:
                    print(f"  {d}", file=sys.stderr)
                return 1
            for ident, path in sorted(tracks.items()):
                ti = read_track_info(path)
                kind = "arena" if ti.is_arena else ("soccer" if ti.is_soccer else "track")
                print(f"{ident:<28} {kind:<7} {ti.name}")
            return 0

        if args.all:
            tracks = find_tracks(args.data_dir)
            if not tracks:
                raise SystemExit("no tracks found - pass --data-dir")
            os.makedirs(args.output_dir, exist_ok=True)
            ok = 0
            for ident, path in sorted(tracks.items()):
                try:
                    img, fr, g, ti = build(path, args)
                except SystemExit as exc:
                    print(f"  skip {ident}: {exc}", file=sys.stderr)
                    continue
                out = os.path.join(args.output_dir, f"{ident}_minimap.png")
                img.save(out)
                ok += 1
                if not args.quiet:
                    print(f"{out}  ({len(g.nodes)} quads, {img.width}x{img.height})")
            if not args.quiet:
                print(f"\n{ok}/{len(tracks)} tracks rendered into {args.output_dir}")
            return 0

        if not args.track:
            ap.error("give a track (or --list / --all)")

        track_dir = resolve_track(args.track, args.data_dir, tmpdirs)
        img, fr, g, ti = build(track_dir, args)
        out = args.output or f"{ti.ident}_minimap.png"
        img.save(out)

        if not args.quiet:
            vis = sum(1 for n in g.nodes if not n.invisible)
            print(f"{ti.name}  [{ti.ident}]  "
                  f"{'arena/navmesh' if g.kind == 'arena' else 'driveline'}")
            print(f"  quads      : {len(g.nodes)} ({vis} visible)")
            print(f"  bbox       : x {g.bb_min[0]:.2f}..{g.bb_max[0]:.2f}   "
                  f"y {g.bb_min[1]:.2f}..{g.bb_max[1]:.2f}   "
                  f"z {g.bb_min[2]:.2f}..{g.bb_max[2]:.2f}")
            print(f"  scaling    : {fr.scaling:.5f} px per world unit")
            print(f"  world->px  : px = (x - {fr.origin_x:.3f}) * {fr.scaling:.5f}")
            print(f"               py = {fr.height} - (z - {fr.origin_z:.3f}) * {fr.scaling:.5f}")
            print(f"  wrote      : {out}  ({img.width}x{img.height})")
            if args.style == "exact":
                print("  note       : 'exact' matches the in-game texture - white at "
                      "alpha 127 on a\n               transparent background, so it "
                      "looks blank in some viewers.\n               Try --style clean, "
                      "or --background '#101418'.")
        return 0
    finally:
        for d in tmpdirs:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
