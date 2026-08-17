#!/usr/bin/env python3
"""
Real interop: the actual patched supertuxkart binary as the server, the real
App/SyncClient as the client.  STK isn't watching a replay in this run (no
GUI automation for Irrlicht's menus), so this only reaches the "connected,
nothing loaded yet" state - but every byte on the wire is produced by the
real C++ ReplaySyncServer, not a stand-in for it.

Needs a real patched build at the default patches/build.sh location; skips
if none is found.
"""
import subprocess
import sys
import time

import _paths
from stk_minimap.game.discovery import default_patched_stk_binary

PORT = 27997


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

    # stdout=PIPE without draining it deadlocks STK once its startup log
    # fills the 64KB pipe buffer, well before it gets anywhere near
    # --sync-port - a log *file* doesn't have that problem.
    log_path = _paths.scratch_dir("stk-minimap-e2e-") / "stk_real_e2e.log"
    log = open(log_path, "w")
    stk = subprocess.Popen(
        [str(binary), "--no-graphics", f"--sync-port={PORT}"],
        cwd=str(root), stdout=log, stderr=subprocess.STDOUT)
    time.sleep(8)

    app.sync_port.set(str(PORT))
    app.sync_toggle()
    _paths.pump(app, 5.0, step=0.02)

    c.check(app.sync_connected, "connects to the real STK binary")
    kinds = [k for k, _ in events]
    c.check("sync-status" in kinds, "got status events")
    c.check(kinds.count("sync-state") >= 3,
            f"received real STATE heartbeats ({kinds.count('sync-state')})")
    states = [p for k, p in events if k == "sync-state"]
    c.check(all(pl[1] is True and abs(pl[2] - 1.0) < 1e-6 for pl in states),
            "STK's own default state (playing, rate 1) came through intact")
    c.check("sync-replay" not in kinds,
            "no REPLAY line sent - correctly omitted, nothing is loaded "
            "(matches patches/PROTOCOL.md 'as implemented')")

    app.sync_toggle()
    app.root.update()

    stk.terminate()
    try:
        stk.wait(timeout=5)
    except subprocess.TimeoutExpired:
        stk.kill()
        stk.wait()
    log.close()
    out = open(log_path).read()
    c.check("ReplaySync: Listening" in out,
            "STK's own log confirms it listened")

    app.root.destroy()
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
