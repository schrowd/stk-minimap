# SuperTuxKart patches

These patch **SuperTuxKart itself**, not stk-minimap, to make a replay
controllable from outside the game - so that pausing, scrubbing or slowing a
run in one window does the same in the other.

Both sides are complete: a patched build listens on loopback and answers the
protocol, and `stk_minimap.py` has a client for it (`SyncClient` and the
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

The one genuine blocker for rewind, and worth having on its own merits.

`GhostController::update()` only ever advances its index **forward**:

```cpp
while (m_current_index + 1 < m_all_times.size() &&
       m_current_time >= m_all_times[m_current_index + 1])
    m_current_index++;
```

Move the clock backwards and the index is stuck, so the ghost freezes. The
patch adds a matching backward walk. It is deliberately written so the
**forward path is untouched** - during normal playback the new loop provably
never executes, because after the forward search `m_all_times[index]` is
always `<= m_current_time` and the next frame's time is larger still.

It also fixes a second latch in `GhostKart::update()`: reaching the end of a
replay calls `m_node->setVisible(false)`, and in watch-replay mode nothing
ever set it back - so rewinding from the finish left an invisible kart.

**Verified** with `ghost_index_test.cpp` (see below), which lifts both the old
and new index logic out verbatim and drives them against real replay
timings, checking both against an independently-computed reference:

| | old | new |
|---|---|---|
| ordinary forward playback | correct | correct - provably unchanged |
| 20,000 random seeks | **18,181 wrong** | 0 wrong |
| rewind from the end | **stuck at the last index** | correct |

Run across all 113 replays available locally (57 recorded plus the 51 that
ship with 1.5): the patched logic matches the reference in every case, on
every file.

### `0002-replay-playback-control.patch`

Pause, seek and rate for a replay being watched. Adds `ReplayControl`
(`src/replay/replay_control.{hpp,cpp}`), a hook in `WorldStatus::updateTime`,
a `--replay-control` flag, and a reset in `World::reset` so a second viewing
doesn't inherit the first one's paused state.

**It drives `m_time` directly rather than changing the main loop's tick
rate.** That is the important design decision. Every kart in watch-replay
mode is a ghost whose position is a pure function of the race clock, so
moving that one clock moves the ghosts, the on-screen timer, lap counters and
anything else reading `World::getTime()` *together*. Rate-limiting the main
loop instead would leave the game's own replay UI running on a different
clock from the karts, which is exactly the desync worth avoiding.

Concretely, in `CLOCK_CHRONO`:

```cpp
if (rc && rc->isEnabled() && RaceManager::get()->isWatchingReplay())
    setTime((float)rc->advance(stk_config->ticks2Time(1)));
else
    { m_time_ticks++; m_time = stk_config->ticks2Time(m_time_ticks); }
```

Behaviour when the flag is absent is the original statement, unchanged.

**Verified** with `replay_control_test.cpp`, which compiles the real class
(not a copy) and drives it - 24 checks, all passing:

- rate 1 counts up identically to a plain accumulator over 10s
- pause holds the time; resume continues from the same point
- 0.25x and 4x cover the expected span; rate 0, negative and absurd rates
  are all refused or clamped
- seek clamps at both ends, and **can seek backwards from the end** - the
  case patch `0001` exists to make work
- playback holds at the end instead of running past it, and pressing play
  from the end restarts from the top
- with no known duration, no upper clamp is applied
- the dirty flag is set by seek/pause/rate but *not* by ordinary playback
- 4 threads hammering seek/rate/play while the main thread ticks: no torn
  state, and **clean under ThreadSanitizer**

One thing measured rather than assumed: `setTime()` quantises to integer
ticks via `int(t * fps)` while the accumulator is in seconds, so the round
trip could in principle truncate and make the clock lag. Measured over 5
minutes of ticks at 120 Hz: **0 of 36,000 ticks deviate**, worst deviation
0 ms. The accumulate-then-quantise path is exact at rate 1.

Runtime-verified too: `--replay-control` logs
`ReplayControl: Replay playback control enabled.` and the log line is absent
without the flag. (Worth noting the first attempt at that check was
worthless - STK doesn't reject unknown flags, so a flag being *accepted*
proves nothing, and `--version` exits before command-line handling runs at
all.)

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
line**, so the loop runs one extra iteration. The arithmetic is exact:

| | |
|---|---|
| header lines skipped | 13 |
| `size:` line | 1 |
| data rows (`size: 1304`) | 1304 |
| **expected total** | **1318** |
| **actual lines in file** | **1319** |

The extra call enters `readKartData()` with `kart_num == 1`, which does
`m_kart_list.at(1)` on a one-element vector - and it does that *before*
validating that the line is even a `size:` header, so there is no chance to
reject it gracefully. Hence `at(1)` on `size() == 1`, matching the reported
error exactly.

The fix has two parts:

1. `loadFile()` skips whitespace-only lines, so the spurious call never
   happens.
2. `readKartData()` bounds-checks before indexing and logs an error instead
   of throwing, so any *other* malformed file degrades to "that kart didn't
   load" rather than killing the process. The unsigned
   `kart_num - first_loaded_f_num` would wrap on underflow; the same
   comparison catches that too.

**Verified** with `replay_loadfile_test.cpp`, which lifts the real header
arithmetic and read loop and counts how many `readKartData()` calls each file
would produce, against all 113 replays available:

```
  wr_candela_city_202598_1_82_3725.replay   karts=1  stock=2 <-- CRASH  patched=1 ok

113 replays checked
  stock 1.5 : 1 would call readKartData too many times (crash)
  patched   : 0
```

One file in 113, which is why every other replay plays fine and this one
reliably kills the game.

### `0004-replay-sync-server.patch`

The listener. Adds `ReplaySyncServer`
(`src/replay/replay_sync_server.{hpp,cpp}`) and a `--sync-port=<n>` flag which
implies `--replay-control`, speaking [`PROTOCOL.md`](PROTOCOL.md): line-based
ASCII over TCP, `PLAY`/`PAUSE`/`SEEK`/`RATE`/`PING` in,
`HELLO`/`REPLAY`/`DURATION`/`STATE`/`BYE` out.

Three things shaped the design.

**It never touches the main loop.** The server owns a thread that does
nothing but `poll()`, and applies commands straight to `ReplayControl`, which
was already mutex-protected. No queue, no callback into the game, no lock the
main loop can wait on - so a viewer that hangs, floods or vanishes cannot
affect the frame rate. A command still takes effect on the very next tick.

**It binds `127.0.0.1`, never `INADDR_ANY`.** This opens a control channel
into a running game; it has no business being reachable off the machine.

**Nothing fails hard.** A busy port, a bad port number, a client that
disconnects mid-write - all log and carry on. Unknown verbs are ignored rather
than rejected, so a newer viewer can talk to an older game.

`World::reset()` also learns the replay's length, from the longest ghost's
last *recorded* time rather than its finish time: the recording carries on
past the finish line and that tail should still be scrubbable.

**A replay loaded after the viewer connected is broadcast, not just reported
at connect time.** This is what makes "start the game, pick a replay, and the
map follows on its own" work, and it is the normal order of events when the
game is launched from the viewer. The server compares the loaded name each
cycle rather than relying on a flag, which also covers swapping one replay for
another; re-loading the *same* one is deliberately not re-announced, since
`World::reset()` runs for every new world and a viewer shouldn't reload the
file it is already showing.

**Verified** with `replay_sync_test.cpp`, which compiles the real server and
the real `ReplayControl` against a fake 120 Hz game loop - 56 checks covering
the greeting, the 10 Hz heartbeat, pause/seek/rate/ping, clamping, multiple
viewers, the connection cap, disconnect and reconnect, announcing a replay
loaded mid-session (and *not* re-announcing an unchanged one), and shutdown.
All passing, over 10 consecutive runs, and clean under **ThreadSanitizer**,
AddressSanitizer and UBSan.

Framing is tested where it actually breaks: a command split across two
writes, three commands in one packet, CRLF endings, binary junk, and a 20 kB
line with no newline. One case is worth stating because it is deliberate - a
half-written line blocks later commands *on that connection* until it is
terminated, which is what a line protocol should do.

Two claims checked against the running game rather than the harness:

| | |
|---|---|
| with `--sync-port=27982` | `LISTEN 127.0.0.1:27982`, greeting and commands work |
| without it | no socket, no thread, no log line, connection refused |
| from the machine's LAN address | `ConnectionRefusedError` |
| port already in use | logs, game continues |

The important measurement is the one about blocking. With a viewer
connected and deliberately never reading, over 2 seconds: the fake main loop
ran **237 ticks** (unimpeded) and the longest `advance()` call was **2 µs**.

### The viewer side (`stk_minimap.py`)

`SyncClient`, a background thread doing the actual socket I/O, and a set of
`sync_*` methods on the GUI's `App` class, in the Replay tab. Everything the
viewer already had - the play/pause button, the scrub bar, the rate combobox,
frame-stepping - now also drives the connected game, and the game driving
those same controls back moves the viewer.

Structurally, the two directions never cross: local input handlers
(`rp_toggle`, `rp_scrub`, `on_key_step`, `on_key_end`, the rate combobox's
trace) call `sync_send()`, which both transmits and starts a 250ms local-wins
holdoff. Applying an inbound `STATE` (`sync_on_state()`) only ever writes
`rp_t`/`rp_playing`/the rate display directly - it never calls a handler that
would send something back. That split is what makes PROTOCOL.md's loop-safety
rules hold structurally rather than by care at each call site.

When a `REPLAY <path>` arrives - at connect, or later because a replay was
just picked in-game - the viewer looks for a same-named file in the usual
replay folders and loads it automatically, so **the map follows a replay
loaded in the game with nothing open here beforehand.** If nothing matches, it
says so in the status bar rather than guessing.

That auto-load is deliberately non-modal (`use_replay(quiet=True)`): a load
the user didn't ask for shouldn't throw an error dialog in their face because
the game happens to be watching a run on a track they don't have. It also
clears the local-action holdoff rather than sending a `PING` to fetch the
current position - a `PING` through `sync_send()` would start a holdoff and
suppress the very `STATE` it was asking for.

Reconnection is the client's job as much as the server's: `SyncClient` retries
every 2s while a connection is wanted, since the protocol requires both ends
to tolerate the other coming and going.

**Verified** three ways, all against the real `App` - a real Tk root, real
widgets, the real `command=`/`trace_add` handlers, driven through
`root.update()` rather than a mocked event loop:

- 20 checks against a fake server that speaks this exact protocol: connects,
  auto-loads the matching replay, follows STATE (play/pause/rate/time all
  tested), local actions win over a stale STATE within the holdoff window,
  reconnects on its own after the fake server restarts, disconnect actually
  tears the client down, and the two "nothing to sync" cases (an unmatched
  replay, no replay at all) neither crash nor invent state.
- 5 checks feeding the client garbage: an unknown verb, non-numeric `STATE`
  fields, a verb split across two writes, binary junk, an empty `REPLAY` path.
  None of it raises out of the Tk main loop or produces a malformed state
  update - unknown/bad input is dropped, matching `_handle()`'s per-verb
  parsing.
- 14 checks on the auto-follow specifically, the scenario being: connected
  with nothing loaded on either side, then a replay is loaded in-game. Covers
  the map picking it up by itself and then tracking the game's clock,
  switching to a *different* replay mid-session, a replay announced part-way
  through being picked up at that position rather than at the start line, and
  a replay the user doesn't have locally being explained rather than crashing
  or clearing the view.
- 6 checks against the **actual patched `supertuxkart` binary** (not the fake
  server): connects, receives real 10 Hz heartbeats, the default
  playing/rate-1 state comes through correctly, and `REPLAY` is correctly
  absent when nothing is loaded - the game side's own log confirms it was the
  one listening.
- 6 more against the real binary for the auto-follow, which is the one thing
  a fake server can't honestly prove (it was written to match the intended
  behaviour, so testing against it alone would be circular). Driven via
  `--benchmark -N`, which reaches `startWatchingReplay()` for
  `benchmark_black_forest.replay` - a genuine watch-replay `World::reset()`,
  the same path the in-game menu takes. Connecting 1.4 s after launch gives
  a greeting with no `REPLAY` and `DURATION 0.000`, and 1.2 s later a
  post-greeting `REPLAY ./data/replay/benchmark_black_forest.replay` with
  `DURATION 37.138` - a real length, computed from the real ghost.

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

**Verified** the whole chain together, driven through the real `App` exactly
as above: `default_patched_stk_binary()` finds a fresh `build.sh` build with
no settings.json entry needed, clicking Launch starts the real binary with
the right argv and working directory (caught a real bug here - the working
directory was being computed one level short, landing in `build/` instead of
the checkout root, before this test existed), and the client connects on its
own within a few seconds of the click, with nothing else touched.

## Applying

```bash
./patches/build.sh
```

Clones `stk-code` at tag `1.5`, applies all four patches, symlinks assets
from an existing SuperTuxKart install if it finds one (see below), and builds
with cmake + ninja. Safe to re-run - an existing checkout is reused rather
than re-cloned, and the build itself is incremental. Defaults to
`~/.local/share/stk-minimap/stk-code` (`--dir` to use somewhere else,
`--jobs` to control parallelism); this is also where `stk_minimap.py`'s GUI
looks for the result on its own, so for most people this one command plus the
GUI's **Launch SuperTuxKart** button (Replay tab) is the entire setup.

Verified: a clean run against a fresh clone builds successfully end to end,
a second run against the same checkout is a 0.8s no-op (`ninja: no work to
do`), and the resulting binary genuinely listens - confirmed both from the
shell and by connecting `stk_minimap.py`'s real sync client to it.

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
