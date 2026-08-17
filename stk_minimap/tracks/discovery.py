from __future__ import annotations

import glob
import os
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass

from .graph import Graph, get_bool, load_arena_graph, load_drive_graph, read_xml

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

    # an STK folder sitting next to the package (repo root, three levels up
    # from this file), or next to the cwd - covers the Windows portable zip,
    # where people drop the whole stk-minimap folder into the game folder
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
