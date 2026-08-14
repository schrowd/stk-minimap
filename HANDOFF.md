# Context brief: `stk_minimap.py`

I'm attaching a Python script called `stk_minimap.py`. Read it first, then read this
brief — it contains the domain research behind the script, which you won't be able to
reconstruct from the code alone. My actual task is at the bottom.

## What the script does

SuperTuxKart does not ship minimap images. It generates them at track load time, in
`Graph::makeMiniMap` (`src/tracks/graph.cpp` in the `stk-code` repo). The script is a
software reimplementation of that function and its callees, producing a PNG.

## How STK actually builds a minimap

The minimap is the track's **AI/collision graph** viewed from directly above. Two
sources depending on track type:

| Track type | File | Loader |
|---|---|---|
| Race track | `quads.xml` (+ `graph.xml`, unused for rendering) | `DriveGraph::load` |
| Arena / soccer | `navmesh.xml` | `ArenaGraph::loadNavmesh` |

Both funnel into `Graph::createQuad`, which appends a node and grows `m_bb_min` /
`m_bb_max`. `Graph::createMesh` turns the nodes into a triangle mesh, and
`makeMiniMap` renders it to a render-target texture.

Facts that the script depends on — all verified against `stk-code` master:

- **Bounding box includes invisible quads.** It's updated in `createQuad`, which runs
  before any visibility filtering in `createMesh`. Getting this wrong shifts the whole
  mapping.
- **Camera:** orthographic, positioned at `bb_min.y + 1` looking down at `bb_min.y - 1`,
  up-vector `(0,0,1)`. Net effect in the image: +X is right, +Z is up.
- **Ortho box is square:** `range = max(dx, dz)`, projection is `range × range`. The
  camera center is then deliberately offset by `(longer - shorter) / 2` along the
  shorter axis so the track is anchored at `bb_min` on *both* axes. There's a long
  ASCII-art comment in `makeMiniMap` explaining this. The point of the offset is to
  keep `mapPoint2MiniMap` a plain affine map, which is how kart markers get placed:

  ```
  px = (world_x - bb_min.x) * scaling
  py = size - (world_z - bb_min.z) * scaling     # PNG rows count from the top
  scaling = dimension.Width / range
  ```

  Consequence: on a non-square track the output has empty padding on one side. That's
  faithful, not a bug. `--fit` opts out of it and breaks mapPoint2MiniMap compatibility.
- **Colors:** clear color `SColor(0,255,255,255)`, track fill `SColor(127,255,255,255)`
  — i.e. white at alpha 127 on a transparent background. The material is opaque, so
  overlapping quads *overwrite* rather than blend; the script replicates this by
  pasting through a binary coverage mask instead of alpha-compositing.
- **Lap line:** node 0's quad, with edges p0→p3 and p1→p2 shortened to
  `(bb_max.z - bb_min.z) * 0.03`, drawn in `SColor(128,255,0,0)` and raised 0.1 in Y so
  it wins the depth test. Only for `DriveGraph`; `Graph::hasLapLine()` is false for
  arenas, and false for CTF tracks in CTF mode.
- **Quad point references:** in `quads.xml`, `p0="3:2"` means "point 2 of quad 3"
  (`DriveGraph::getPoint`). Most quads use this to share an edge with the previous one.
- **Visibility:** quads with `invisible="y"` are skipped. `direction="forward"` quads are
  force-hidden in reverse races and vice versa.
- **`Quad::getVertices` offsets every vertex by `normal * 0.1`**, then `createMesh`
  flattens Y to 0.1. On sloped ground the XZ component of that offset differs per quad,
  so neighbours can miss each other by a sliver.
- **Navmesh faces are not always quads.** `battleisland`'s has faces with 3, 5 and 6
  indices. STK reads `all_vertices[quad_index[0..3]]` unconditionally — it truncates
  n-gons to the first four vertices and reads out of bounds on triangles. The script
  truncates too by default (faithful), with `--full-polys` to fan them properly.

## Design decisions in the script

- **`--style exact` is the contract.** It must produce interior pixels of exactly
  `(255,255,255,127)` on `(255,255,255,0)` background, with the affine mapping above
  holding. `clean` and `blueprint` are cosmetic presets and can change freely.
- **Supersample + resolve.** Renders at `size * supersample`, then premultiplies alpha,
  box-averages, unpremultiplies. Lanczos was tried and rejected: it rings past alpha
  127, which breaks the exact-style contract. Pillow's own `Image.reduce` on RGBA also
  drifted (uniform 127 came back as 126), hence the hand-rolled numpy box filter.
  numpy is optional; without it there's a lower-quality Lanczos fallback.
- **Seam sealing.** The per-quad normal offset above leaves hairline gaps that the
  outline pass amplifies into visible stripes. Non-exact styles run a morphological
  close (MaxFilter then MinFilter) on the coverage mask. `exact` never does this and
  `--no-seal` disables it.
- Track lookup covers Arch (`/usr/share/supertuxkart/data/tracks`), flatpak, snap,
  in-game addon dirs, and `$STK_TRACK_DIR`. Addon `.zip` files are accepted directly.

## Known gaps / not implemented

- Soccer goal-line node coloring (`ArenaGraph::differentNodeColor` paints red/blue
  nodes). Needs `findRoadSector` + Dijkstra over the navmesh — real work.
- CTF flag bounding-box expansion in `makeMiniMap`.
- Non-square render targets. STK's minimap RTT is square in practice and
  `mapPoint2MiniMap` uses `dimension.Width` for both axes, so a non-square target would
  be inconsistent anyway.
- `height-testing` elements are parsed and ignored (physics only, no visual effect).
- Only the `default` `<mode>` from `track.xml` is honoured for `quads=` / `graph=`.

## Testing situation — read this before you trust anything

SuperTuxKart was **not installed** in the environment where this was written, so the
script has never run against a real STK install. It was validated against:

1. A `quads.xml` reassembled from the real `olivermath` track data (coordinates pulled
   from the `stk-assets` mirror at `github.com/minghuadev/stk-assets`) — 44 quads, real
   geometry, renders recognisably.
2. A synthetic `navmesh.xml` exercising 3-, 4- and 6-vertex faces.
3. A programmatic check that every visible quad's centroid lands on a painted pixel and
   that `bb_min` maps to the bottom-left corner.
4. (added by the end user) A live 1.5 copy of STK

**If STK is installed on this machine, running `./stk_minimap.py --list` and rendering a
few real tracks is the highest-value first step.** Environment is Arch Linux; deps are
`python-pillow` and (optional) `python-numpy`.

When in doubt about STK behaviour, the source of truth is `stk-code` on GitHub:
`src/tracks/graph.cpp`, `drive_graph.cpp`, `arena_graph.cpp`, `quad.cpp`, and
`Track::loadMinimap` in `track.cpp`. Don't guess — the details above were all pulled
from those files, and a few of them are counterintuitive.

---

## My task

Make this thing worthy of publication on official SuperTuxKart channels, including the speedrun.com forums/resources and the Tux Speedrunning Discord.
 This includes Windows functionality and a README file on how to use it. This should be tracked via Git as I will publish this on GitHub once complete.
