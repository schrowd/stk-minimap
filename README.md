# STK Minimap

Render any SuperTuxKart track's minimap to a PNG — the same image the game
builds for the corner of your screen, but offline, at any resolution.

<p align="center">
  <img src="docs/hero_hacienda.png" width="480" alt="Hacienda minimap">
</p>

SuperTuxKart doesn't ship minimap images. There is no file to extract: the game
builds the minimap at track load time from the track's AI graph, uploads it to a
texture, and throws it away when you quit. This script reimplements that path in
software, so you can get the image without running the game.

Useful if you're making route diagrams, split overlays, track guides, or
anything that needs to draw on top of the in-game minimap — `--style exact`
reproduces the game's texture exactly, so overlays line up pixel for pixel.

<p align="center">
  <img src="docs/ex_cornfield.png" width="240" alt="Cornfield Crossing">
  <img src="docs/ex_battleisland.png" width="240" alt="Battle Island">
  <img src="docs/ex_snowtuxpeak.png" width="240" alt="Northern Resort">
</p>

---

## Quick start

You need **Python 3.9+** and **Pillow**. NumPy is optional but recommended (it
makes the downscaling alpha-correct and a bit sharper).

### Windows

1. Install Python from [python.org](https://www.python.org/downloads/) — tick
   **"Add python.exe to PATH"** during setup.
2. Open Command Prompt and run:
   ```
   pip install pillow numpy
   ```
3. Download `stk_minimap.py` (and `STK Minimap.pyw`, optional) from this repo.
4. **Double-click `stk_minimap.py`** — the window opens and finds your tracks by
   itself. Double-click `STK Minimap.pyw` instead if you'd rather not have a
   console window behind it. (That `.pyw` trick is Windows-only — the extension
   has no effect on Linux or macOS.)

### Linux

```bash
sudo pacman -S python-pillow python-numpy tk      # Arch / Manjaro
sudo apt install python3-pil python3-numpy python3-tk python3-pil.imagetk   # Debian / Ubuntu
sudo dnf install python3-pillow python3-numpy python3-tkinter               # Fedora

chmod +x stk_minimap.py
./stk_minimap.py --gui
```

To get a clickable launcher instead of typing that every time:

```bash
./stk_minimap.py --install-desktop
```

**STK Minimap** then appears in your applications list, searchable and
pinnable, and opens the window with no terminal involved.

This is the only way to launch it by clicking on modern GNOME: Files removed
the ability to run executable text files, so double-clicking a `.py` opens it
in a text editor and there's no setting to change that. Undo it by deleting
`~/.local/share/applications/stk-minimap.desktop`.

### macOS

```bash
pip3 install pillow numpy
python3 stk_minimap.py --gui
```

`tk` / `python3-tk` is only needed for the GUI — the command line works without
it.

---

## The window

`--gui`, or just double-click the script on Windows.

<p align="center">
  <img src="docs/gui.png" width="720" alt="The STK Minimap window">
</p>

Pick a track from the list on the left and the preview updates immediately.
Filter it by **type** (race, arena, soccer) and by **source** (built-in or
add-ons), tick **Show in-game names** to list them as *Antediluvian Abyss*
rather than `abyss`, and search either form. Cutscenes and grand-prix screens
are left out — they carry no graph, so there's nothing to draw.
Set the style, output size and quality, tick what you want drawn, then
**Save PNG…**. **Save every track…** batches the whole list into a folder.

If your tracks aren't found automatically, **Add folder…** points it at any
directory of tracks, and **Open .zip…** takes an addon archive as downloaded
from the STK add-ons site.

Transparent `exact` renders are previewed over a checkerboard, otherwise they'd
look like an empty box.

### Watching a replay

**Browse replays…** lists every replay on your machine in a sortable table —
your own runs *and* the 21 world records and challenge ghosts that ship with
SuperTuxKart 1.5. Click any column to sort:

| Column | |
|---|---|
| **Track** | in-game name; flags anything whose track isn't installed |
| **Time** | the run's time, fastest first by default |
| **Laps**, **Driver** | from the replay header |
| **Date** | when the run was set, read from STK's filename (the shipped records all share a packaging date, so their file timestamps are useless) |
| **Source** | World record, Ghost, Challenge, or Yours |

**Show** narrows it to one source, and the search box matches track, driver or
filename. Selecting a run shows its description underneath — that's how you
tell a current world record from a former one. **Load** opens it; double-click
does the same.

So the fastest legal run of any track is two clicks away: **Browse replays…**,
**Show → World record**.

**Open replay…** takes a `.replay` file from anywhere if you'd rather pick it
by hand.

Press **▶** to play, drag the slider to scrub or rewind, and pick a rate from
`0.1x` to `4x` to crawl through a corner in slow motion. The live readout shows
the lap, speed in km/h, nitro in hand, items held, and which of nitro/skid/
zipper is active on the current frame.

**Lap** decides how much of the run is drawn. A three-lap run stacked on one
piece of track is unreadable, so the default is **Follow** — only the lap the
playhead is in. Pick **All** for the whole run, or a specific lap to compare
one against another.

**Colour** picks *one* thing to show, rather than layering them:

| Mode | Draws |
|---|---|
| **Speed** | blue where you're slow through to red where you're quick — where a run loses time, at a glance |
| **Nitro & skid** | a dim route, cyan where nitro was burning, yellow and red where a skid was charged |
| **Plain** | just the line |

**Items / zippers** adds a yellow ring where a zipper fired and a pink diamond
where an item was used.

#### Reading the kart marker

The marker carries two rings, so you can see what the kart is doing at the
moment it does it:

| Ring | Meaning |
|---|---|
| **Black**, close in | skidding, not yet charged |
| **Yellow**, close in | yellow skid earned |
| **Red**, close in | red skid earned |
| **Cyan**, further out | nitro burning |

They're at different radii, so a red skid while on nitro shows both at once.
The skid charge comes from the replay's own `skidding_effect` column, which
steps up as you hold the skid — the same thing that turns the sparks yellow
then red in game.

#### Comparing two runs

**Compare with…** loads a second replay of the same track and plays both
together — run **A** and run **B**, each with its own colour, marker and
readout line.

Underneath them is the gap:

```
Δ  B behind by 7.03s at this point on track
```

That's the difference measured **at the same place on the track**, not at the
same moment in time — the same thing a ghost shows you. Positions at the same
timestamp only tell you who's further ahead; the time difference at a given
corner is what tells you where a run was actually won or lost. Scrub through
and watch the number grow or shrink to find the sections that cost you.

Both runs must be on the same track, or it'll say so rather than compare
nonsense. Loading a new run into **A** clears the comparison.

The obvious use: load a world record as **A**, then **Browse replays…** →
**Compare with loaded run** on your own attempt, and scrub through to see
exactly which corners the record is taking better.

Replays with more than one kart — a run recorded against a ghost — already
contain two karts, and are drawn the same way.

The map has to match the replay: opening one selects its track for you, and if
that track isn't installed it'll say so rather than draw the run on the wrong
map.

---

## Command line

```bash
./stk_minimap.py hacienda                        # -> hacienda_minimap.png
./stk_minimap.py hacienda --style clean --title  # readable, with the track name
./stk_minimap.py battleisland -s 1024            # 1024x1024
./stk_minimap.py --list                          # what can I render?
./stk_minimap.py --all -O ./minimaps --style clean    # everything, into a folder
./stk_minimap.py ~/Downloads/mytrack.zip --style clean    # an addon archive
./stk_minimap.py /path/to/some/track_folder      # an unpacked track
```

`--list` on a stock SuperTuxKart 1.5 install finds 44 tracks; 38 of them have a
graph to render (the other six are cutscenes and grand-prix result screens,
which have no driveline).

### Options

| Flag | What it does |
|---|---|
| `-o, --output` | output file (default `<ident>_minimap.png`) |
| `-O, --output-dir` | where `--all` writes |
| `-s, --size` | output size in pixels, default 512 |
| `--style` | `exact`, `clean` or `blueprint` |
| `--background` | override the background, e.g. `'#101418'` or `none` |
| `--outline` | outline width in pixels; `0` turns it off (default: none for `clean`, 1 for `blueprint`) |
| `--title` | draw the track's display name |
| `--supersample` | antialiasing factor, default 4 (the game itself uses 2) |
| `--fit` | crop to the track instead of the game's square view |
| `--margin` | padding fraction, `--fit` only |
| `--reverse` | reverse mode — honours `direction="forward\|reverse"` quads |
| `--show-invisible` | also draw quads the game hides |
| `--invert-x-z` | mirror X and Z, as the game does for the blue soccer team |
| `--full-polys` | navmesh: draw >4-sided faces in full (the game truncates them) |
| `--data-dir` | extra directory to search, repeatable |
| `--no-seal` | don't weld hairline gaps between quads |
| `--gui` | open the window |
| `--install-desktop` | Linux: add a launcher to the applications menu |
| `--version` | print the version |

---

## Styles

<p align="center">
  <img src="docs/styles.png" width="720" alt="exact, clean and blueprint styles">
</p>

**`exact`** (default) — what the game actually renders: white at alpha 127 on a
fully transparent background. On its own it looks blank in most image viewers;
that's correct, and it's the one to use if you're compositing over the in-game
minimap. Shown above over grey so you can see it at all.

**`clean`** — light track on a dark background. What you want for a guide, a
diagram, or a Discord post. Deliberately has no outline: a stroke thinner than
the antialiasing ramp can't survive the downscale as its own colour, so it just
softens the edge instead of defining it. Add one with `--outline 2` if you want
it. **`blueprint`** — the same geometry in a schematic blue; this one keeps its
outline, because its fill is translucent and would barely register without it.

`clean` and `blueprint` are cosmetic and may change between versions. `exact` is
a contract and won't.

---

## Drawing on top of the minimap

The `exact` style reproduces the game's framing, so the mapping from world
coordinates to pixels is the same one the game uses to place the kart markers
(`Graph::mapPoint2MiniMap`):

```
px = (world_x - origin_x) * scaling
py = height - (world_z - origin_z) * scaling      # PNG rows count from the top
```

The script prints those three numbers for you:

```
$ ./stk_minimap.py hacienda
Hacienda  [hacienda]  driveline
  quads      : 109 (109 visible)
  bbox       : x -5.11..302.39   y -20.99..9.01   z -140.00..178.27
  scaling    : 1.60872 px per world unit
  world->px  : px = (x - -5.107) * 1.60872
               py = 512 - (z - -139.999) * 1.60872
```

So a kart at world `(150, ?, 20)` on a 512px Hacienda minimap sits at
`((150 + 5.107) * 1.60872, 512 - (20 + 139.999) * 1.60872)` = `(249.5, 254.6)`.

Two things to know:

- **Height (`y`) is ignored.** The minimap is a flat top-down projection.
- **Non-square tracks get empty padding on one side.** The game's orthographic
  box is square — `range = max(width, depth)` — and it's deliberately anchored
  so that the mapping above stays a plain affine transform. That padding is
  faithful, not a bug. `--fit` crops it away, and doing so **breaks the
  mapping**, so don't use `--fit` for overlays.

---

## Where it looks for tracks

Windows
- `C:\Program Files\SuperTuxKart*\data\tracks` (and the x86 / `PROGRAMW6432` variants)
- `%APPDATA%\supertuxkart\addons\tracks` — tracks you downloaded in-game
- Steam libraries, found by reading `steamapps\libraryfolders.vdf`
- portable zips extracted to Desktop, Downloads, Documents, Games, `C:\`, `D:\`
- a `data\tracks` folder next to the script, so you can drop it into the game folder

Linux
- `/usr/share/supertuxkart/data/tracks` and the usual `/usr/local`, `/opt`,
  `/usr/share/games` variants
- `~/.local/share/supertuxkart/addons/tracks`, `~/.supertuxkart/addons/tracks`
- Flatpak and Snap locations, Steam libraries
- `~/*/stk-assets/tracks` for source checkouts

macOS
- `/Applications/SuperTuxKart*.app/Contents/Resources/data/tracks`
- `~/Library/Application Support/SuperTuxKart/addons/tracks`, Steam libraries

Replays are looked for in two places on every platform: the folder STK records
into (`%APPDATA%\supertuxkart\replay` on Windows,
`~/.local/share/supertuxkart/replay` on Linux,
`~/Library/Application Support/SuperTuxKart/replay` on macOS), and the `replay`
folder that sits next to the `tracks` folder of whichever install was found
above — which is where the shipped world records live. Pointing the tool at a
different install with `--data-dir` picks up that install's records too.

If none of that matches your setup, set `STK_TRACK_DIR` or pass `--data-dir`:

```bash
STK_TRACK_DIR="/somewhere/else/tracks" ./stk_minimap.py --list
./stk_minimap.py --data-dir "/somewhere/else/tracks" --list
```

---

## How faithful is it?

This is a reimplementation of `Graph::makeMiniMap` and its callees from
[stk-code](https://github.com/supertuxkart/stk-code) (`src/tracks/graph.cpp`,
`drive_graph.cpp`, `arena_graph.cpp`, `quad.cpp`). Race tracks are read from
`quads.xml` (the driveline graph), arenas and soccer fields from `navmesh.xml`.

Behaviours that are reproduced on purpose, because getting any of them wrong
shifts or distorts the image:

- The bounding box includes **invisible** quads — the game grows it in
  `createQuad`, before any visibility filtering.
- The orthographic box is square and offset toward the shorter axis, anchoring
  the track at the bounding-box minimum on both axes.
- Overlapping quads **overwrite** rather than blend, because the game's material
  is opaque.
- The lap line is node 0's quad, shortened to 3% of the track's Z extent, drawn
  in red — and only for race tracks, never arenas.
- `p0="3:2"` in `quads.xml` means "point 2 of quad 3"; most quads share an edge
  with their neighbour this way.
- Navmesh faces aren't always four-sided. The game reads the first four indices
  unconditionally, so this does too; `--full-polys` opts out.

Rendering is supersampled and resolved with a premultiplied box filter, which
keeps `exact` landing on alpha 127 exactly. (Lanczos rings past 127 and breaks
that guarantee, so it's only used as a fallback when NumPy isn't installed.)

If you want to change any of that, read [docs/NOTES.md](docs/NOTES.md) first — it
records the research behind these decisions and the traps in reproducing them.

### Known gaps

- Soccer goal-line node colouring (`ArenaGraph::differentNodeColor`) isn't
  implemented — it needs a Dijkstra pass over the navmesh.
- CTF flag bounding-box expansion isn't implemented.
- `height-testing` elements are parsed and ignored; they only affect physics.
- Only the `default` `<mode>` from `track.xml` is honoured.

---

## Troubleshooting

**The PNG looks blank / empty.** That's `--style exact` doing its job — it's
white at alpha 127 on a transparent background, so a white image viewer shows
nothing. Use `--style clean`, or `--background '#101418'`.

**"No tracks found."** Pass `--data-dir /path/to/tracks`, set `STK_TRACK_DIR`,
or use **Add folder…** in the GUI. Run `--list` to see which directories were
searched.

**"The GUI needs tkinter."** Install it: `sudo pacman -S tk` (Arch),
`sudo apt install python3-tk python3-pil.imagetk` (Debian/Ubuntu),
`sudo dnf install python3-tkinter` (Fedora). Windows and macOS builds from
python.org already include it. The command line works without it.

**Hairline gaps between sections of track.** The game nudges every quad along
its own surface normal, so on sloped ground neighbours can miss each other by a
sliver. `clean` and `blueprint` weld those shut; `exact` deliberately doesn't,
to stay pixel-faithful. `--no-seal` turns the welding off.

---

## Credits

SuperTuxKart is made by the [SuperTuxKart team](https://supertuxkart.net/); all
the track data and the minimap algorithm are theirs. This is an independent tool
and isn't affiliated with or endorsed by the project.

Licensed under the **GPL-3.0**, the same licence as SuperTuxKart's code — see
[LICENSE](LICENSE).
