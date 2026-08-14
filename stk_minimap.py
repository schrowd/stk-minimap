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

__version__ = "1.0.1"

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

def steam_library_roots() -> list[str]:
    """
    Steam installs games into libraries that can live on any drive; the set of
    them is listed in steamapps/libraryfolders.vdf.  Parsing it beats guessing
    drive letters, and saves hardcoding an app id that may change.
    """
    home = os.path.expanduser("~")
    vdfs = []
    if os.name == "nt":
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "PROGRAMW6432"):
            base = os.environ.get(env)
            if base:
                vdfs.append(os.path.join(base, "Steam", "steamapps",
                                         "libraryfolders.vdf"))
    elif sys.platform == "darwin":
        vdfs.append(os.path.join(home, "Library/Application Support/Steam/"
                                       "steamapps/libraryfolders.vdf"))
    else:
        vdfs += [os.path.join(home, ".steam/steam/steamapps/libraryfolders.vdf"),
                 os.path.join(home, ".local/share/Steam/steamapps/libraryfolders.vdf"),
                 os.path.join(home, ".var/app/com.valvesoftware.Steam/.local/share/"
                                    "Steam/steamapps/libraryfolders.vdf")]

    roots: list[str] = []
    for vdf in vdfs:
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        roots.append(os.path.dirname(os.path.dirname(vdf)))   # the default library
        # "path"   "D:\\SteamLibrary"
        for m in re.finditer(r'"path"\s*"([^"]+)"', text):
            roots.append(m.group(1).replace("\\\\", "\\"))
    return roots


def default_track_dirs() -> list[str]:
    home = os.path.expanduser("~")
    cands: list[str] = []
    pats: list[str] = []

    # an STK folder sitting next to this script, or next to the cwd - covers the
    # Windows portable zip, where people drop the script into the game folder
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, os.getcwd()):
        cands += [os.path.join(base, "data", "tracks"),
                  os.path.join(base, "tracks")]
        pats.append(os.path.join(base, "SuperTuxKart*", "data", "tracks"))
        pats.append(os.path.join(os.path.dirname(base), "data", "tracks"))

    if os.name == "nt":
        # installer default is C:\Program Files\SuperTuxKart <version>\
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"):
            base = os.environ.get(env)
            if base:
                pats.append(os.path.join(base, "SuperTuxKart*", "data", "tracks"))
        # in-game addons live under %APPDATA%\supertuxkart\
        for env in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands.append(os.path.join(base, "supertuxkart", "addons", "tracks"))
                pats.append(os.path.join(base, "supertuxkart*", "addons", "tracks"))
        # portable zips usually get extracted somewhere obvious
        for folder in ("Desktop", "Downloads", "Documents", "Games"):
            pats.append(os.path.join(home, folder, "SuperTuxKart*", "data", "tracks"))
        for drive in ("C:\\", "D:\\"):
            pats.append(os.path.join(drive, "SuperTuxKart*", "data", "tracks"))
            pats.append(os.path.join(drive, "Games", "SuperTuxKart*", "data", "tracks"))

    elif sys.platform == "darwin":
        pats += ["/Applications/SuperTuxKart*.app/Contents/Resources/data/tracks",
                 os.path.join(home, "Applications/SuperTuxKart*.app/Contents/"
                                    "Resources/data/tracks")]
        cands += [os.path.join(home, "Library/Application Support/SuperTuxKart/"
                                     "addons/tracks"),
                  "/Applications/SuperTuxKart.app/Contents/Resources/data/tracks"]

    else:
        xdg = os.environ.get("XDG_DATA_HOME", os.path.join(home, ".local", "share"))
        cands += [
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
        pats += ["/usr/share/supertuxkart*/data/tracks",
                 "/usr/share/games/supertuxkart*/data/tracks",
                 os.path.join(home, "*/stk-assets/tracks"),
                 os.path.join(home, "*/supertuxkart*/data/tracks")]

    # Steam, on every platform
    for root in steam_library_roots():
        pats.append(os.path.join(root, "steamapps", "common", "SuperTuxKart*",
                                 "data", "tracks"))
        pats.append(os.path.join(root, "steamapps", "common", "SuperTuxKart*",
                                 "SuperTuxKart.app", "Contents", "Resources",
                                 "data", "tracks"))

    for pat in pats:
        cands.extend(glob.glob(pat))

    env = os.environ.get("STK_TRACK_DIR") or os.environ.get("SUPERTUXKART_DATADIR")
    if env:
        for p in reversed(env.split(os.pathsep)):
            cands.insert(0, p)
            cands.insert(1, os.path.join(p, "tracks"))
            cands.insert(2, os.path.join(p, "data", "tracks"))

    out, seen = [], set()
    for c in cands:
        c = os.path.normpath(c)
        key = os.path.normcase(c)
        if key not in seen and os.path.isdir(c):
            seen.add(key)
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
        font = find_title_font(max(11, fr.height // 26))
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


# --------------------------------------------------------------------------
# GUI  (--gui, or just double-click the script on Windows)
# --------------------------------------------------------------------------

def _gui_args(**over):
    """build() takes the argparse namespace, so the GUI fakes one."""
    a = argparse.Namespace(reverse=False, full_polys=False, size=512, fit=False,
                           margin=0.02, style="exact", supersample=4,
                           show_invisible=False, invert_x_z=False, outline=1.0,
                           title=False, background=None, no_seal=False)
    for k, v in over.items():
        setattr(a, k, v)
    return a


def _checkerboard(size, cell=8, a=(74, 78, 84), b=(58, 61, 66)):
    """'exact' minimaps are transparent; without this the preview looks empty."""
    img = Image.new("RGB", size, a)
    d = ImageDraw.Draw(img)
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            if (x // cell + y // cell) % 2:
                d.rectangle([x, y, x + cell - 1, y + cell - 1], fill=b)
    return img


def run_gui(extra_dirs: list[str]) -> int:
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
    except ImportError:
        sys.exit("The GUI needs tkinter, which your Python is missing.\n"
                 "  Arch/Manjaro : sudo pacman -S tk\n"
                 "  Debian/Ubuntu: sudo apt install python3-tk\n"
                 "  Fedora       : sudo dnf install python3-tkinter\n"
                 "  macOS/Windows: reinstall Python from python.org (it bundles it)\n"
                 "The command line still works without it - see --help.")
    try:
        from PIL import ImageTk
    except ImportError:
        sys.exit("The GUI needs Pillow's ImageTk module (package python3-pil.imagetk "
                 "on Debian/Ubuntu).")
    import queue
    import threading

    PREVIEW = 420

    class App:
        def __init__(self, root):
            self.root = root
            self.q: queue.Queue = queue.Queue()
            self.tmpdirs: list[str] = []
            self.extra_dirs = list(extra_dirs)
            self.tracks = find_tracks(self.extra_dirs)
            self.current = None          # (Image, Framing, Graph, TrackInfo)
            self.busy = False
            root.title(f"STK Minimap {__version__}")
            root.minsize(880, 560)

            outer = ttk.Frame(root, padding=8)
            outer.pack(fill="both", expand=True)

            # ---- left: track list -------------------------------------
            left = ttk.Frame(outer)
            left.pack(side="left", fill="both", expand=False)
            ttk.Label(left, text="Track").pack(anchor="w")
            self.filter = tk.StringVar()
            ent = ttk.Entry(left, textvariable=self.filter, width=30)
            ent.pack(fill="x")
            ent.insert(0, "")
            self.filter.trace_add("write", lambda *_: self.refill())

            box = ttk.Frame(left)
            box.pack(fill="both", expand=True, pady=(4, 4))
            self.listbox = tk.Listbox(box, width=30, height=22,
                                      exportselection=False, activestyle="none")
            sb = ttk.Scrollbar(box, orient="vertical", command=self.listbox.yview)
            self.listbox.configure(yscrollcommand=sb.set)
            self.listbox.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            self.listbox.bind("<<ListboxSelect>>", lambda _e: self.preview())

            btns = ttk.Frame(left)
            btns.pack(fill="x")
            ttk.Button(btns, text="Add folder…",
                       command=self.add_folder).pack(side="left", expand=True, fill="x")
            ttk.Button(btns, text="Open .zip…",
                       command=self.open_zip).pack(side="left", expand=True, fill="x")

            # ---- right: preview + options -----------------------------
            right = ttk.Frame(outer, padding=(10, 0, 0, 0))
            right.pack(side="left", fill="both", expand=True)

            self.canvas = tk.Label(right, background="#2b2e33", anchor="center")
            self.canvas.pack(fill="both", expand=True)

            self.info = ttk.Label(right, text="Pick a track.", anchor="w",
                                  justify="left")
            self.info.pack(fill="x", pady=(6, 4))

            opt = ttk.LabelFrame(right, text="Options", padding=6)
            opt.pack(fill="x")

            self.style = tk.StringVar(value="clean")
            self.size = tk.IntVar(value=512)
            self.ss = tk.IntVar(value=4)
            self.v_title = tk.BooleanVar(value=False)
            self.v_invis = tk.BooleanVar(value=False)
            self.v_rev = tk.BooleanVar(value=False)
            self.v_fit = tk.BooleanVar(value=False)
            self.v_full = tk.BooleanVar(value=False)

            r1 = ttk.Frame(opt); r1.pack(fill="x", pady=2)
            ttk.Label(r1, text="Style").pack(side="left")
            cb = ttk.Combobox(r1, textvariable=self.style, width=10, state="readonly",
                              values=sorted(STYLES))
            cb.pack(side="left", padx=(4, 12))
            cb.bind("<<ComboboxSelected>>", lambda _e: self.preview())
            ttk.Label(r1, text="Size").pack(side="left")
            sz = ttk.Combobox(r1, textvariable=self.size, width=7, state="readonly",
                              values=(256, 512, 1024, 2048))
            sz.pack(side="left", padx=(4, 12))
            ttk.Label(r1, text="Quality").pack(side="left")
            ttk.Spinbox(r1, from_=1, to=8, width=4,
                        textvariable=self.ss).pack(side="left", padx=4)

            r2 = ttk.Frame(opt); r2.pack(fill="x", pady=2)
            for text, var in (("Track name", self.v_title),
                              ("Hidden quads", self.v_invis),
                              ("Reverse", self.v_rev),
                              ("Crop to track", self.v_fit),
                              ("Full n-gons", self.v_full)):
                ttk.Checkbutton(r2, text=text, variable=var,
                                command=self.preview).pack(side="left", padx=(0, 10))

            r3 = ttk.Frame(right); r3.pack(fill="x", pady=(8, 0))
            self.save_btn = ttk.Button(r3, text="Save PNG…", command=self.save)
            self.save_btn.pack(side="left")
            self.all_btn = ttk.Button(r3, text="Save every track…",
                                      command=self.save_all)
            self.all_btn.pack(side="left", padx=6)
            self.status = ttk.Label(r3, text="", anchor="e")
            self.status.pack(side="right", fill="x", expand=True)

            self.refill()
            self._poll()
            if not self.tracks:
                self.info.configure(
                    text="No SuperTuxKart tracks found.\n"
                         "Use “Add folder…” to point at your STK data\\tracks "
                         "folder, or “Open .zip…” for an addon.")
            root.protocol("WM_DELETE_WINDOW", self.quit)

        # -- helpers ------------------------------------------------------
        def quit(self):
            for d in self.tmpdirs:
                shutil.rmtree(d, ignore_errors=True)
            self.root.destroy()

        def refill(self):
            f = self.filter.get().strip().lower()
            self.shown = [k for k in sorted(self.tracks) if f in k.lower()]
            self.listbox.delete(0, "end")
            for k in self.shown:
                self.listbox.insert("end", k)

        def selected(self):
            sel = self.listbox.curselection()
            return self.shown[sel[0]] if sel else None

        def args(self, size=None, ss=None):
            return _gui_args(style=self.style.get(),
                             size=size or self.size.get(),
                             supersample=ss or self.ss.get(),
                             title=self.v_title.get(),
                             show_invisible=self.v_invis.get(),
                             reverse=self.v_rev.get(),
                             fit=self.v_fit.get(),
                             full_polys=self.v_full.get())

        def set_busy(self, on, msg=""):
            self.busy = on
            state = "disabled" if on else "normal"
            self.save_btn.configure(state=state)
            self.all_btn.configure(state=state)
            self.status.configure(text=msg)
            self.root.update_idletasks()

        # -- actions ------------------------------------------------------
        def preview(self):
            ident = self.selected()
            if not ident or self.busy:
                return
            # cheap settings: the preview only has to look right, not be final
            try:
                img, fr, g, ti = build(self.tracks[ident],
                                       self.args(size=PREVIEW, ss=2))
            except SystemExit as exc:
                self.info.configure(text=f"{ident}: {exc}")
                self.canvas.configure(image="")
                return
            self.current = (ident, ti, g, fr)
            shown = _checkerboard(img.size)
            shown.paste(img, mask=img.split()[3])
            self.photo = ImageTk.PhotoImage(shown)
            self.canvas.configure(image=self.photo)
            vis = sum(1 for n in g.nodes if not n.invisible)
            kind = "arena / navmesh" if g.kind == "arena" else "driveline"
            self.info.configure(
                text=f"{ti.name}  [{ti.ident}]   {kind}   "
                     f"{len(g.nodes)} quads ({vis} visible)\n"
                     f"px = (x − {fr.origin_x:.2f}) × {fr.scaling:.4f}     "
                     f"py = {fr.height} − (z − {fr.origin_z:.2f}) "
                     f"× {fr.scaling:.4f}")

        def _work(self, fn, done):
            """
            Tk may only be touched from the thread running the main loop, so the
            worker never calls a widget - it posts to a queue that the UI drains
            on a timer.  (Calling root.after() from the worker looks like it
            works and then raises "main thread is not in main loop".)
            """
            def run():
                try:
                    res = fn()
                except Exception as exc:                      # noqa: BLE001
                    res = exc
                self.q.put(("done", (done, res)))
            threading.Thread(target=run, daemon=True).start()

        def _poll(self):
            try:
                while True:
                    kind, payload = self.q.get_nowait()
                    if kind == "status":
                        self.status.configure(text=payload)
                    else:
                        fn, res = payload
                        fn(res)
            except queue.Empty:
                pass
            self.root.after(80, self._poll)

        def save(self):
            ident = self.selected()
            if not ident or self.busy:
                return
            path = filedialog.asksaveasfilename(
                defaultextension=".png", filetypes=[("PNG image", "*.png")],
                initialfile=f"{ident}_minimap.png")
            if not path:
                return
            self.set_busy(True, "rendering…")

            def job():
                img, _fr, _g, _ti = build(self.tracks[ident], self.args())
                img.save(path)
                return path

            def done(res):
                self.set_busy(False, "")
                if isinstance(res, Exception):
                    messagebox.showerror("Save failed", str(res))
                else:
                    self.status.configure(text=f"saved {os.path.basename(res)}")

            self._work(job, done)

        def save_all(self):
            if self.busy or not self.tracks:
                return
            folder = filedialog.askdirectory(title="Save every minimap into…")
            if not folder:
                return
            self.set_busy(True, "rendering…")
            args = self.args()
            items = sorted(self.tracks.items())

            def job():
                ok = 0
                for i, (ident, path) in enumerate(items, 1):
                    self.q.put(("status", f"{i}/{len(items)}  {ident}"))
                    try:
                        img, _fr, _g, _ti = build(path, args)
                    except SystemExit:
                        continue                     # cutscenes have no graph
                    img.save(os.path.join(folder, f"{ident}_minimap.png"))
                    ok += 1
                return ok

            def done(res):
                self.set_busy(False, "")
                if isinstance(res, Exception):
                    messagebox.showerror("Save failed", str(res))
                else:
                    self.status.configure(text=f"wrote {res} minimaps")

            self._work(job, done)

        def add_folder(self):
            d = filedialog.askdirectory(title="Folder holding STK tracks")
            if not d:
                return
            self.extra_dirs.insert(0, d)
            self.tracks = find_tracks(self.extra_dirs)
            self.refill()
            self.status.configure(text=f"{len(self.tracks)} tracks")

        def open_zip(self):
            f = filedialog.askopenfilename(title="Addon track archive",
                                           filetypes=[("Track archive", "*.zip")])
            if not f:
                return
            try:
                d = resolve_track(f, self.extra_dirs, self.tmpdirs)
            except SystemExit as exc:
                messagebox.showerror("Could not open", str(exc))
                return
            ti = read_track_info(d)
            self.tracks[ti.ident] = d
            self.refill()
            if ti.ident in self.shown:
                self.listbox.selection_clear(0, "end")
                self.listbox.selection_set(self.shown.index(ti.ident))
                self.preview()

    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    app = App(root)                      # keep a reference: the timer callbacks
    root.mainloop()                      # are the only other thing holding it
    del app
    return 0


def install_desktop_entry(quiet: bool = False) -> int:
    """
    Put a launcher in the application menu.

    GNOME Files dropped the ability to run executable text files - the
    executable-text-activation preference no longer exists - so double-clicking
    a .py there just opens an editor.  A .desktop entry is the supported way in,
    and it also gets the app into the grid, search and the dash.
    """
    if os.name == "nt" or sys.platform == "darwin":
        print("--install-desktop is for Linux/BSD desktops.  On Windows, "
              "double-click 'STK Minimap.pyw'.", file=sys.stderr)
        return 1

    def dq(s: str) -> str:
        # .desktop Exec quoting: double quotes, backslash-escaped internals
        if any(c in s for c in ' \t"\\$`'):
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return s

    data = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    apps = os.path.join(data, "applications")
    os.makedirs(apps, exist_ok=True)
    path = os.path.join(apps, "stk-minimap.desktop")

    entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name=STK Minimap\n"
        "GenericName=Minimap renderer\n"
        "Comment=Render SuperTuxKart track minimaps to PNG\n"
        f"Exec={dq(sys.executable)} {dq(os.path.abspath(__file__))} --gui\n"
        "Icon=applications-graphics\n"
        "Terminal=false\n"
        # one main category only, or the entry can show up twice in the menu
        "Categories=Graphics;2DGraphics;RasterGraphics;\n"
        "Keywords=supertuxkart;stk;minimap;track;speedrun;\n"
        f"X-Version={__version__}\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(entry)
    os.chmod(path, 0o755)

    # some desktops need the cache poked before the entry shows up
    if shutil.which("update-desktop-database"):
        os.system(f"update-desktop-database {apps!r} 2>/dev/null")

    if not quiet:
        print(f"Installed {path}\n"
              f"  'STK Minimap' should now appear in your applications list - "
              f"you may need to log out\n  and back in if it doesn't show up "
              f"straight away.\n"
              f"  Remove it again with:  rm {path!r}")
    return 0


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
    ap.add_argument("--gui", action="store_true",
                    help="open the point-and-click window (default when the script "
                         "is started with no arguments on Windows)")
    ap.add_argument("--install-desktop", action="store_true",
                    help="Linux: add 'STK Minimap' to the application menu, so you "
                         "can launch the window without a terminal")
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
    ap.add_argument("--version", action="version",
                    version=f"stk_minimap {__version__}")

    args = ap.parse_args(argv)
    tmpdirs: list[str] = []

    if args.install_desktop:
        return install_desktop_entry(args.quiet)

    # double-clicking the script in Explorer passes no arguments; a usage error
    # in a console that closes instantly is useless, so open the window instead
    if args.gui or (os.name == "nt" and not sys.argv[1:]):
        return run_gui(args.data_dir)

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
