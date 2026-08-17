from __future__ import annotations

import math
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

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
