# Implementation notes

Background research behind the `stk_viewer` package, kept because most of it can't be
reconstructed from the code alone. Worth reading before changing the graph loading,
the framing, or the replay parsing: several of the behaviours below are
counterintuitive, and getting one wrong silently shifts or distorts every image the
tool produces, or mislabels what a run was doing.

The two halves were established differently, which matters when you go to verify
something:

- **The minimap half** was read out of `stk-code` master. That, not this document,
  is the source of truth - see the links at the bottom.
- **The replay half** was established by measuring real recordings, because the
  format is barely documented and several fields do not mean what their names
  suggest. Where a claim here came from measurement, it says so.

## What the script does

Two things.

**Minimaps.** SuperTuxKart does not ship minimap images. It generates them at track
load time, in `Graph::makeMiniMap` (`src/tracks/graph.cpp` in the `stk-code` repo).
The script is a software reimplementation of that function and its callees,
producing a PNG.

**Replays.** It also reads STK's `.replay` files and draws a run over that minimap -
position, speed, nitro, skid charge, item use - including comparing two runs. The
world records shipped with the game are ordinary replay files, so they load like any
other. The format is documented under [Replay files](#replay-files) below.

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

Facts that the script depends on - all verified against `stk-code` master:

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
  - i.e. white at alpha 127 on a transparent background. The material is opaque, so
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
  indices. STK reads `all_vertices[quad_index[0..3]]` unconditionally - it truncates
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
  The box filter reduces the two block axes one at a time rather than with
  `mean(axis=(1, 3))` - twice as fast, and provably identical here because the
  coverage masks are binary, so the premultiplied values are exact in float32 and
  summation order cannot matter. Verified bit-identical across every style on five
  tracks; if the masks ever stop being binary, that guarantee goes with it.
- **No outline on `clean`.** An inward stroke one output pixel wide is thinner than
  the antialiasing ramp, so it never survives the resolve as its own colour - it
  just stretches the ramp, which reads as blur rather than definition, and on a
  thin driveline it eats the whole width. `blueprint` keeps a stroke because its
  fill is translucent and barely registers without one. Hence per-style
  `outline_px` defaults rather than one global width.
- **Seam sealing.** The per-quad normal offset above leaves hairline gaps that an
  outline pass amplifies into visible stripes. Non-exact styles run a morphological
  close on the coverage mask. `exact` never does this and `--no-seal` disables it.
  The close and the outline erosion both use a **disk**, not Pillow's square
  `Min/MaxFilter`: a square structuring element reaches √2 further into a 45°
  edge than a horizontal one, measured at 44% fatter outlines on the diagonals,
  which reads as a stroke that wobbles along curves.
- Track lookup covers Arch (`/usr/share/supertuxkart/data/tracks`), flatpak, snap,
  in-game addon dirs, and `$STK_TRACK_DIR`. Addon `.zip` files are accepted directly.

## Rotation

`--rotate` turns the map by putting the rotation inside `Framing.to_px`, rather
than rotating the finished image. Two reasons: everything that draws - the graph,
the check lines, the replay route and markers - goes through that one function, so
they stay consistent for free; and re-framing around the rotated extent avoids both
the clipping and the resampling blur you would get from rotating a bitmap.

The framing has to measure the **rotated** points to find the new extent. The
corners of the unrotated bounding box are not enough: a diagonal track fills a very
different box once turned, and using the old one clips it.

The angle is negated on the way in, so that a positive angle turns the map
clockwise on screen. `+z` maps upward in the image, so the unnegated maths turns it
the other way, which is not what "rotate right" means anywhere else.

At an angle of zero the framing is bit-identical to the unrotated path - the same
`bb_min` anchoring, so the `exact` contract is unaffected. Rotation does break
`mapPoint2MiniMap` compatibility, as `--fit` does, and for the same reason: the
mapping is no longer a plain affine transform of the world point.

## When a replay leaves the driveline graph's bounding box

STK builds the minimap from the driveline graph alone (`Graph::createQuad`'s
bounding box), which is not necessarily everywhere a kart can physically go.
Confirmed against real data before writing anything: the shipped Cocoa Temple
world record dips 14.8 world units past the graph's minimum Z; a minigolf
egg-hunt challenge goes 24–27 units past on both axes. At the default size
that's tens of pixels of a genuine shortcut - not a rare artefact - silently
missing, because PIL's `ImageDraw` clips a call that goes off-canvas rather
than raising, so nothing *looks* broken until you go looking for the missing
piece. A sweep of every local replay against its own track found 9 of the 21
tracks with at least one replay affected, not just Cocoa Temple.

`expand_framing_for_replay` grows the canvas to fit whenever this happens.
The one property that matters, and the one this cannot violate given the
orientation-lock guarantee above: **scaling, angle and pivot never change,
canvas size and origin do** - which makes it a pure translation, not a
rescale. Concretely: `new_origin_x = origin_x - left`,
`new_width = width + (left + right) * scaling`, and the reverse on Z; a point
that was already on the map moves by exactly the same pixel vector as every
other point, so distances, angles and proportions between any two points on
the map are preserved to floating-point precision (verified: ~1e-13 across
sampled point pairs). The base minimap ends up positioned somewhere other
than pixel (0, 0) of the output when this fires, with the shortcut visible in
the surrounding margin - the game-accurate part of the image is untouched,
just no longer flush against the corner. A margin of `3%` of the frame is
added on all four sides whenever *any* side needs to grow, for breathing
room, which is why even a side with no excursion can still shift by a
constant amount - correct and expected, not a bug (see the verification note
below).

Wired in at the one place both entry points already funnel through:
`build()` takes an optional `extra_karts` list purely for this expansion,
independent of whether it's also drawing an overlay through `render()`'s
`replay=` parameter. The CLI's `--replay`/`--compare` path already has karts
in hand and passes them along; the GUI's live view draws its replay overlay
straight onto the Tk canvas rather than through that PIL path; it passes the
loaded replay's karts (both, if comparing) into the same `extra_karts`
parameter purely to get the framing sized right, without duplicating any of
`build()`'s graph or checkline loading. This is also why the fix is entirely
free for the overwhelmingly common case - an ordinary track/replay pair
returns the identical `Framing` object, verified by identity (`is`), and a
plain map render (no `--replay` at all) never touches this path.

Verification trap worth recording: my first check compared each point's
*absolute* pixel position between the old and new framing and found points
apparently drifting up to 30px, which looked like a bug. It wasn't - that
was `abs(dx) + abs(dy)` summed across both axes, and both axes legitimately
shift once padding is added on all four sides. The property that actually
needs checking is *relative* geometry - the distance between any two points
in pixel space - which came back exact. Don't test "did this pixel move",
test "did the picture change shape".

Rotation composes correctly without special-casing: because `make_framing`'s
rotated case already fits the whole graph tightly *for that specific angle*,
how much slack is left over on any given side varies with the angle - at
rotate=0 or 60 Cocoa Temple's excursion needs expansion, at rotate=30 the
already-computed rotated frame happens to have enough natural margin on the
relevant side and no expansion fires at all. Confirmed by comparing the three
angles directly rather than assuming; not a bug; the same excursion in world
space simply lands inside a differently-shaped box depending on the angle.

## Replay playback

Recorded frames are irregular and sparse: about 15 per second, with gaps ranging
from 8ms to 100ms. At the 50fps redraw the marker would hold still for several
frames and then jump - 4.5 world units at top speed. Positions are therefore
linearly interpolated between the two bracketing frames, and the trail is drawn to
the interpolated head rather than the last recorded point, or it visibly lags the
marker. Speed in the readout is interpolated too; the categorical fields (skid
level, nitro, items) are not, since there is nothing meaningful between two states.

The marker is an arrow along the kart's heading, recovered from the recorded
quaternion in columns 5–8 by rotating local `+Z` into world space:

```
heading = atan2( 2(qx·qz + qy·qw),  1 − 2(qx² + qy²) )
```

Two checks established that this is the right reading rather than a plausible
one. Against the direction of travel it sits at a median 8.3°, where the mirrored
interpretation is 80° out - so the axis and handedness are right. And the residual
is not error but **slip angle**: broken down by skid charge it is 0.3° when not
skidding, 16.5° skidding uncharged, 21.4° at yellow and 25.1° at red. A conversion
that was subtly wrong would not line up with the skid state like that.

Heading is interpolated the short way around the circle, or the arrow spins
through a full turn whenever a run crosses ±π.

The arrow is turned by the framing's own angle so it keeps pointing down the
track when the map is rotated, and the screen angle is negated because the image
has Y increasing downward.

Marker radius shrinks by kart index, and the skid and nitro rings are offset from
that radius rather than fixed. Two runs of the same track sit exactly on top of
each other at the start line, and Tk canvas items have no alpha, so without
differing radii the second kart would completely hide the first.

**Rotation locks to 0° the moment a replay is loaded** (`lock_rotation`,
called from `use_replay`), and the Rotate control is disabled while it holds.
A rotated map is fine for a diagram; a rotated *replay* would mean the thing
being studied no longer visually matches what actually happened in the race,
which defeats the point of a replay viewer. There's no "close replay" action
anywhere in the app, so nothing currently re-enables the control once a
replay has been loaded - matching intent, not an oversight, since there's
nothing to revert to.

## Check lines

Check structures live in the track's **`scene.xml`**, not in any of the graph
files, under a `<checks>` element. Across the 44 stock 1.5 tracks there are only
two element types: 126 `<check-line>` (101 `kind="activate"`, 25 `kind="lap"`) and
23 `<check-lap>`. `check-lap` carries no geometry - it is the lap counter, and
refers to check lines by id - so nothing is drawn for it. No shipped track defines
`check-goal`, so soccer goal lines are still not available from here.

The endpoint format is the trap. `p1` and `p2` are written **either** as `"x z"`
**or** as `"x y z"`, matching `CheckLine`'s attempt at a 2D read before falling
back to 3D. Taking the first two components unconditionally is wrong for the
3-component form and collapses every line into a thin band near z = 0 - and it
fails quietly, because those bogus coordinates still land inside most tracks'
bounding boxes. Testing whether the line's midpoint falls on a painted quad
separates the two cleanly: reading three components as `(x, y, z)` puts 85% of
midpoints on the driveline, against 11% for the first-two reading.

Lap lines are frequently much shorter than the gates - Hacienda's are two
2-unit segments at the edges of the start line, against `activate` gates
spanning ~26 units - so they can render as only a few pixels. That is the real
geometry.

### Sector splits

`compute_splits` turns the same check lines into per-lap sector times, using
`_seg_intersect_frac` (standard 2D segment-segment intersection, solving for
both parametric fractions and requiring each in [0, 1]) walked once forward
through the recorded path.

Two things had to be established against real data before trusting any of
this, not assumed from the XML shape:

- **Alternate-route gates share a `same-group` value.** Confirmed against
  Hacienda's `scene.xml` directly: indices 3 and 4 (both `kind="activate"`)
  carry an identical `same-group="3 4"` for the fork after the loop. That
  value only means what it looks like it means if check indices are counted
  over **every** direct child of `<checks>` in document order, including
  `check-lap`, which has no geometry of its own - indices computed by
  searching for `<check-line>` alone come out wrong. `sector_gates` groups
  lines by their literal `same-group` tuple; STK always gives even a lone
  gate a self-referential id (`same-group="1"` on a line at index 1), so the
  grouping key needs no separate index field on `CheckLine`.
- **The lap line itself can't be used to find lap boundaries.** The obvious
  design was to detect the lap-line crossing the same way as a gate crossing.
  It doesn't work: on Hacienda the two lap-line segments sit at x≈-5 and
  x≈+5, both at z≈0, while the real racing line crosses z=0 near x≈0 -
  between them, touching neither. A kart driving down the middle of the road
  never intersects either segment. `compute_splits` instead anchors each
  lap's start/end on the frame range `lap_frame_range` already gives
  (distance-based, ±0.6s or so) and only uses geometric crossing detection
  for the gates in between, which are wide enough to actually catch a normal
  line. Sector 1 and the last sector inherit that ±0.6s; the gate-to-gate
  sectors between them are exact.

One accounting bug worth remembering if this gets touched again: the natural
loop produces `len(gates)` sectors (start→gate0, gate0→gate1, ...,
gate[n-2]→gate[n-1]) and silently drops the trailing leg from the last gate to
the lap boundary. That leg is real track - on the replay used to validate this
it was 3.6s, not a rounding error - and it undercounts every lap by that
amount unless appended as sector `n+1`. Caught by summing a lap's sectors and
comparing to its `total`; they must match exactly (verified to 0.000 across
every completed lap in the local replay set once fixed).

Validated against all 113 replays available at the time (local recordings
plus the ones that ship with 1.5): no exceptions, no negative sector times,
and sector sums equal to lap totals wherever no gate was missed. Across four
different tracks (Hacienda, Scotland, Zengarden, Around the Lighthouse) with
gate counts from 3 to 7.

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

- **x/y/z are world coordinates**, so `Framing.to_px` places them directly -
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
  through a single skid - pre-charge, yellow, red. `red_skidding` is *not* the
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

- **What the player recorded** - the same per-user directory the addon tracks
  live beside (`%APPDATA%\supertuxkart\replay`, `~/.local/share/...`, and the
  flatpak/snap variants).
- **What ships with the game** - `<data>/replay`, a sibling of the
  `<data>/tracks` that track discovery already locates. Deriving it from there
  rather than writing a second set of platform guesses means Windows, macOS,
  Steam, flatpak and snap all work for free, and `--data-dir` picks up that
  install's records too. STK 1.5 ships 51: 21 `wr_*` world records, 25
  `standard_*` ghosts, 4 `challenge_*` and one benchmark.

The `wr_*` files carry an extra `info:` header line - *"Hacienda (Glitchless) -
Former World Record set on 25 March 2020"* - which is the only way to tell a
current record from a former one, so it's surfaced in the browser.

Dates come from the filename (`<track>_<YYYYMD>_<n>_<sec>_<frac>`), not the
file mtime: every shipped record has the packaging date as its mtime, which
sorts them meaninglessly. The month and day are **not** zero-padded, so
`2025824` is 2025-08-24 while `202153` is 2021-05-03 - the split is ambiguous
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

## Watching alongside the real game (investigated, not shipped)

A "Launch SuperTuxKart" button existed briefly (v1.2.0 development) and was
pulled back out before release - not because it didn't work, but because a
loose "open both, no sync" version wasn't worth having yet on its own, with
real two-way sync (pause one, both pause; scrub one, both scrub) planned for
later. The investigation behind that decision is worth keeping, so it isn't
redone from scratch next time:

Read the actual replay/ghost source in `stk-code` (`replay_play.hpp`,
`ghost_kart.cpp`, `history.hpp`) rather than guess. Findings:

- `GhostKart::update(int ticks)` positions a ghost by interpolating between
  two recorded transforms using a tick index into the replay - structurally
  the same technique this file uses for marker interpolation. That means
  rewind isn't blocked by the data model, only by nothing in STK ever driving
  that tick backward.
- Neither `ReplayPlay`, `GhostKart`, nor `History` (a separate, older,
  forward-only input-log replay system used for physics debugging, not the
  same thing as `.replay` ghost files) exposes pause, seek, rewind, or a
  speed control. The only pause is the generic whole-race pause, which
  freezes ghosts as a side effect of freezing the world clock.
- No CLI flag opens STK directly on a chosen `.replay`; `--history` replays a
  fixed `history.dat`, unrelated to ghost files. The user would have to pick
  it from STK's own Replay screen by hand regardless.
- No IPC, socket, or scriptable hook exposes live game state externally.
  `NETWORKING.md`'s only console is server admin (kick/ban). The only place
  STK streams live per-kart transforms is its actual multiplayer protocol,
  built for real races between real clients, not for reading state out of an
  offline solo replay.

Genuine two-way sync would mean patching STK itself - `GhostKart`'s
tick-indexed design suggests it's a small, well-isolated patch rather than a
rewrite - but that turns this from a script into a fork of the game, with the
maintenance and trust costs that implies. Locating the executable, for
whenever this is picked back up, doesn't need reinventing either: derive it
the same way `default_track_dirs` locates the game's data (PATH first, a
binary next to an already-found `data/tracks` directory, a couple of fixed
spots on macOS, a flatpak fallback).

**That work has since started** - see [`../patches/`](../patches/) for the
patch series and the wire protocol. Two further findings from reading the 1.5
source directly, both of which make it considerably smaller than feared:

- **Watch-replay mode has no physics to unwind.**
  `RaceManager::startWatchingReplay` sets `m_num_karts = getNumGhostKart()`
  and marks every kart `KT_GHOST`. There is no physics-driven player kart, so
  seeking is only moving a clock that every position is a pure function of -
  categorically easier than rewinding a live race, which stays out of scope.
- **The seek and pause primitives already exist and are already public.**
  `WorldStatus` exposes `setTime(float)`, `setTicks(int)`, `pause(Phase)` and
  `unpause()`. `setTime` is four lines and sets both `m_time_ticks` and
  `m_time`. Nothing needed to be added to reach them.

So the actual blocker was a single forward-only loop in
`GhostController::update`, plus a visibility latch in `GhostKart::update` -
both fixed in `patches/0001`, with the defect and the fix measured against
real replay data rather than argued from the source. The clock itself advances
one tick per `updateTime()` call in `CLOCK_CHRONO`; because ghost positions
are a pure function of that clock, rate control and pause are best done by
driving `m_time` directly in watch-replay mode rather than by trying to change
the main loop's tick rate.

## Known gaps / not implemented

- Soccer goal-line node coloring (`ArenaGraph::differentNodeColor` paints red/blue
  nodes). Needs `findRoadSector` + Dijkstra over the navmesh - real work.
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
  bottom-left corner exactly - `(0.0, 512.0)` at the default size.

Earlier validation, from before an STK install was available, used a `quads.xml`
reassembled from real `olivermath` data and a synthetic `navmesh.xml` exercising 3-,
4- and 6-vertex faces. The synthetic navmesh is still the only coverage for the
n-gon truncation path, since no stock 1.5 track exercises every case.

The GUI was driven end to end under a real Tk main loop: track list, preview,
`exact`-style preview, save, save-all, the filter box, and replay playback
(load, scrub, rate, lap follow, skid and nitro rings, two-run comparison).

Two traps if you write more of those. Stubbing out `mainloop` produces failures
that are artifacts of the stub rather than the code - drive a real main loop and
script the steps with `after()` instead. And since the track filters now persist,
anything driving the GUI must run with an isolated `XDG_CONFIG_HOME` (or
`%APPDATA%`), or a partially-completed run leaves saved filters behind and the
next one starts from a different list.

Replay parsing was checked against all 108 replays available - 57 local
recordings plus the 51 that ship with 1.5 - with no failures and no
out-of-range lap indices. Every one is a single-kart run, so the multi-kart
path is covered only by a hand-built two-kart file.

The ghost delta was validated against ground truth: two real Hacienda runs
whose recorded times differ by 14.98s converge to a 15.00s gap by the end.

Windows and macOS paths are exercised against simulated install trees (a
Program Files layout, an `%APPDATA%` replay folder and a Steam
`libraryfolders.vdf`), which proves the path construction and the vdf parsing
but not the real-world install locations. Nothing here has run on Windows.

The Render/Replay tab split, the splits panel, CSV export, and keyboard
control (space, arrows, home/end) were all driven the same way, plus one
methodology trap worth recording: comparing the *displayed* clock label
(`_mmss`, one decimal place) between two single-frame steps can show no change
even though the step happened correctly - two adjacent frames a tenth of a
second apart can round to the same text. Assert against the underlying
`rp_t` float, not the label, when testing frame-stepping. Caught this by
reading `rp_t` through the bound method captured in a `root.bind` closure's
`__self__`, since the App instance has no other handle from outside `run_gui`.

That same closure-capture trick surfaced a second, sharper methodology gap:
`widget.event_generate(seq)` and `root.focus_get()` are **not reliable** under
a headless Tk instance. `.focus_set()` calls on a Scale/Treeview/Button
silently failed to move focus in this environment - `focus_get()` kept
reporting the listbox regardless of what had just been focused - so a test
that checks "did the toplevel binding fire after focusing widget X" can pass
in the harness while the real, windowed, real-focus behaviour is still
broken. This is exactly what happened with space/arrow playback control:
tested via `root.event_generate` (bypasses real focus routing entirely) and
via `.focus_set()` + `event_generate` (silently didn't move focus here), both
looked fine, and space still didn't work for the user because ttk.Button,
Checkbutton, Scale, Treeview and Listbox all have default bindings for
space/arrows that fire at the widget's own bindtag *before* a toplevel
binding is ever reached - so focus landing on the scrub slider you'd just
dragged, or any button, silently ate the keypress. The fix that's actually
reliable regardless of focus: `harden_playback_keys` walks the tree at
startup and rebinds space/arrows/home/end directly on every widget of those
five types, each handler returning `"break"` so the widget's own default
action doesn't also fire. The right way to *test* this, once identified: call
`widget.event_generate(seq)` directly **on the specific widget** rather than
on `root` or via `focus_set`, since that reliably invokes that widget's own
bindtags regardless of whatever a headless display reports as focused.
Verified this way on all five widget types, confirming both that play/pause
now fires and that the widget's own action (button re-click, scale nudge)
does not also fire alongside it.

`invert_x_z` was found to be silently absent from `draw_replay_overlay` -
checklines and the map itself already respected it, the replay route didn't.
Fixed by threading it through the same way, and cross-checked against real
`stk-code` (`Track::mapPoint2MiniMap`, `Track::loadArenaGraph`): the mirror
STK actually applies is exactly "negate both X and Z, keep the same
already-computed bounding box" - confirmed by reading the source rather than
trusting the README's pre-existing claim about it - and is only ever true in
soccer mode for a kart on the blue team.  Rendering it against Hacienda (an
ordinary, markedly asymmetric race track) at first looked broken - almost the
whole mirrored track fell outside the frame - until re-reading
`Graph::makeMiniMap`'s camera setup confirmed STK really does reuse the
un-mirrored bounding box for the camera regardless of the mirror, so a
markedly asymmetric track *should* render that way once inverted; re-tested
against an actual soccer arena (`icy_soccer_field`, roughly symmetric by
design) and got a sane, fully-visible mirrored field, which is what confirmed
the implementation rather than the test track was the issue.

Deps are `python-pillow` and, optionally, `python-numpy`; the GUI additionally needs
`tk` and `python-pillow`'s ImageTk.

## Source of truth

`stk-code` on GitHub: `src/tracks/graph.cpp`, `drive_graph.cpp`, `arena_graph.cpp`,
`quad.cpp`, and `Track::loadMinimap` in `track.cpp`.
