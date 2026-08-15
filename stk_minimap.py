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

__version__ = "1.1.1"

import argparse
import glob
import html
import json
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


# the two left-pane filters, kept here so saved settings can be validated
# against them - a hand-edited settings file should not be able to leave the
# track list permanently empty
FILTER_KINDS = ("All types", "Race", "Arena", "Soccer")
FILTER_SOURCES = ("All sources", "Built-in", "Add-ons")


def settings_path() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "stk-minimap", "settings.json")


def load_settings() -> dict:
    try:
        with open(settings_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    """Best effort - a read-only home is no reason to fail a render."""
    path = settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
    except OSError:
        pass


def track_kind(ti: TrackInfo) -> str:
    return "soccer" if ti.is_soccer else ("arena" if ti.is_arena else "race")


def track_is_addon(path: str) -> bool:
    """Add-ons live under an 'addons' directory; everything else ships with STK."""
    parts = [p.lower() for p in os.path.normpath(path).split(os.sep)]
    return "addons" in parts


def track_renderable(ti: TrackInfo) -> bool:
    """False for cutscenes and grand-prix screens, which carry no graph."""
    return (os.path.isfile(os.path.join(ti.directory, ti.quad_name)) or
            os.path.isfile(os.path.join(ti.directory, "navmesh.xml")))


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
# replay files (.replay, written by STK's ReplayRecorder)
# --------------------------------------------------------------------------
#
# Plain text.  A header of "key: value" lines, with one "kart:" line per kart
# up to "kart_list_end", then "size: N" followed by N whitespace-separated rows
# per kart, karts in the order they were listed.  26 columns per row:
#
#   0 time      1 x          2 y            3 z
#   4 qx        5 qy         6 qz           7 qw
#   8 speed     9 steer     10..13 suspension
#  14 skidding_state        15 attachment  16 nitro_amount
#  17 item_amount           18 item_type   19 special_value
#  20 distance (along the driveline)       21 nitro_usage
#  22 zipper_usage          23 skidding_effect
#  24 red_skidding          25 jumping
#
# x/y/z are world coordinates, so Framing.to_px maps them onto the minimap
# directly - the same transform the game uses for the kart markers.

REPLAY_COLUMNS = 26


def user_replay_dirs() -> list[str]:
    """Where STK saves the replays you record."""
    home = os.path.expanduser("~")
    cands = []
    if os.name == "nt":
        for env in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands.append(os.path.join(base, "supertuxkart", "replay"))
    elif sys.platform == "darwin":
        cands.append(os.path.join(home, "Library/Application Support/"
                                        "SuperTuxKart/replay"))
    else:
        xdg = os.environ.get("XDG_DATA_HOME", os.path.join(home, ".local", "share"))
        cands += [os.path.join(xdg, "supertuxkart", "replay"),
                  os.path.join(home, ".supertuxkart", "replay"),
                  os.path.join(home, ".var/app/net.supertuxkart.SuperTuxKart/"
                                     ".local/share/supertuxkart/replay"),
                  os.path.join(home, "snap/supertuxkart/current/.local/share/"
                                     "supertuxkart/replay")]
    return [c for c in cands if os.path.isdir(c)]


def shipped_replay_dirs() -> list[str]:
    """
    The world records and challenge ghosts that come with the game.

    They live in <data>/replay, right next to the <data>/tracks that track
    discovery already locates - so deriving it from there means Windows, macOS,
    Steam, flatpak and snap all work without a second set of guesses.
    """
    out, seen = [], set()
    for tracks in default_track_dirs():
        cand = os.path.join(os.path.dirname(tracks), "replay")
        key = os.path.normcase(cand)
        if key not in seen and os.path.isdir(cand):
            seen.add(key)
            out.append(cand)
    return out


def default_replay_dirs() -> list[str]:
    """Everywhere worth looking, the user's own replays first."""
    out, seen = [], set()
    for d in user_replay_dirs() + shipped_replay_dirs():
        key = os.path.normcase(os.path.normpath(d))
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def replay_source(path: str) -> str:
    """Categorise a replay by the filename STK gives it."""
    name = os.path.basename(path).lower()
    if name.startswith("wr_"):
        return "World record"
    if name.startswith("challenge_"):
        return "Challenge"
    if name.startswith("standard_"):
        return "Ghost"
    if name.startswith("benchmark"):
        return "Benchmark"
    return "Yours"


def replay_date(path: str) -> float:
    """
    Best guess at when a run was set, as a POSIX timestamp.

    STK encodes the date in the filename as <track>_<YYYYMD>_..., which is what
    you actually want: the shipped world records all carry the packaging date
    as their mtime.  The month and day are not zero-padded, so the split is
    ambiguous and has to be tried both ways.  Falls back to mtime.
    """
    stem = os.path.basename(path)
    for m in re.finditer(r"_(\d{6,8})_", stem):
        digits = m.group(1)
        year = int(digits[:4])
        rest = digits[4:]
        if not 1990 <= year <= 2100:
            continue
        for cut in (1, 2):
            if len(rest) - cut not in (1, 2):
                continue
            try:
                month, day = int(rest[:cut]), int(rest[cut:])
                import datetime
                return datetime.datetime(year, month, day).timestamp()
            except ValueError:
                continue
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def scan_replays(dirs: list[str]) -> list[Replay]:
    """Header-only scan of every replay in the given directories."""
    out = []
    seen = set()
    for d in dirs:
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for name in entries:
            if not name.lower().endswith(".replay"):
                continue
            path = os.path.join(d, name)
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            seen.add(key)
            try:
                out.append(replay_header(path))
            except OSError:
                continue
    return out


@dataclass
class ReplayKart:
    ident: str = ""                       # kart model, e.g. "puffy"
    name: str = ""                        # player name
    time: list[float] = field(default_factory=list)
    x: list[float] = field(default_factory=list)
    y: list[float] = field(default_factory=list)
    z: list[float] = field(default_factory=list)
    speed: list[float] = field(default_factory=list)
    nitro_amount: list[float] = field(default_factory=list)
    nitro_use: list[bool] = field(default_factory=list)
    zipper: list[bool] = field(default_factory=list)
    skid: list[int] = field(default_factory=list)
    skid_effect: list[float] = field(default_factory=list)
    red_skid: list[bool] = field(default_factory=list)
    item_amount: list[int] = field(default_factory=list)
    item_type: list[int] = field(default_factory=list)
    distance: list[float] = field(default_factory=list)
    lap: list[int] = field(default_factory=list)     # 0-based, per frame

    def __len__(self) -> int:
        return len(self.time)

    @property
    def duration(self) -> float:
        return self.time[-1] if self.time else 0.0

    def frame_at(self, t: float) -> int:
        """Index of the last frame at or before t (frames are time-ordered)."""
        lo, hi = 0, len(self.time) - 1
        if hi < 0:
            return 0
        if t <= self.time[0]:
            return 0
        if t >= self.time[hi]:
            return hi
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.time[mid] <= t:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def skid_level(self, i: int) -> int:
        """
        0 = not skidding, 1 = skidding but not charged, 2 = yellow, 3 = red.

        The charge shows up in 'skidding_effect', which steps 200 -> 2000 ->
        2500 through a single skid.  ('red_skidding' is the boost being spent
        afterwards, so it is already set before the next skid begins and is no
        use for colouring the charge.)
        """
        if i >= len(self.skid_effect):
            return 0
        e = self.skid_effect[i]
        if e >= 2500:
            return 3
        if e >= 2000:
            return 2
        return 1 if (i < len(self.skid) and self.skid[i]) else 0

    def time_at_distance(self, d: float) -> float | None:
        """When this kart had covered d along the track - for ghost deltas."""
        if not self.distance:
            return None
        mx, run = 0.0, []
        for v in self.distance:
            if v > mx:
                mx = v
            run.append(mx)
        if d <= run[0]:
            return self.time[0]
        if d > run[-1]:
            return None
        lo, hi = 0, len(run) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if run[mid] < d:
                lo = mid + 1
            else:
                hi = mid
        return self.time[lo]

    def item_uses(self) -> list[int]:
        """Frames where the item count dropped - i.e. something was fired."""
        out = []
        for i in range(1, len(self.item_amount)):
            if self.item_amount[i] < self.item_amount[i - 1]:
                out.append(i)
        return out


@dataclass
class Replay:
    path: str = ""
    version: str = ""
    stk_version: str = ""
    track: str = ""
    mode: str = ""
    difficulty: str = ""
    laps: int = 0
    min_time: float = 0.0
    reverse: bool = False
    info: str = ""                 # the shipped world records carry a blurb
    karts: list[ReplayKart] = field(default_factory=list)
    names: list[tuple[str, str]] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max((k.duration for k in self.karts), default=0.0)


def split_laps(kart: ReplayKart, laps: int) -> list[int]:
    """
    Per-frame lap number, from the cumulative 'distance' column.

    That column counts distance along the driveline for the whole run, not per
    lap, so equal bands of it split the laps.  It also holds a large negative
    placeholder until the kart first registers on the driveline, and blips to
    ~0 for a single frame as the lap line is crossed; running the maximum
    forward absorbs both.
    """
    n = len(kart)
    if n == 0:
        return []
    if laps <= 1:
        return [0] * n
    run, mx = [], 0.0
    for d in kart.distance:
        if d > mx:
            mx = d
        run.append(mx)
    total = run[-1]
    if total <= 0:
        return [0] * n
    band = total / laps
    return [min(laps - 1, int(v / band)) if v > 0 else 0 for v in run]


def _unescape(s: str) -> str:
    """
    STK writes player names XML-escaped, so the in-game "☆★STK★☆" lands in the
    file as "&#x2606;&#x2605;STK&#x2605;&#x2606;".  Decode it back, or the
    shipped ghosts all show up as a wall of entity codes.
    """
    return html.unescape(s) if "&" in s else s


def _parse_replay_header(lines) -> tuple[Replay, int, int]:
    """Fill a Replay from the header. Returns (replay, size, first data line)."""
    rp = Replay()
    size = 0
    start = 0
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key == "kart":
            # "kart: <model> <player name>"; the name may contain spaces
            model, _, who = val.partition(" ")
            rp.names.append((model, _unescape(who)))
        elif key == "version":
            rp.version = val
        elif key == "stk_version":
            rp.stk_version = val
        elif key == "track":
            rp.track = val
        elif key == "mode":
            rp.mode = val
        elif key == "difficulty":
            rp.difficulty = val
        elif key == "info":
            rp.info = _unescape(val)
        elif key == "laps":
            rp.laps = int(float(val or 0))
        elif key == "min_time":
            rp.min_time = float(val or 0)
        elif key == "reverse":
            rp.reverse = val not in ("", "0")
        elif key == "size":
            size = int(float(val or 0))
            start = i + 1
            break
    return rp, size, start


def replay_header(path: str) -> Replay:
    """
    Just the header, without reading the thousands of data rows behind it.

    The browser scans every replay on the machine; parsing them in full would
    mean megabytes of float conversion to fill in one table.
    """
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            out.append(line)
            if line.startswith("size:"):
                break
    rp, _size, _start = _parse_replay_header(out)
    rp.path = path
    return rp


def load_replay(path: str) -> Replay:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()

    rp, size, start = _parse_replay_header(lines)
    rp.path = path
    names = rp.names

    if not size or not start:
        raise SystemExit(f"{path}: no 'size:' line - is this a .replay file?")
    if not names:
        names = [("", "")]

    rows = []
    for raw in lines[start:]:
        p = raw.split()
        if len(p) < REPLAY_COLUMNS:
            continue
        try:
            rows.append([float(v) for v in p[:REPLAY_COLUMNS]])
        except ValueError:
            continue

    # karts are stored as consecutive blocks of `size` rows
    for n, (model, who) in enumerate(names):
        block = rows[n * size:(n + 1) * size]
        if not block:
            continue
        k = ReplayKart(ident=model, name=who)
        for r in block:
            k.time.append(r[0])
            k.x.append(r[1]); k.y.append(r[2]); k.z.append(r[3])
            k.speed.append(r[8])
            k.skid.append(int(r[14]))
            k.nitro_amount.append(r[16])
            k.item_amount.append(int(r[17]))
            k.item_type.append(int(r[18]))
            k.distance.append(r[20])
            k.nitro_use.append(r[21] != 0)
            k.zipper.append(r[22] != 0)
            k.skid_effect.append(r[23])
            k.red_skid.append(r[24] != 0)
        k.lap = split_laps(k, rp.laps)
        rp.karts.append(k)

    if not rp.karts:
        raise SystemExit(f"{path}: no usable frames")
    return rp


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
                           show_invisible=False, invert_x_z=False, outline=None,
                           title=False, background=None, no_seal=False)
    for k, v in over.items():
        setattr(a, k, v)
    return a


# slow -> fast, used to colour the replay route
_SPEED_RAMP = ["#3b4cc0", "#5977e3", "#7b9ff9", "#c0d4f5",
               "#f2cbb7", "#f4a582", "#e26952", "#b40426"]
_KART_COLOURS = ["#ffe066", "#7ce38b", "#ff8fa3", "#8ab4ff",
                 "#d6a2ff", "#7fe3d4"]
# skid charge: 1 = skidding, 2 = yellow earned, 3 = red earned
_SKID_COLOURS = {1: "#101010", 2: "#ffd23f", 3: "#ff3b30"}
_SKID_NAMES = {0: "", 1: "skid", 2: "YELLOW SKID", 3: "RED SKID"}


def _mmss(t: float) -> str:
    return f"{int(t) // 60}:{t % 60:04.1f}"


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
    import time

    PREVIEW = 420

    class App:
        def __init__(self, root):
            self.root = root
            self.q: queue.Queue = queue.Queue()
            self.tmpdirs: list[str] = []
            self.extra_dirs = list(extra_dirs)
            self.settings = load_settings()
            self.tracks = find_tracks(self.extra_dirs)
            self.meta: dict[str, dict] = {}
            self.scan_tracks()
            self.current = None          # (Image, Framing, Graph, TrackInfo)
            self.busy = False
            self.frame: Framing | None = None
            self.replay: Replay | None = None
            self.replay_b: Replay | None = None   # the one being compared
            self.rp_t = 0.0              # playback head, seconds
            self.rp_playing = False
            self.rp_last = 0.0           # monotonic clock at the last tick
            self.rp_scrubbing = False
            self.rp_marks: list = []     # canvas ids for the moving overlay
            self.rp_cache: list = []     # route projected to canvas coords
            self.rp_drawn_lap = None     # which lap the static layer shows
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

            # remembered from last time; anything unrecognised falls back, so a
            # stale or edited settings file can't hide every track
            saved = self.settings
            kind = saved.get("filter_kind")
            src = saved.get("filter_source")

            f1 = ttk.Frame(left); f1.pack(fill="x", pady=(4, 0))
            self.f_kind = tk.StringVar(
                value=kind if kind in FILTER_KINDS else FILTER_KINDS[0])
            ttk.Combobox(f1, textvariable=self.f_kind, state="readonly", width=11,
                         values=FILTER_KINDS).pack(side="left", fill="x",
                                                   expand=True)
            self.f_src = tk.StringVar(
                value=src if src in FILTER_SOURCES else FILTER_SOURCES[0])
            ttk.Combobox(f1, textvariable=self.f_src, state="readonly", width=11,
                         values=FILTER_SOURCES).pack(side="left", fill="x",
                                                     expand=True, padx=(4, 0))
            self.f_kind.trace_add("write", lambda *_: self.filters_changed())
            self.f_src.trace_add("write", lambda *_: self.filters_changed())

            self.f_names = tk.BooleanVar(value=bool(saved.get("show_names")))
            ttk.Checkbutton(left, text="Show in-game names",
                            variable=self.f_names,
                            command=self.filters_changed).pack(anchor="w",
                                                               pady=(3, 0))

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

            # a Canvas rather than a Label: replay playback moves a marker every
            # frame, and moving a canvas item beats re-rasterising the image
            self.canvas = tk.Canvas(right, background="#2b2e33", width=PREVIEW,
                                    height=PREVIEW, highlightthickness=0)
            self.canvas.pack(fill="both", expand=True)
            self.img_x = self.img_y = 0

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

            # ---- replay playback --------------------------------------
            rp = ttk.LabelFrame(right, text="Replay", padding=6)
            rp.pack(fill="x", pady=(6, 0))

            q1 = ttk.Frame(rp); q1.pack(fill="x")
            ttk.Button(q1, text="Browse replays…",
                       command=self.browse_replays).pack(side="left")
            ttk.Button(q1, text="Open replay…",
                       command=self.open_replay).pack(side="left", padx=(4, 0))
            self.rp_cmp_btn = ttk.Button(q1, text="Compare with…",
                                         command=self.open_compare,
                                         state="disabled")
            self.rp_cmp_btn.pack(side="left", padx=(4, 0))
            self.rp_info = ttk.Label(q1, text="none loaded", anchor="w")
            self.rp_info.pack(side="left", padx=8, fill="x", expand=True)

            q2 = ttk.Frame(rp); q2.pack(fill="x", pady=(5, 0))
            self.rp_back = ttk.Button(q2, text="⏮", width=3,
                                      command=self.rp_restart, state="disabled")
            self.rp_back.pack(side="left")
            self.rp_play_btn = ttk.Button(q2, text="▶", width=3,
                                          command=self.rp_toggle, state="disabled")
            self.rp_play_btn.pack(side="left", padx=(3, 8))
            self.rp_rate = tk.StringVar(value="1x")
            ttk.Combobox(q2, textvariable=self.rp_rate, width=5, state="readonly",
                         values=("0.1x", "0.25x", "0.5x", "1x", "2x",
                                 "4x")).pack(side="left")
            self.rp_pos = tk.DoubleVar(value=0.0)
            self.rp_scale = ttk.Scale(q2, from_=0.0, to=1.0, variable=self.rp_pos,
                                      command=self.rp_scrub, state="disabled")
            self.rp_scale.pack(side="left", fill="x", expand=True, padx=8)
            self.rp_clock = ttk.Label(q2, text="0:00.0 / 0:00.0", width=17,
                                      anchor="e")
            self.rp_clock.pack(side="right")

            # One "colour by" choice rather than four layers that stack: drawing
            # speed, nitro and skids on the same line at once is what made the
            # map unreadable.  Laps stack too, so default to showing just the
            # one the playhead is in.
            q3 = ttk.Frame(rp); q3.pack(fill="x", pady=(5, 0))
            ttk.Label(q3, text="Lap").pack(side="left")
            self.rp_lap = tk.StringVar(value="Follow")
            self.rp_lap_box = ttk.Combobox(q3, textvariable=self.rp_lap, width=7,
                                           state="readonly",
                                           values=("Follow", "All"))
            self.rp_lap_box.pack(side="left", padx=(4, 12))
            ttk.Label(q3, text="Colour").pack(side="left")
            self.rp_colour = tk.StringVar(value="Speed")
            ttk.Combobox(q3, textvariable=self.rp_colour, width=13,
                         state="readonly",
                         values=("Speed", "Nitro & skid",
                                 "Plain")).pack(side="left", padx=(4, 12))
            self.rp_v_items = tk.BooleanVar(value=True)
            ttk.Checkbutton(q3, text="Items / zippers", variable=self.rp_v_items,
                            command=self.rp_redraw).pack(side="left")
            self.rp_lap.trace_add("write", lambda *_: self.rp_redraw())
            self.rp_colour.trace_add("write", lambda *_: self.rp_redraw())
            self.rp_readout = ttk.Label(rp, text="", anchor="w", justify="left")
            self.rp_readout.pack(fill="x", pady=(5, 0))

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

        def scan_tracks(self):
            """
            Read every track.xml once, for the display name, the type and
            whether it is an add-on.  ~2ms for a stock install, so it can just
            happen up front rather than on every keystroke in the filter box.
            """
            self.meta = {}
            for ident, path in self.tracks.items():
                try:
                    ti = read_track_info(path)
                except SystemExit:
                    continue
                self.meta[ident] = dict(name=ti.name or ident,
                                        kind=track_kind(ti),
                                        addon=track_is_addon(path),
                                        renderable=track_renderable(ti))

        def filters_changed(self):
            self.refill()
            self.settings.update(filter_kind=self.f_kind.get(),
                                 filter_source=self.f_src.get(),
                                 show_names=bool(self.f_names.get()))
            save_settings(self.settings)

        def refill(self):
            f = self.filter.get().strip().lower()
            kind = self.f_kind.get()
            src = self.f_src.get()
            use_names = self.f_names.get()

            keep = []
            for ident in self.tracks:
                md = self.meta.get(ident)
                if not md or not md["renderable"]:
                    continue          # cutscenes and GP screens have no graph
                if kind != "All types" and md["kind"] != kind.lower():
                    continue
                if src == "Built-in" and md["addon"]:
                    continue
                if src == "Add-ons" and not md["addon"]:
                    continue
                if f and f not in ident.lower() and f not in md["name"].lower():
                    continue
                keep.append(ident)

            # sort by whatever the user is actually reading
            keep.sort(key=lambda i: (self.meta[i]["name"] if use_names
                                     else i).lower())
            self.shown = keep
            self.listbox.delete(0, "end")
            for ident in keep:
                md = self.meta[ident]
                label = md["name"] if use_names else ident
                if md["addon"]:
                    label += "  (add-on)"
                self.listbox.insert("end", label)

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
                self.canvas.delete("all")
                return
            self.current = (ident, ti, g, fr)
            self.frame = fr
            shown = _checkerboard(img.size)
            shown.paste(img, mask=img.split()[3])
            self.photo = ImageTk.PhotoImage(shown)
            self.canvas.delete("all")
            cw = self.canvas.winfo_width() or shown.width
            ch = self.canvas.winfo_height() or shown.height
            self.img_x = max(0, (cw - shown.width) // 2)
            self.img_y = max(0, (ch - shown.height) // 2)
            self.canvas.create_image(self.img_x, self.img_y, anchor="nw",
                                     image=self.photo)
            if self.replay and self.replay.track == ident:
                self.rp_draw_static()
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

        # -- replay -------------------------------------------------------
        def open_replay(self):
            dirs = default_replay_dirs()
            f = filedialog.askopenfilename(
                title="SuperTuxKart replay",
                initialdir=dirs[0] if dirs else None,
                filetypes=[("STK replay", "*.replay"), ("All files", "*")])
            if f:
                self.use_replay(f)

        def use_replay(self, f):
            """Load a replay as run A."""
            try:
                rp = load_replay(f)
            except (SystemExit, OSError, ValueError) as exc:
                messagebox.showerror("Could not read replay", str(exc))
                return
            if rp.track not in self.tracks:
                messagebox.showerror(
                    "Track not found",
                    f"This replay is on “{rp.track}”, which isn't in any of the "
                    f"track folders I know about.\n\nUse “Add folder…” to point "
                    f"me at it, then open the replay again.")
                return

            self.replay = rp
            self.replay_b = None          # a new run A invalidates the compare
            self.rp_t = 0.0
            self.rp_playing = False
            self.rp_play_btn.configure(text="▶", state="normal")
            self.rp_back.configure(state="normal")
            self.rp_scale.configure(state="normal")
            self.rp_cmp_btn.configure(state="normal")
            who = ", ".join(k.name or k.ident or "?" for k in rp.karts)
            self.rp_info.configure(
                text=f"{os.path.basename(f)} — {who} on {rp.track}, "
                     f"{rp.mode}, {rp.laps} lap(s), best {_mmss(rp.min_time)}")

            # the lap picker only makes sense once we know the lap count
            self.rp_lap.set("Follow")
            self.rp_lap_box.configure(
                values=("Follow", "All") + tuple(str(i + 1)
                                                 for i in range(max(1, rp.laps))))
            self.rp_drawn_lap = None

            # match the map to the replay, then select its track, clearing any
            # filter that would otherwise hide it
            self.v_rev.set(rp.reverse)
            if rp.track not in self.shown:
                self.filter.set("")
                self.f_kind.set("All types")
                self.f_src.set("All sources")
                self.refill()
            if rp.track in self.shown:
                i = self.shown.index(rp.track)
                self.listbox.selection_clear(0, "end")
                self.listbox.selection_set(i)
                self.listbox.see(i)
            self.preview()               # redraws, then calls rp_draw_static
            self.rp_update()

        def open_compare(self):
            """Load a second replay alongside the first."""
            if not self.replay:
                return
            dirs = default_replay_dirs()
            f = filedialog.askopenfilename(
                title="Second replay to compare",
                initialdir=os.path.dirname(self.replay.path) or
                (dirs[0] if dirs else None),
                filetypes=[("STK replay", "*.replay"), ("All files", "*")])
            if not f:
                return
            if f:
                self.use_compare(f)

        def use_compare(self, f):
            """Load a replay as run B, alongside A."""
            if not self.replay:
                return
            if os.path.abspath(f) == os.path.abspath(self.replay.path):
                messagebox.showinfo("Same replay",
                                    "That's the replay already loaded.")
                return
            try:
                rp = load_replay(f)
            except (SystemExit, OSError, ValueError) as exc:
                messagebox.showerror("Could not read replay", str(exc))
                return
            if rp.track != self.replay.track:
                messagebox.showerror(
                    "Different track",
                    f"That run is on “{rp.track}”, but the one loaded is on "
                    f"“{self.replay.track}”.\n\nTwo runs can only be compared "
                    f"on the same track.")
                return
            self.replay_b = rp
            self.rp_t = 0.0
            self.rp_playing = False
            self.rp_play_btn.configure(text="▶")
            self.rp_info.configure(
                text=f"A {os.path.basename(self.replay.path)}  "
                     f"({_mmss(self.replay.min_time)})   vs   "
                     f"B {os.path.basename(f)}  ({_mmss(rp.min_time)})")
            self.rp_redraw()

        def browse_replays(self):
            """
            A sortable table of every replay on the machine, including the
            world records and challenge ghosts that ship with the game.
            """
            reps = scan_replays(default_replay_dirs())
            if not reps:
                messagebox.showinfo(
                    "No replays",
                    "I couldn't find any .replay files.\n\nSTK writes them "
                    "when you finish a race with recording turned on, and "
                    "ships world records in its own data folder.")
                return

            win = tk.Toplevel(self.root)
            win.title("Replays")
            win.minsize(760, 460)
            win.transient(self.root)

            top = ttk.Frame(win, padding=8)
            top.pack(fill="x")
            ttk.Label(top, text="Search").pack(side="left")
            q = tk.StringVar()
            ttk.Entry(top, textvariable=q, width=24).pack(side="left", padx=(4, 12))
            ttk.Label(top, text="Show").pack(side="left")
            src = tk.StringVar(value="All")
            ttk.Combobox(top, textvariable=src, state="readonly", width=14,
                         values=("All", "World record", "Ghost", "Challenge",
                                 "Yours")).pack(side="left", padx=4)

            cols = ("track", "time", "laps", "driver", "date", "source")
            heads = {"track": "Track", "time": "Time", "laps": "Laps",
                     "driver": "Driver", "date": "Date", "source": "Source"}
            widths = {"track": 210, "time": 80, "laps": 50, "driver": 150,
                      "date": 100, "source": 110}
            mid = ttk.Frame(win, padding=(8, 0))
            mid.pack(fill="both", expand=True)
            tree = ttk.Treeview(mid, columns=cols, show="headings",
                                selectmode="browse")
            for c in cols:
                tree.heading(c, text=heads[c],
                             command=lambda c=c: sort_by(c))
                tree.column(c, width=widths[c],
                            anchor="e" if c in ("time", "laps") else "w")
            vs = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=vs.set)
            tree.pack(side="left", fill="both", expand=True)
            vs.pack(side="right", fill="y")

            detail = ttk.Label(win, text="", anchor="w", padding=(10, 4),
                               justify="left")
            detail.pack(fill="x")

            rows = []
            for r in reps:
                md = self.meta.get(r.track)
                rows.append(dict(
                    path=r.path,
                    track=(md["name"] if md else r.track),
                    ident=r.track,
                    time=r.min_time,
                    laps=r.laps,
                    driver=(r.names[0][1] if r.names else ""),
                    date=replay_date(r.path),
                    source=replay_source(r.path),
                    info=r.info,
                    known=md is not None))

            state = {"col": "time", "desc": False}

            def visible():
                text = q.get().strip().lower()
                want = src.get()
                out = []
                for r in rows:
                    if want != "All" and r["source"] != want:
                        continue
                    if text and text not in r["track"].lower() \
                            and text not in r["driver"].lower() \
                            and text not in os.path.basename(r["path"]).lower():
                        continue
                    out.append(r)
                out.sort(key=lambda r: r[state["col"]], reverse=state["desc"])
                return out

            import datetime

            def repopulate(*_):
                tree.delete(*tree.get_children())
                for r in visible():
                    tree.insert("", "end", iid=r["path"], values=(
                        r["track"] + ("" if r["known"] else "  (not installed)"),
                        _mmss(r["time"]) if r["time"] else "—",
                        r["laps"] or "—",
                        r["driver"],
                        datetime.datetime.fromtimestamp(
                            r["date"]).strftime("%Y-%m-%d") if r["date"] else "",
                        r["source"]))

            def sort_by(col, toggle=True):
                # clicking the current column reverses it; anything else, and
                # the initial sort, starts ascending
                state["desc"] = (not state["desc"]
                                 if toggle and state["col"] == col else False)
                state["col"] = col
                for c in cols:
                    arrow = "" if c != col else ("  ▾" if state["desc"] else "  ▴")
                    tree.heading(c, text=heads[c] + arrow)
                repopulate()

            def selected_row():
                sel = tree.selection()
                if not sel:
                    return None
                return next((r for r in rows if r["path"] == sel[0]), None)

            def on_select(_e=None):
                r = selected_row()
                if not r:
                    return
                bits = [os.path.basename(r["path"])]
                if r["info"]:
                    bits.append(r["info"])
                if not r["known"]:
                    bits.append("track not installed — can't draw this one")
                detail.configure(text="\n".join(bits))
            tree.bind("<<TreeviewSelect>>", on_select)

            def load_a(_e=None):
                r = selected_row()
                if not r:
                    return
                win.destroy()
                self.use_replay(r["path"])

            def load_b():
                r = selected_row()
                if not r:
                    return
                win.destroy()
                self.use_compare(r["path"])

            tree.bind("<Double-1>", load_a)
            q.trace_add("write", repopulate)
            src.trace_add("write", repopulate)

            btns = ttk.Frame(win, padding=8)
            btns.pack(fill="x")
            ttk.Button(btns, text="Load", command=load_a).pack(side="left")
            cmp_btn = ttk.Button(btns, text="Compare with loaded run",
                                 command=load_b)
            cmp_btn.pack(side="left", padx=6)
            if not self.replay:
                cmp_btn.configure(state="disabled")
            ttk.Button(btns, text="Close",
                       command=win.destroy).pack(side="right")
            ttk.Label(btns, text=f"{len(rows)} replays").pack(side="right",
                                                              padx=10)
            sort_by("time", toggle=False)
            win.update_idletasks()

        def rp_px(self, kart):
            """Replay world positions -> canvas coordinates."""
            fr = self.frame
            out = []
            for x, z in zip(kart.x, kart.z):
                px, py = fr.to_px(x, z)
                out.append((self.img_x + px, self.img_y + py))
            return out

        def rp_on_screen(self) -> bool:
            """True when the map on display is the one the replay was set on."""
            return bool(self.replay and self.frame and self.current
                        and self.current[0] == self.replay.track)

        def rp_entries(self):
            """
            Every kart on show, as (label, kart, colour).

            One replay with one kart is the common case; a ghost race gives two
            karts in one replay, and "Compare with…" gives a second replay.
            Everything downstream just walks this list.
            """
            out = []
            reps = [r for r in (self.replay, self.replay_b) if r]
            for ri, rep in enumerate(reps):
                for ki, kart in enumerate(rep.karts):
                    who = kart.name or kart.ident or f"kart {ki + 1}"
                    label = f"{'AB'[ri]} {who}" if len(reps) > 1 else who
                    colour = _KART_COLOURS[(ri * 3 + ki) % len(_KART_COLOURS)]
                    out.append((label, kart, colour))
            return out

        def rp_duration(self) -> float:
            return max((r.duration for r in (self.replay, self.replay_b) if r),
                       default=0.0)

        def rp_wanted_lap(self):
            """Which lap the static layer should show; None means all of them."""
            sel = self.rp_lap.get()
            if sel == "All" or not self.replay:
                return None
            if sel == "Follow":
                k = self.replay.karts[0]
                return k.lap[k.frame_at(self.rp_t)] if k.lap else None
            try:
                return int(sel) - 1
            except ValueError:
                return None

        def rp_lap_range(self, kart, lap):
            """Frame range [a, b] for a lap, or the whole run when lap is None."""
            if lap is None or not kart.lap:
                return 0, len(kart) - 1
            idx = [i for i, l in enumerate(kart.lap) if l == lap]
            if not idx:
                return 0, len(kart) - 1
            return idx[0], idx[-1]

        def rp_redraw(self):
            self.rp_drawn_lap = None
            self.rp_draw_static()

        def rp_draw_static(self):
            """The parts that don't move: route, nitro, skids, item uses."""
            self.canvas.delete("rp")
            self.rp_marks = []
            if not self.rp_on_screen():
                return
            # cache the projected route: rp_update runs 50x a second and must
            # not reproject thousands of points each time
            entries = self.rp_entries()
            self.rp_cache = [self.rp_px(k) for _l, k, _c in entries]
            lap = self.rp_wanted_lap()
            self.rp_drawn_lap = lap
            mode = self.rp_colour.get()

            for ki, (_label, kart, base) in enumerate(entries):
                pts = self.rp_cache[ki]
                if len(pts) < 2:
                    continue
                a, b = self.rp_lap_range(kart, lap)

                def seg(i0, i1, colour, width):
                    i0, i1 = max(i0, a), min(i1, b)
                    if i1 > i0:
                        self.canvas.create_line(
                            *[c for p in pts[i0:i1 + 1] for c in p],
                            fill=colour, width=width, tags="rp")

                if mode == "Speed":
                    # where a run is slow is where it is losing time, and that
                    # reads instantly off the map
                    top = max(kart.speed) or 1.0
                    for i0, i1, bucket in _runs_by(kart.speed, lambda s:
                                                  min(7, int(8 * s / top))):
                        seg(i0, i1, _SPEED_RAMP[bucket], 3)
                elif mode == "Nitro & skid":
                    seg(a, b, "#4a5568", 2)
                    levels = [kart.skid_level(i) for i in range(len(kart))]
                    for i0, i1, lv in _runs_by(levels, lambda v: v):
                        if lv >= 2:      # only charged skids are interesting
                            seg(i0, i1, _SKID_COLOURS[lv], 3)
                    for i0, i1, on in _runs_by(kart.nitro_use, bool):
                        if on:
                            seg(i0, i1, "#39e0ff", 3)
                else:
                    seg(a, b, base, 2)

                if self.rp_v_items.get():
                    for i0, i1, on in _runs_by(kart.zipper, bool):
                        if on and a <= i0 <= b:
                            x, y = pts[i0]
                            self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4,
                                                    outline="#ffd23f", width=2,
                                                    tags="rp")
                    for i in kart.item_uses():
                        if a <= i <= b:
                            x, y = pts[i]
                            self.canvas.create_polygon(
                                x, y - 5, x + 5, y, x, y + 5, x - 5, y,
                                fill="#ff4fd8", outline="", tags="rp")
            self.rp_update()

        def rp_rate_value(self) -> float:
            try:
                return float(self.rp_rate.get().rstrip("x"))
            except ValueError:
                return 1.0

        def rp_toggle(self):
            if not self.replay:
                return
            if self.rp_t >= self.rp_duration():
                self.rp_t = 0.0
            self.rp_playing = not self.rp_playing
            self.rp_play_btn.configure(text="⏸" if self.rp_playing else "▶")
            if self.rp_playing:
                self.rp_last = time.monotonic()
                self.root.after(20, self.rp_tick)

        def rp_restart(self):
            self.rp_t = 0.0
            self.rp_update()

        def rp_scrub(self, _v):
            if not self.replay or self.rp_scrubbing:
                return
            self.rp_t = self.rp_pos.get() * self.rp_duration()
            self.rp_update(from_scrub=True)

        def rp_tick(self):
            if not (self.rp_playing and self.replay):
                return
            now = time.monotonic()
            self.rp_t += (now - self.rp_last) * self.rp_rate_value()
            self.rp_last = now
            if self.rp_t >= self.rp_duration():
                self.rp_t = self.rp_duration()
                self.rp_playing = False
                self.rp_play_btn.configure(text="▶")
            self.rp_update()
            if self.rp_playing:
                self.root.after(20, self.rp_tick)

        def rp_update(self, from_scrub=False):
            """Move the kart marker and refresh the readouts."""
            if not self.rp_on_screen() or not self.rp_cache:
                return
            # in Follow mode the visible lap changes as the run progresses;
            # rp_draw_static calls back here once the new lap is drawn
            want = self.rp_wanted_lap()
            if want != self.rp_drawn_lap:
                self.rp_draw_static()
                return
            self.canvas.delete("rpnow")
            lines = []
            entries = self.rp_entries()
            for ki, (label, kart, colour) in enumerate(entries):
                if not len(kart) or ki >= len(self.rp_cache):
                    continue
                pts = self.rp_cache[ki]
                i = kart.frame_at(self.rp_t)
                x, y = pts[i]

                tail = pts[max(0, i - 22):i + 1]
                if len(tail) > 1:
                    self.canvas.create_line(*[c for p in tail for c in p],
                                            fill="#ffffff", width=3,
                                            tags="rpnow")
                # nitro sits outside the skid ring so the two never collide
                if kart.nitro_use[i]:
                    self.canvas.create_oval(x - 15, y - 15, x + 15, y + 15,
                                            outline="#39e0ff", width=3,
                                            tags="rpnow")
                level = kart.skid_level(i)
                if level:
                    self.canvas.create_oval(x - 10, y - 10, x + 10, y + 10,
                                            outline=_SKID_COLOURS[level],
                                            width=3, tags="rpnow")
                r = 6
                self.canvas.create_oval(x - r, y - r, x + r, y + r,
                                        fill=colour, outline="#101010",
                                        width=2, tags="rpnow")

                flags = []
                if kart.nitro_use[i]:
                    flags.append("NITRO")
                flags.append(_SKID_NAMES[level])
                if kart.zipper[i]:
                    flags.append("ZIPPER")
                laps = max(r.laps for r in (self.replay, self.replay_b) if r)
                lap = f"lap {kart.lap[i] + 1}/{laps}   " \
                    if kart.lap and laps > 1 else ""
                lines.append(
                    f"{label}:  {lap}{kart.speed[i] * 3.6:5.1f} km/h   "
                    f"nitro {kart.nitro_amount[i]:.0f}   "
                    f"items {kart.item_amount[i]}   "
                    f"{'  '.join(f for f in flags if f)}")

            gap = self.rp_gap(entries)
            if gap:
                lines.append(gap)
            self.rp_readout.configure(text="\n".join(lines))
            self.rp_clock.configure(
                text=f"{_mmss(self.rp_t)} / {_mmss(self.rp_duration())}")
            if not from_scrub:
                self.rp_scrubbing = True
                self.rp_pos.set(self.rp_t / (self.rp_duration() or 1.0))
                self.rp_scrubbing = False

        def rp_gap(self, entries) -> str:
            """
            How far apart two runs are, in seconds at the same point on track.

            Comparing positions at the same *time* only says who is ahead;
            what a run is actually worth is the time difference at the same
            *place*, which is what a ghost shows you.
            """
            if not (self.replay_b and len(entries) >= 2):
                return ""
            a_kart = entries[0][1]
            b_kart = next((k for _l, k, _c in entries[1:]
                           if k is not entries[0][1]), None)
            if b_kart is None:
                return ""
            i = a_kart.frame_at(self.rp_t)
            here = a_kart.distance[i]
            if here <= 0:
                return "Δ  —"
            t_b = b_kart.time_at_distance(here)
            if t_b is None:
                return "Δ  B hasn't reached this point"
            d = t_b - a_kart.time[i]
            who = "B behind" if d > 0 else "B ahead"
            return f"Δ  {who} by {abs(d):.2f}s at this point on track"

        def add_folder(self):
            d = filedialog.askdirectory(title="Folder holding STK tracks")
            if not d:
                return
            self.extra_dirs.insert(0, d)
            self.tracks = find_tracks(self.extra_dirs)
            self.scan_tracks()
            self.refill()
            self.status.configure(text=f"{len(self.shown)} tracks")

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
            self.meta[ti.ident] = dict(name=ti.name or ti.ident,
                                       kind=track_kind(ti),
                                       addon=True,      # opened by hand = add-on
                                       renderable=track_renderable(ti))
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
    ap.add_argument("--outline", type=float, default=None,
                    help="outline width in output pixels; 0 turns it off "
                         "(default: none for 'clean', 1 for 'blueprint')")
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
