# stk-viewer ⇄ SuperTuxKart sync protocol

Version 1. **Both sides are implemented and verified**: the game side in
[`0004-replay-sync-server.patch`](README.md), the viewer side in
`stk_viewer/sync/client.py`'s `SyncClient` and the `sync_*` methods in
`stk_viewer/gui/replay_tab.py`, driven against a fake server
speaking this exact protocol, and separately against the real patched binary.
The "avoiding a feedback loop" rules below are tested behaviour, not just
design intent - see `SYNC_DEADBAND` and `SYNC_LOCAL_HOLDOFF` in
`stk_viewer/sync/`.

The goal is two-way sync: pause in either window and both pause; scrub in
either and both scrub. This document is the contract between the STK patch
(`patches/`) and the viewer, written down separately so the two can be
reviewed and changed independently.

## Transport

**TCP on `127.0.0.1`, default port `27982`, loopback only.**

Considered and rejected:

- **Unix domain sockets**: not portable to Windows in a form that Python's
  `socket` and a C++ patch can both rely on across the versions STK targets.
  Given this project's whole Windows story is already unverified, adding a
  transport that behaves differently there is the wrong trade.
- **Named pipes**: Windows-only, so it would mean two implementations.
- **A shared state file**: polling a file for a 50 Hz playback head means
  either latency or hammering the disk, and it can't carry commands cleanly.

Loopback TCP is the only option that is one implementation on all three
platforms. Binding to `127.0.0.1` rather than `0.0.0.0` matters: this opens a
control channel into a running game, and it must not be reachable off the
machine.

**STK listens, the viewer connects.** The game is the thing with a fixed
lifetime and a real clock; the viewer is the thing that may come and go. STK
must tolerate zero clients, a client disconnecting mid-run, and a client
reconnecting, without ever blocking its main loop on the socket.

## Framing

Line-delimited ASCII. One command per line, `\n`-terminated, fields separated
by single spaces. Case-sensitive, verbs uppercase.

No JSON. STK vendors no JSON parser and this protocol does not justify adding
a dependency to a patch intended for upstream review. `sscanf` on one side and
`str.split()` on the other is the entire implementation, and it stays
debuggable with `nc 127.0.0.1 27982` by hand.

Unknown verbs **must be ignored, not error**, so either side can add commands
later without a version negotiation dance.

## Commands

Viewer → STK:

| Line | Meaning |
|---|---|
| `PLAY` | resume playback |
| `PAUSE` | halt playback, keep rendering |
| `SEEK <seconds>` | jump the playback head; float, clamped to the replay |
| `RATE <multiplier>` | playback speed; `1.0` normal, `0.25` quarter speed |
| `PING` | request an immediate `STATE` |

STK → viewer:

| Line | Meaning |
|---|---|
| `HELLO stk <version> <protocol>` | sent on connect, e.g. `HELLO stk 1.5 1` |
| `REPLAY <path>` | which file is loaded; sent on connect and on change |
| `DURATION <seconds>` | length of the loaded replay |
| `STATE <time> <playing> <rate>` | playback head; `playing` is `0` or `1` |
| `BYE` | STK is shutting down |

`STATE` is the only high-frequency message. It is sent on every change of
`playing`/`rate`, immediately after a `SEEK`, and otherwise at **10 Hz** - not
per frame. The viewer interpolates between updates using its own clock, which
it already does for replay frames anyway (recorded replays are ~15 Hz, so the
viewer's marker interpolation is already solving this exact problem).

## Avoiding a feedback loop

The hard part of two-way sync. Naively, STK's `STATE` makes the viewer seek,
which makes the viewer send `SEEK`, which makes STK seek, forever.

Rules:

1. **Applying a remote message never emits a local one.** A `STATE` that
   causes the viewer to move its head does not produce a `SEEK`. A `SEEK` that
   moves STK's clock does not produce an immediate contradicting `STATE` - it
   produces a confirming one, which is a no-op for the viewer by rule 2.
2. **Ignore state you already agree with.** A `STATE` whose time is within
   **one replay frame** (~0.1 s, the real recorded interval) of the local head
   is dropped. Without a deadband the two sides fight over float noise
   forever.
3. **A local user action wins for 250 ms.** Dragging the viewer's scrub bar
   suppresses inbound `STATE` for a moment, or every drag frame fights the
   game's 10 Hz updates. Whoever touched a control last is authoritative.

Rule 2's deadband is deliberately the *recorded frame interval*, not an
arbitrary epsilon: tighter than that is below the resolution the data actually
has, so it would be chasing noise.

## Opting in

The listener is **off unless STK is started with `--sync-port=<n>`**. No flag,
no socket, no thread, no behaviour change whatsoever. This is a patch intended
to be reviewable - and ideally upstreamable - so the default build must be
byte-for-byte the game people already run.

`--sync-port` implies `--replay-control`; a viewer that cannot move the
playback head would not be much use.

## As implemented

Details the spec above leaves open, pinned down by `0004`:

- **Up to 4 viewers.** Further connections are accepted and immediately
  closed; existing ones are unaffected.
- **`STATE` goes to every connected viewer**, including the one whose command
  caused it. `PING` is answered only to the viewer that asked.
- **`REPLAY` is omitted when no replay is loaded**: for instance if a viewer
  connects while the game is sitting in the menus. It is sent on connect if
  something is loaded, and broadcast to everyone already connected whenever
  the loaded replay *changes* - which is what lets the viewer follow a replay
  picked in-game after it connected. `DURATION` always accompanies it.
- **Loading the same replay again is not re-announced.** `World::reset()` runs
  for every new world, including restarting the same replay, and a viewer
  should not reload the file it is already showing.
- **Leaving a replay clears the name**, so an ordinary race afterwards doesn't
  leave the viewer thinking a replay is still loaded. No `REPLAY` is sent for
  that transition; the viewer simply keeps showing the last one, which is
  still a perfectly good map.
- **`DURATION` is the longest ghost's last recorded time**, which is past the
  finish line; that tail is still scrubbable.
- **Trailing `\r` is stripped**, so a viewer that writes CRLF works.
- **A half-written line blocks later commands on that connection** until it is
  terminated, as a line protocol should. A connection that sends more than
  8 kB without a newline has its backlog dropped.
- **Nothing fails hard.** A port that is busy or out of range logs an error
  and the game carries on without sync.

## Scope

Watch-replay mode only (`RaceManager::isWatchingReplay()`). In that mode every
kart is a `GhostKart` - verified in `RaceManager::startWatchingReplay`, which
sets `m_num_karts = getNumGhostKart()` and marks every one `KT_GHOST` - so
there is no physics being integrated and no player input to reconcile. Seeking
is just moving a clock that positions are a pure function of.

Applying any of this to a live race would mean rewinding actual physics, which
is a categorically harder problem and explicitly out of scope.
