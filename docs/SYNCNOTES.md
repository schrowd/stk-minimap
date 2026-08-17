# Sync implementation notes

Design rationale and verification behind the SuperTuxKart patches in
[`../patches/`](../patches/) and the viewer's sync client in
`stk_minimap/sync/client.py` and `stk_minimap/gui/replay_tab.py`.
Companion to [`NOTES.md`](NOTES.md) - same idea, different half of the
project: kept because most of it can't be reconstructed from the code alone,
and it isn't worth re-deriving next time this gets touched.

This is *not* the wire contract - that's
[`../patches/PROTOCOL.md`](../patches/PROTOCOL.md), written to be read on its
own by anyone implementing either side. [`../patches/README.md`](../patches/README.md)
has the short version of everything below: what each patch does, how to build
and run the tests. Read this file when you need the *why*, or the verification
behind a *"this is correct"* claim.

## 0001 - ghost controller seekable

`GhostController::update()` only ever advanced its index **forward**:

```cpp
while (m_current_index + 1 < m_all_times.size() &&
       m_current_time >= m_all_times[m_current_index + 1])
    m_current_index++;
```

Move the clock backwards and the index is stuck, so the ghost freezes. The
patch adds a matching backward walk, deliberately written so the **forward
path is untouched** - during normal playback the new loop provably never
executes, because after the forward search `m_all_times[index]` is always
`<= m_current_time` and the next frame's time is larger still.

It also fixes a second latch in `GhostKart::update()`: reaching the end of a
replay calls `m_node->setVisible(false)`, and in watch-replay mode nothing
ever set it back - so rewinding from the finish left an invisible kart.

**Verified** with `ghost_index_test.cpp`, which lifts both the old and new
index logic out verbatim and drives them against real replay timings, checked
against an independently-computed reference:

| | old | new |
|---|---|---|
| ordinary forward playback | correct | correct - provably unchanged |
| 20,000 random seeks | **18,181 wrong** | 0 wrong |
| rewind from the end | **stuck at the last index** | correct |

Run across all 113 replays available locally (57 recorded plus the 51 that
ship with 1.5): the patched logic matches the reference in every case, on
every file.

## 0002 - replay playback control

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

## 0003 - fix replay trailing blank line crash

`ReplayPlay::loadFile()` skips a computed number of header lines, then loops
`fgets` -> `readKartData()` until EOF. `wr_candela_city_202598_1_82_3725.replay`
ends with a **trailing blank line**, so the loop runs one extra iteration.
The arithmetic is exact:

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
`vector::_M_range_check` error exactly.

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

## 0004 - replay sync server

Three things shaped the design.

**It never touches the main loop.** The server owns a thread that does
nothing but `poll()`, and applies commands straight to `ReplayControl`, which
was already mutex-protected. No queue, no callback into the game, no lock the
main loop can wait on - so a viewer that hangs, floods or vanishes cannot
affect the frame rate. A command still takes effect on the very next tick.
Measured with a viewer connected and deliberately never reading, over 2
seconds: the fake main loop ran **237 ticks** (unimpeded) and the longest
`advance()` call was **2 µs**.

**It binds `127.0.0.1`, never `INADDR_ANY`.** This opens a control channel
into a running game; it has no business being reachable off the machine.

**Nothing fails hard.** A busy port, a bad port number, a client that
disconnects mid-write - all log and carry on. Unknown verbs are ignored
rather than rejected, so a newer viewer can talk to an older game.

`World::reset()` also learns the replay's length, from the longest ghost's
last *recorded* time rather than its finish time: the recording carries on
past the finish line and that tail should still be scrubbable.

**A replay loaded after the viewer connected is broadcast, not just reported
at connect time.** This is what makes "start the game, pick a replay, and the
map follows on its own" work, and it is the normal order of events when the
game is launched from the viewer. The server compares the loaded name each
cycle rather than relying on a flag, which also covers swapping one replay
for another; re-loading the *same* one is deliberately not re-announced,
since `World::reset()` runs for every new world and a viewer shouldn't reload
the file it is already showing.

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

## The viewer side

`stk_minimap/sync/client.py`'s `SyncClient`, and the `sync_*` bridge methods
on `stk_minimap/gui/replay_tab.py`'s `ReplayTabMixin`. Structurally, the two
sync directions never cross: local input handlers
(`rp_toggle`, `rp_scrub`, `on_key_step`, `on_key_end`, the rate combobox's
trace) call `sync_send()`, which both transmits and starts a 250ms local-wins
holdoff. Applying an inbound `STATE` (`sync_on_state()`) only ever writes
`rp_t`/`rp_playing`/the rate display directly - it never calls a handler that
would send something back. That split is what makes
[`PROTOCOL.md`](../patches/PROTOCOL.md)'s loop-safety rules hold
structurally rather than by care at each call site.

When a `REPLAY <path>` arrives - at connect, or later because a replay was
just picked in-game - the viewer looks for a same-named file in the usual
replay folders and loads it automatically, so the map follows a replay
loaded in the game with nothing open here beforehand. If nothing matches, it
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

## Getting a patched build running

The first time this got tried for real, it didn't work - the running game had
no `--sync-port` flag at all, because starting it is a separate manual step
easy to get wrong or forget. `patches/build.sh` and the GUI's "Launch
SuperTuxKart" button close that gap: the button starts the discovered binary
with `--sync-port=<the port already in the field next to it>` and the correct
working directory, then starts trying to connect on its own - the two things
that have to be right for sync to come up at all, taken out of the user's
hands.

**Verified** the whole chain together, driven through the real `App` exactly
as the sync-client checks above: `default_patched_stk_binary()` finds a fresh
`build.sh` build with no settings.json entry needed, clicking Launch starts
the real binary with the right argv and working directory (caught a real bug
here - the working directory was being computed one level short, landing in
`build/` instead of the checkout root, before this test existed), and the
client connects on its own within a few seconds of the click, with nothing
else touched.
