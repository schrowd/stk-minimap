"""
stk_minimap - render a SuperTuxKart minimap to a PNG.

Requires: Pillow.  numpy is used if present (nicer alpha-correct downscaling).

Examples
--------
    python3 -m stk_minimap hacienda            # look the track up in the STK data dirs
    python3 -m stk_minimap ~/tracks/mytrack -o map.png
    python3 -m stk_minimap cornfield_crossing --style clean --size 1024
    python3 -m stk_minimap battleisland --style clean --fit
    python3 -m stk_minimap --list
    python3 -m stk_minimap --all -O ./minimaps --style clean
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from . import __version__
from .replay.analysis import compute_splits, format_splits, write_replay_csv, write_splits_csv
from .rendering.styles import STYLES
from .tracks.discovery import default_track_dirs, find_tracks, read_track_info, resolve_track
from .tracks.minimap import _read_replay_or_die, build
from .tracks.scene import load_checklines


def _launch_gui(extra_dirs: list[str]) -> int:
    """
    Deferred import, so `python -m stk_minimap --list` never has to touch
    tkinter - the command line works without it.
    """
    try:
        import tkinter  # noqa: F401
    except ImportError:
        sys.exit("The GUI needs tkinter, which your Python is missing.\n"
                 "  Arch/Manjaro : sudo pacman -S tk\n"
                 "  Debian/Ubuntu: sudo apt install python3-tk\n"
                 "  Fedora       : sudo dnf install python3-tkinter\n"
                 "  macOS/Windows: reinstall Python from python.org (it bundles it)\n"
                 "The command line still works without it - see --help.")
    try:
        from PIL import ImageTk  # noqa: F401
    except ImportError:
        sys.exit("The GUI needs Pillow's ImageTk module (package python3-pil.imagetk "
                 "on Debian/Ubuntu).")
    from .gui.app import run_gui
    return run_gui(extra_dirs)


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

    # the repo root: this file lives at <root>/stk_minimap/cli.py
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name=STK Minimap\n"
        "GenericName=Minimap renderer\n"
        "Comment=Render SuperTuxKart track minimaps to PNG\n"
        f"Exec={dq(sys.executable)} -m stk_minimap --gui\n"
        f"Path={dq(repo_root)}\n"
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
    ap.add_argument("--checklines", action="store_true",
                    help="draw the track's check lines from scene.xml - red "
                         "for the lap line, cyan for the ordered gates that "
                         "stop a shortcut counting")
    ap.add_argument("--no-seal", action="store_true",
                    help="don't weld hairline gaps between quads (always off for "
                         "--style exact, which stays pixel-faithful)")

    ap.add_argument("--fit", action="store_true",
                    help="crop to the track instead of STK's square letterboxed view "
                         "(breaks mapPoint2MiniMap compatibility)")
    ap.add_argument("--rotate", type=float, default=0.0, metavar="DEGREES",
                    help="turn the map clockwise by this many degrees "
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

    ap.add_argument("--replay", metavar="FILE",
                    help="overlay a .replay run's route on the map; the "
                         "track argument can be omitted, it's read from the "
                         "replay itself")
    ap.add_argument("--compare", metavar="FILE",
                    help="overlay a second .replay alongside --replay")
    ap.add_argument("--replay-colour", choices=("speed", "nitro", "plain"),
                    default="speed",
                    help="how to colour the --replay route (default speed)")
    ap.add_argument("--replay-lap", default="all", metavar="all|N",
                    help="'all' or a 1-based lap number to draw (--replay only)")
    ap.add_argument("--csv", metavar="FILE",
                    help="write --replay's per-frame telemetry to a CSV file")
    ap.add_argument("--splits", action="store_true",
                    help="print --replay's sector splits (needs check lines)")
    ap.add_argument("--splits-csv", metavar="FILE",
                    help="write --replay's sector splits to a CSV file")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--version", action="version",
                    version=f"stk_minimap {__version__}")

    args = ap.parse_args(argv)
    tmpdirs: list[str] = []

    if (args.csv or args.splits or args.splits_csv) and not args.replay:
        ap.error("--csv / --splits / --splits-csv need --replay")
    if args.compare and not args.replay:
        ap.error("--compare needs --replay")

    if args.install_desktop:
        return install_desktop_entry(args.quiet)

    # double-clicking the script in Explorer passes no arguments; a usage error
    # in a console that closes instantly is useless, so open the window instead
    if args.gui or (os.name == "nt" and not sys.argv[1:]):
        return _launch_gui(args.data_dir)

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
            if args.replay:
                # the replay names its own track, so it needn't be typed twice
                args.track = _read_replay_or_die(args.replay,
                                                 header_only=True).track
            else:
                ap.error("give a track (or --list / --all)")

        track_dir = resolve_track(args.track, args.data_dir, tmpdirs)
        img, fr, g, ti = build(track_dir, args)
        out = args.output or f"{ti.ident}_minimap.png"
        img.save(out)

        if args.replay and (args.csv or args.splits or args.splits_csv):
            rp = _read_replay_or_die(args.replay)
            kart = rp.karts[0]
            if args.csv:
                write_replay_csv(args.csv, [(kart.name or kart.ident or "A", kart)])
                if not args.quiet:
                    print(f"  csv        : {args.csv}  ({len(kart)} rows)")
            if args.splits or args.splits_csv:
                checks = load_checklines(track_dir)
                splits = compute_splits(kart, checks)
                if args.splits:
                    print()
                    print(format_splits(splits))
                if args.splits_csv:
                    write_splits_csv(args.splits_csv, splits)
                    if not args.quiet:
                        print(f"  splits csv : {args.splits_csv}  ({len(splits)} laps)")

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
