# SuperTuxKart patches

These patch **SuperTuxKart itself**, not stk-minimap, to make a replay
controllable from outside the game - so that pausing, scrubbing or slowing a
run in one window does the same in the other.

Both sides are complete: a patched build listens on loopback and answers the
protocol, and the viewer has a client for it (`SyncClient` and the
`sync_*` methods, in the Replay tab). See [`PROTOCOL.md`](PROTOCOL.md) for
what's verified and how.

Nothing here is required to use stk-minimap. Without a patched game and
`--sync-port`, the viewer works exactly as it always has against the stock
game.

## Why patch the game at all

Investigated before writing any of it (full findings in
[`../docs/NOTES.md`](../docs/NOTES.md)). SuperTuxKart 1.5 has no pause, no
seek, no rewind and no speed control for replays, and exposes no IPC, socket
or scripting hook that an external process could drive. The only live per-kart
transform stream is the multiplayer network protocol, which is built for real
races between real clients - not for reading state out of an offline replay.

So sync is not achievable from outside the game. It needs a patch.

The good news, from reading the source: the ghost-replay system is already
structured in a way that makes this small rather than a rewrite.
`GhostKart::update()` positions a ghost by interpolating between two recorded
transforms using a **tick index** - position is a pure function of the clock,
not something integrated forward. And in watch-replay mode there is no physics
at all: `RaceManager::startWatchingReplay` sets `m_num_karts =
getNumGhostKart()` and marks every kart `KT_GHOST`. Nothing to unwind.
`WorldStatus` already has public `setTime()`, `setTicks()`, `pause()` and
`unpause()`.

## Patches

### `0001-ghost-controller-seekable.patch`

The one genuine blocker for rewind, and worth having on its own merits:
`GhostController::update()` only ever advanced its index **forward**, so
moving the clock backwards left the ghost frozen. Also fixes a kart staying
invisible after rewinding from the end of a replay, and is written so the
forward-playback path is provably untouched.

**Verified** against all 113 replays available locally and 20,000 random
seeks checked against an independent reference (0 wrong, against 18,181
wrong under the old logic). Full breakdown:
[`../docs/SYNCNOTES.md`](../docs/SYNCNOTES.md#0001---ghost-controller-seekable).

### `0002-replay-playback-control.patch`

Pause, seek and rate for a replay being watched. Adds `ReplayControl`
(`src/replay/replay_control.{hpp,cpp}`), a hook in `WorldStatus::updateTime`,
a `--replay-control` flag, and a reset in `World::reset` so a second viewing
doesn't inherit the first one's paused state.

It drives `m_time` directly rather than changing the main loop's tick rate,
so the ghosts, the on-screen timer and everything else reading
`World::getTime()` move together - the design reasoning is in
[`../docs/SYNCNOTES.md`](../docs/SYNCNOTES.md#0002---replay-playback-control).
Behaviour when the flag is absent is unchanged.

**Verified** with `replay_control_test.cpp` (24 checks against the real
class: rate changes, pause/resume, seek clamping and backward-seek-from-end,
end-of-playback handling, and clean under ThreadSanitizer with 4 threads
hammering seek/rate/play concurrently), plus a measured clock-quantisation
check (0 of 36,000 ticks deviate over 5 minutes at 120 Hz) and a runtime log
check. Full breakdown:
[`../docs/SYNCNOTES.md`](../docs/SYNCNOTES.md#0002---replay-playback-control).

### `0003-fix-replay-trailing-blank-line-crash.patch`

**A stock SuperTuxKart 1.5 crash, nothing to do with the rest of this.**
Independent of `0001` and `0002`, applies on its own, and is the most
straightforwardly upstreamable patch here.

Watching `wr_candela_city_202598_1_82_3725.replay` - a **world record replay
shipped with the game** - takes the whole game down:

```
[error] main: Exception caught : vector::_M_range_check:
        __n (which is 1) >= this->size() (which is 1).
[error] main: Aborting SuperTuxKart.
```

`ReplayPlay::loadFile()` skips a computed number of header lines, then loops
`fgets` → `readKartData()` until EOF. That file ends with a **trailing blank
line**, so the loop runs one extra iteration, entering `readKartData()` with
`kart_num == 1` on a one-element kart vector - which is the reported
`vector::_M_range_check` crash, exactly.

The fix has two parts:

1. `loadFile()` skips whitespace-only lines, so the spurious call never
   happens.
2. `readKartData()` bounds-checks before indexing and logs an error instead
   of throwing, so any *other* malformed file degrades to "that kart didn't
   load" rather than killing the process.

**Verified** with `replay_loadfile_test.cpp` against all 113 replays
available: exactly the one file crashes stock, zero crash patched. Exact
line-count arithmetic and the full test output:
[`../docs/SYNCNOTES.md`](../docs/SYNCNOTES.md#0003---fix-replay-trailing-blank-line-crash).

### `0004-replay-sync-server.patch`

The listener. Adds `ReplaySyncServer`
(`src/replay/replay_sync_server.{hpp,cpp}`) and a `--sync-port=<n>` flag which
implies `--replay-control`, speaking [`PROTOCOL.md`](PROTOCOL.md): line-based
ASCII over TCP, `PLAY`/`PAUSE`/`SEEK`/`RATE`/`PING` in,
`HELLO`/`REPLAY`/`DURATION`/`STATE`/`BYE` out.

Three things shaped the design: it never touches the main loop (a dedicated
thread just `poll()`s and applies commands to the already mutex-protected
`ReplayControl`), it binds `127.0.0.1` and never `INADDR_ANY`, and nothing
fails hard - a busy port, a bad client, a mid-write disconnect all log and
carry on. A replay loaded after the viewer connected is broadcast, not just
reported at connect time, which is what lets the map pick up a replay chosen
in-game after the fact. Full design writeup:
[`../docs/SYNCNOTES.md`](../docs/SYNCNOTES.md#0004---replay-sync-server).

**Verified** with `replay_sync_test.cpp` - 56 checks against the real server
and `ReplayControl` driven by a fake 120 Hz game loop, covering the protocol,
concurrency and framing edge cases, clean under ThreadSanitizer, AddressSanitizer
and UBSan - plus checks against the actual running game (real `LISTEN`, real
refusal from a LAN address, real behaviour with the flag absent). Full
breakdown, including the main-loop-blocking measurement:
[`../docs/SYNCNOTES.md`](../docs/SYNCNOTES.md#0004---replay-sync-server).

### The viewer side (`stk_minimap/sync/client.py`, `stk_minimap/gui/replay_tab.py`)

`SyncClient`, a background thread doing the actual socket I/O, and a set of
`sync_*` methods on `ReplayTabMixin`, in the Replay tab. Everything the
viewer already had - the play/pause button, the scrub bar, the rate combobox,
frame-stepping - now also drives the connected game, and the game driving
those same controls back moves the viewer. A replay loaded in-game is
auto-loaded here too, non-modally, so the map follows a replay picked in-game
with nothing open here beforehand.

The loop-safety split (local actions call `sync_send()`, inbound `STATE`
never calls a handler that could send something back) and the full
verification - 45 checks total across a fake server, garbage input, the
auto-follow path, and the real patched binary - are in
[`../docs/SYNCNOTES.md`](../docs/SYNCNOTES.md#the-viewer-side).

### Getting a patched build running (`build.sh`, the Launch button)

The first time this got tried for real, it didn't work - the running game had
no `--sync-port` flag at all, because starting it is a separate manual step
easy to get wrong or forget. Two things close that gap:

- **`patches/build.sh`** (below) builds the patched game with one command, to
  a predictable default location.
- **The GUI's "Launch SuperTuxKart" button** (Replay tab) starts that binary
  with `--sync-port=<the port already in the field next to it>` and the
  correct working directory, then starts trying to connect on its own -
  the two things that have to be right for sync to come up at all, taken out
  of the user's hands. `default_patched_stk_binary()` finds `build.sh`'s
  output automatically, so with nothing configured this is a single click.

**Verified** the whole chain together, driven through the real `App`: finds a
fresh build with no settings.json entry needed, clicking Launch starts the
real binary with the right argv and working directory, and the client
connects on its own within a few seconds. Details (including a real bug this
caught):
[`../docs/SYNCNOTES.md`](../docs/SYNCNOTES.md#getting-a-patched-build-running).

## Applying

```bash
./patches/build.sh
```

Clones `stk-code` at tag `1.5`, applies all four patches, symlinks assets
from an existing SuperTuxKart install if it finds one (see below), and builds
with cmake + ninja. Safe to re-run - an existing checkout is reused rather
than re-cloned, and the build itself is incremental. Defaults to
`~/.local/share/stk-minimap/stk-code` (`--dir` to use somewhere else,
`--jobs` to control parallelism); this is also where the stk_minimap GUI
looks for the result on its own, so for most people this one command plus the
GUI's **Launch SuperTuxKart** button (Replay tab) is the entire setup.

Verified: a clean run against a fresh clone builds successfully end to end,
a second run against the same checkout is a 0.8s no-op (`ninja: no work to
do`), and the resulting binary genuinely listens - confirmed both from the
shell and by connecting stk_minimap's real sync client to it.

Doing it by hand is the same four steps, if you'd rather:

```bash
git clone --depth 1 --branch 1.5 https://github.com/supertuxkart/stk-code.git
cd stk-code
git apply /path/to/stk-minimap/patches/0001-ghost-controller-seekable.patch
git apply /path/to/stk-minimap/patches/0002-replay-playback-control.patch
git apply /path/to/stk-minimap/patches/0003-fix-replay-trailing-blank-line-crash.patch
git apply /path/to/stk-minimap/patches/0004-replay-sync-server.patch
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_RECORDER=0
ninja -C build
```

(`-DBUILD_RECORDER=0` skips a hard dependency on `libopenglrecorder`, which
most distros don't package and which this project has no use for - a plain
tagged checkout treats a missing one as a fatal error rather than just
disabling the feature, unlike a `git describe`-style dev build.)

All four apply cleanly, in order, to a pristine `1.5` checkout - including the
new files `0002` and `0004` add. `sources.cmake` globs `src/**.cpp`
recursively, so no build-system change is needed for those.

The series is checked, not assumed: applying all four to a pristine tree
reproduces the development tree byte-for-byte, and the intermediate state
(`0001`–`0003`, without the sync server) compiles on its own.

The build can run against an existing installed copy of the game's data
rather than a separate `stk-assets` clone, which saves about a gigabyte -
`build.sh` does this automatically; by hand it's:

```bash
cd stk-code/data
for d in library models music sfx textures tracks karts; do
    ln -s /usr/share/supertuxkart/data/$d $d
done
```

**Whichever way you build it, launch it from the checkout root** (`cd
stk-code && ./build/bin/supertuxkart ...`), not from `build/bin/` directly -
the game finds its `data/` directory by walking up from the working
directory (`FileManager::discoverPaths`), and the wrong cwd is a fatal "could
not find file 'supertuxkart.1.5'" with nothing about `--sync-port` in it,
which reads like something else entirely broke. `build.sh`'s printed run
command and the GUI's Launch button both already get this right.

Running the tests:

```bash
g++ -O2 -o ghost_index_test patches/ghost_index_test.cpp
./ghost_index_test ~/.local/share/supertuxkart/replay/some_run.replay

g++ -std=c++17 -O1 -Istk-code/src -pthread -o replay_control_test \
    patches/replay_control_test.cpp stk-code/src/replay/replay_control.cpp
./replay_control_test

g++ -O2 -o loadfile_test patches/replay_loadfile_test.cpp
./loadfile_test /usr/share/supertuxkart/data/replay/*.replay

g++ -std=c++11 -pthread -Ipatches/testinc -Istk-code/src -o replay_sync_test \
    patches/replay_sync_test.cpp \
    stk-code/src/replay/replay_sync_server.cpp \
    stk-code/src/replay/replay_control.cpp
./replay_sync_test
```

`replay_sync_test` needs `patches/testinc` on the include path: the real
`utils/log.hpp` and `utils/constants.hpp` drag in most of the game, so those
two - and only those two - are stubbed. The code under test is the shipped
source, not a copy of it. Takes about 15 s (it waits on real timers) and
binds port 27991 while it runs.

The viewer-side and end-to-end tests are Python, in `patches/tests/`:

```bash
cd patches/tests
python3 test_sync_client.py           # fake server, real App
python3 test_sync_client_garbage.py   # malformed input from the server
python3 test_launch_button.py         # Launch button: argv, cwd, detachment
python3 test_autoload.py              # a replay loaded in-game is followed
python3 test_full_loop.py             # zero-config: discover, launch, connect
python3 test_sync_real_stk.py         # the actual patched binary as server
python3 test_autoload_real_stk.py     # same, for the auto-follow specifically
```

Each is a standalone script (`python3 <file>.py`), not a pytest suite - matching
the rest of this project's style. All of them build and drive the real
`stk_minimap.gui.app.App` (see `_paths.build_app()`) rather than a mock, and
none of them can touch a real `~/.config/stk-minimap/settings.json` -
`_paths.py` redirects `XDG_CONFIG_HOME`/`APPDATA` to a throwaway directory
before `stk_minimap` is even imported.

`test_sync_client.py` and `test_autoload.py` need one or two locally-recorded
Hacienda replays to run against and skip (not fail) if none are found -
record a lap or two on Hacienda first if you want them to actually run.
`test_full_loop.py`, `test_sync_real_stk.py` and `test_autoload_real_stk.py`
need a real patched build at `patches/build.sh`'s default location and skip
likewise if there isn't one. The last two launch the real game - expect a
window to open, and `test_autoload_real_stk.py` in particular takes close to
a minute (it drives `--benchmark -N` to reach a genuine watch-replay
`World::reset()` headlessly, since the replay menu itself can't be
automated).

`patches/tests/mkpatches.py` is not a test - it's what regenerates the four
`.patch` files above from a working dev tree (`$STK_DEV_TREE`, default
`~/stk-code`) whenever their contents change. It replays the series into a
scratch `git worktree` one commit at a time, since several files carry hunks
belonging to different patches.

## Intent

These are written to be **reviewable, and ideally upstreamable** - not as a
private fork. That constrains the design: no new dependencies, no behaviour
change unless explicitly opted into, and the default build byte-for-byte the
game people already run. Patch `0001` in particular is a plain bug fix that
stands on its own, independent of any sync feature.

A fork that speedrunners have to install instead of the official game is a
real cost - maintenance, trust, and one more thing to go stale at the next STK
release. Keeping each piece small and defensible is what keeps that cost from
being permanent.
