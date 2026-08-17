#!/usr/bin/env python3
"""
The scenario: stk-viewer has nothing loaded and is already connected, then a
replay is loaded in-game.  The map should pick it up and follow it by itself.

Also covers switching to a *different* replay mid-session, a replay the user
doesn't have locally, and a replay already part-way through when announced.

Needs two locally-recorded Hacienda replays to switch between; skips if fewer
than two are found (see patches/README.md's "Running the tests").
"""
import os
import subprocess
import sys
import time

import _paths
from stk_viewer.replay.parser import load_replay

PORT = 27994


def loaded(app):
    return os.path.basename(app.replay.path) if app.replay else None


def main():
    replays = _paths.find_local_replays(n=2)
    if len(replays) < 2:
        print("skipped: need two locally-recorded Hacienda replays "
              f"(found {len(replays)})")
        return 0
    path_a, path_b = replays
    a, b = os.path.basename(path_a), os.path.basename(path_b)
    dur_a = load_replay(path_a).duration
    dur_b = load_replay(path_b).duration

    c = _paths.Checker()

    # Start with the game idle in the menus: no replay loaded, so no REPLAY
    # line at connect time - exactly the state after Launch.
    fake = subprocess.Popen(
        [sys.executable, str(_paths.FAKE_STK), str(PORT), "", "0",
         "--no-replay-line"],
        stdin=subprocess.PIPE, text=True, bufsize=1)
    time.sleep(0.4)

    app = _paths.build_app()
    app.sync_port.set(str(PORT))

    print("\n1. connected with nothing loaded on either side")
    app.sync_toggle()
    c.check(_paths.wait_for(app, lambda: app.sync_connected), "connected")
    c.check(app.replay is None, "stk-viewer has no replay loaded")

    print("\n2. a replay is loaded in-game -> the map follows by itself")
    fake.stdin.write(f"LOAD {a} {dur_a}\n")
    got = _paths.wait_for(app, lambda: loaded(app) == a)
    c.check(got, f"auto-loaded {a} (loaded: {loaded(app)})")
    c.check("following" in app.status.cget("text"),
            "status says it's following: " + app.status.cget("text"))
    c.check(a in app.sync_status_lbl.cget("text"),
            "sync label names the replay: " + app.sync_status_lbl.cget("text"))
    c.check(str(app.rp_play_btn.cget("state")) == "normal",
            "playback controls came alive")

    print("\n3. and then actually tracks the game's clock")
    _paths.pump(app, 0.8)
    c.check(app.rp_t > 0.2, f"rp_t is following the game ({app.rp_t:.2f})")
    c.check(app.rp_playing, "and follows its playing state")

    print("\n4. switching to a different replay in-game")
    fake.stdin.write(f"LOAD {b} {dur_b}\n")
    c.check(_paths.wait_for(app, lambda: loaded(app) == b),
            f"switched to {b} (loaded: {loaded(app)})")
    c.check(abs(app.rp_duration() - dur_b) < 0.5,
            f"duration came from the newly loaded file "
            f"({app.rp_duration():.2f} vs {dur_b:.2f})")

    print("\n5. a replay announced part-way through is picked up at that point")
    fake.stdin.write(f"LOAD {a} {dur_a} 40.0\n")
    c.check(_paths.wait_for(app, lambda: loaded(app) == a), "loaded it again")
    # use_replay() starts at 0; the handoff has to move it to the game's
    # position rather than leaving the marker at the start line.
    c.check(_paths.wait_for(app, lambda: app.rp_t > 35.0, timeout=2.0),
            f"jumped to the game's position, not 0 (rp_t={app.rp_t:.2f})")

    print("\n6. a replay we don't have locally: explained, not crashed")
    before = loaded(app)
    fake.stdin.write("LOAD definitely_not_here_12345.replay 60.0\n")
    c.check(_paths.wait_for(app,
            lambda: "definitely_not_here" in app.status.cget("text")),
            "status explains it: " + app.status.cget("text"))
    c.check(loaded(app) == before,
            "kept the previous replay rather than clearing the view")

    app.sync_toggle()
    fake.stdin.close()
    fake.terminate()
    try:
        fake.wait(timeout=3)
    except subprocess.TimeoutExpired:
        fake.kill()
    app.root.destroy()
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
