#!/usr/bin/env python3
"""
Closes the loop the fake server can't: the real patched binary as the server,
the real SyncClient as the client, and a REPLAY announcement produced by the
actual C++ code path rather than by a stand-in.

STK's replay menu can't be driven headlessly, so this uses --benchmark, which
calls Profiler::startBenchmark() -> setWatchingReplay(true) ->
startWatchingReplay() for benchmark_black_forest.replay.  That is a genuine
watch-replay World::reset(), so it exercises exactly the path a user hitting
"Watch" in the menu does: duration computed from the ghosts, name set, and
the server noticing the change and broadcasting it.

Needs a real patched build at the default patches/build.sh location, with the
shipped benchmark replay's assets in place (build.sh's normal output);
skips if none is found. Opens a real STK window for roughly a minute.
"""
import subprocess
import sys

import _paths
from stk_viewer.game.discovery import default_patched_stk_binary

PORT = 27993


def main():
    binary = default_patched_stk_binary()
    if not binary:
        print("skipped: no patched build at the default location - "
              "run patches/build.sh first")
        return 0
    root = _paths.stk_binary_root(binary)

    c = _paths.Checker()
    app = _paths.build_app()
    events = []
    orig_post = app.sync_post
    def spy_post(kind, payload):
        events.append((kind, payload))
        orig_post(kind, payload)
    app.sync_post = spy_post

    log_path = _paths.scratch_dir("stk-viewer-autoload-") / "stk_autoload.log"
    log = open(log_path, "w")
    stk = subprocess.Popen(
        # -N is required: startBenchmark() is only reached when
        # m_no_start_screen is set, so --benchmark on its own just sets a flag
        # and leaves the game sitting in the main menu.
        ["./build/bin/supertuxkart", f"--sync-port={PORT}", "--benchmark",
         "-N"],
        cwd=str(root), stdout=log, stderr=subprocess.STDOUT)
    try:
        app.sync_port.set(str(PORT))
        app.sync_toggle()

        print("\n1. connect to the real binary while it is still in the menus")
        c.check(_paths.wait_for(app, lambda: app.sync_connected, timeout=40),
                "connected to the real patched game")
        greeting_replays = [p for k, p in events if k == "sync-replay"]
        c.check(not greeting_replays,
                f"no REPLAY at connect - nothing loaded yet {greeting_replays}")

        print("\n2. the game starts watching a replay -> REPLAY is broadcast")
        got = _paths.wait_for(
            app, lambda: any(k == "sync-replay" for k, _ in events),
            timeout=90)
        names = [p for k, p in events if k == "sync-replay"]
        c.check(got,
                f"the real server announced a replay mid-session: {names}")
        if names:
            c.check("black_forest" in names[0],
                    f"and it's the one the game loaded: {names[0]}")
        durs = [p for k, p in events if k == "sync-duration"]
        c.check(any(d > 1.0 for d in durs),
                f"a real non-zero DURATION came with it: {durs[:3]}")
    finally:
        app.sync_toggle()
        stk.terminate()
        try:
            stk.wait(timeout=10)
        except subprocess.TimeoutExpired:
            stk.kill(); stk.wait()
        log.close()

    out = open(log_path).read()
    c.check("ReplaySync: Listening" in out,
            "STK's log confirms it listened")
    app.root.destroy()
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
