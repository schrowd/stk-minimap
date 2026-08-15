# Implementation notes

Background research behind `stk_minimap.py`, kept because most of it can't be
reconstructed from the code alone. Worth reading before changing anything in the
loading or framing path: several of the behaviours below are counterintuitive, and
getting one wrong silently shifts or distorts every image the tool produces.

Everything here was pulled from `stk-code` master — that, not this document, is the
source of truth.

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

## Replay files

`.replay` files are plain text, written by `ReplayRecorder::save` in
`src/replay/replay_recorder.cpp`. A header of `key: value` lines, one `kart:`
line per kart terminated by `kart_list_end`, then `size: N` and N
whitespace-separated rows **per kart**, in the order the karts were listed:

```
version: 4
stk_version: 1.5
kart: puffy schrowd          <- model, then player name (may contain spaces)
kart_color: 0.000000
kart_list_end
reverse: 0
difficulty: 3
mode: time-trial
track: hacienda
laps: 3
min_time: 95.099823
replay_uid: 1389859808968254121
size:     1505
```

26 columns per row:

| # | Field | # | Field |
|---|---|---|---|
| 1 | time | 14 | susp3 |
| 2 | x | 15 | skidding_state |
| 3 | y | 16 | attachment |
| 4 | z | 17 | nitro_amount |
| 5–8 | qx qy qz qw | 18 | item_amount |
| 9 | speed | 19 | item_type |
| 10 | steer | 20 | special_value |
| 11–13 | susp0–2 | 21 | distance |
| | | 22 | nitro_usage |
| | | 23 | zipper_usage |
| | | 24 | skidding_effect |
| | | 25 | red_skidding |
| | | 26 | jumping |

Things worth knowing, all established by measuring a real 1.5 recording rather
than reading the source:

- **x/y/z are world coordinates**, so `Framing.to_px` places them directly —
  the same transform the game uses for the kart markers. Verified against
  Hacienda: the replay spans x −3.2…293.4, z −138.5…172.2 against a track
  bounding box of x −5.1…302.4, z −140.0…178.3.
- **`distance` is cumulative for the whole run**, not per lap, and it is the
  basis for both lap splitting and ghost comparison. Two quirks: it holds a
  large negative placeholder (about −1020) until the kart first registers on
  the driveline, and it blips to ~0 for a single frame as the lap line is
  crossed. Running the maximum forward absorbs both, which is what
  `split_laps` and `time_at_distance` do.
- **Skid charge lives in `skidding_effect`**, which steps `200 → 2000 → 2500`
  through a single skid — pre-charge, yellow, red. `red_skidding` is *not* the
  charge level: it marks the boost being spent, so it is already set before the
  next skid begins. Colouring by it gives visibly wrong results.
- **`nitro_usage` is 0 or 800**, i.e. effectively a boolean.
- `attachment`, `special_value` and `jumping` were constant across every
  recording available, so nothing depends on them.

Lap boundaries derived from equal bands of `distance` land within ~0.6s of the
lap line crossings found geometrically (by proximity to the start position),
which is comfortably good enough for choosing what to draw.

### Finding replays

Two sources:

- **What the player recorded** — the same per-user directory the addon tracks
  live beside (`%APPDATA%\supertuxkart\replay`, `~/.local/share/...`, and the
  flatpak/snap variants).
- **What ships with the game** — `<data>/replay`, a sibling of the
  `<data>/tracks` that track discovery already locates. Deriving it from there
  rather than writing a second set of platform guesses means Windows, macOS,
  Steam, flatpak and snap all work for free, and `--data-dir` picks up that
  install's records too. STK 1.5 ships 51: 21 `wr_*` world records, 25
  `standard_*` ghosts, 4 `challenge_*` and one benchmark.

The `wr_*` files carry an extra `info:` header line — *"Hacienda (Glitchless) -
Former World Record set on 25 March 2020"* — which is the only way to tell a
current record from a former one, so it's surfaced in the browser.

Dates come from the filename (`<track>_<YYYYMD>_<n>_<sec>_<frac>`), not the
file mtime: every shipped record has the packaging date as its mtime, which
sorts them meaninglessly. The month and day are **not** zero-padded, so
`2025824` is 2025-08-24 while `202153` is 2021-05-03 — the split is ambiguous
and has to be tried both ways, taking whichever yields a valid date. The
middle number is not the kart count, lap count or difficulty; nothing depends
on it.

Browsing reads headers only, stopping at the `size:` line. Parsing all 108
replays in full to fill in one table would mean megabytes of float conversion;
header-only is ~4ms for the same set.

The ghost delta answers "how far apart are these two runs **at the same point
on the track**", by finding when run B covered the distance run A has covered
now. Comparing positions at equal timestamps only says who is further ahead;
the time difference at a given corner is the number that says where a run was
won or lost. Sanity check: two real Hacienda runs 14.98s apart in final time
converge to a 15.00s gap by the end of the run.

## Known gaps / not implemented

- Soccer goal-line node coloring (`ArenaGraph::differentNodeColor` paints red/blue
  nodes). Needs `findRoadSector` + Dijkstra over the navmesh — real work.
- CTF flag bounding-box expansion in `makeMiniMap`.
- Non-square render targets. STK's minimap RTT is square in practice and
  `mapPoint2MiniMap` uses `dimension.Width` for both axes, so a non-square target would
  be inconsistent anyway.
- `height-testing` elements are parsed and ignored (physics only, no visual effect).
- Only the `default` `<mode>` from `track.xml` is honoured for `quads=` / `graph=`.

## Testing

Validated against a stock **SuperTuxKart 1.5** install on Arch Linux:

- `--list` finds 44 tracks; 38 have a graph and render. The other six are cutscenes
  and grand-prix result screens with no driveline, and are skipped by `--all`.
- Race tracks (`hacienda`, `cornfield_crossing`), arenas (`battleisland`) and soccer
  fields (`icy_soccer_field`) all render recognisably.
- The `exact` palette check: interior pixels are exactly `(255,255,255,127)`,
  background `(255,255,255,0)`, lap line `(255,0,0,128)` on race tracks only, with
  roughly 1% antialiased edge pixels in between.
- Every visible quad's centroid lands on a painted pixel, and `bb_min` maps to the
  bottom-left corner exactly — `(0.0, 512.0)` at the default size.

Earlier validation, from before an STK install was available, used a `quads.xml`
reassembled from real `olivermath` data and a synthetic `navmesh.xml` exercising 3-,
4- and 6-vertex faces. The synthetic navmesh is still the only coverage for the
n-gon truncation path, since no stock 1.5 track exercises every case.

The GUI was driven end to end under a real Tk main loop: track list, preview,
`exact`-style preview, save, save-all, the filter box, and replay playback
(load, scrub, rate, lap follow, skid and nitro rings, two-run comparison).

Replay parsing was checked against all 108 replays available — 57 local
recordings plus the 51 that ship with 1.5 — with no failures and no
out-of-range lap indices. Every one is a single-kart run, so the multi-kart
path is covered only by a hand-built two-kart file.

The ghost delta was validated against ground truth: two real Hacienda runs
whose recorded times differ by 14.98s converge to a 15.00s gap by the end.

Windows and macOS paths are exercised against simulated install trees (a
Program Files layout, an `%APPDATA%` replay folder and a Steam
`libraryfolders.vdf`), which proves the path construction and the vdf parsing
but not the real-world install locations. Nothing here has run on Windows.

Deps are `python-pillow` and, optionally, `python-numpy`; the GUI additionally needs
`tk` and `python-pillow`'s ImageTk.

## Source of truth

`stk-code` on GitHub: `src/tracks/graph.cpp`, `drive_graph.cpp`, `arena_graph.cpp`,
`quad.cpp`, and `Track::loadMinimap` in `track.cpp`.
