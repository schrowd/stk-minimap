from __future__ import annotations

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

import html
import math
import os
import re
import sys
from dataclasses import dataclass, field

from ..tracks.discovery import default_track_dirs

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
    heading: list[float] = field(default_factory=list)   # radians in world XZ
    lap: list[int] = field(default_factory=list)     # 0-based, per frame

    def heading_at(self, i: int, frac: float = 0.0) -> float:
        """Heading between two frames, taking the short way round the circle."""
        if not self.heading:
            return 0.0
        h = self.heading[i]
        if frac <= 0.0 or i + 1 >= len(self.heading):
            return h
        d = (self.heading[i + 1] - h + math.pi) % (2 * math.pi) - math.pi
        return h + d * frac

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
            # which way the kart is *pointing*, from the recorded quaternion:
            # local +Z turned into world space.  Not the same as the direction
            # of travel - the difference is the slip angle, and it grows from
            # 0.3 degrees when straight to 25 when a red skid is charged.
            qx, qy, qz, qw = r[4], r[5], r[6], r[7]
            k.heading.append(math.atan2(2.0 * (qx * qz + qy * qw),
                                        1.0 - 2.0 * (qx * qx + qy * qy)))
        k.lap = split_laps(k, rp.laps)
        rp.karts.append(k)

    if not rp.karts:
        raise SystemExit(f"{path}: no usable frames")
    return rp
