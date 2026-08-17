#!/usr/bin/env python3
"""
Drives the real stk_minimap App (real Tk widgets, real event loop) against a
fake STK sync server, and checks the App's actual state - not a mock of it.

Every call below is either the exact function Tk would invoke for that user
action (self.rp_toggle for the play button, self.rp_scrub for dragging the
scale, the trace on self.rp_rate for the combobox, self.sync_toggle for the
Connect button) or a real StringVar write.  root.update() pumps the real Tk
event loop, which is what actually drains self.q via the App's own
self.root.after(80, self._poll) chain - nothing here bypasses that.

Needs at least one locally-recorded replay on Hacienda to auto-load against;
skips if none is found (see patches/README.md's "Running the tests").
"""
import os
import subprocess
import sys
import time

import _paths
from stk_minimap.replay.parser import load_replay

PORT = 27995


def start_fake_stk(port, name, duration, extra_args=()):
    p = subprocess.Popen([sys.executable, str(_paths.FAKE_STK),
                          str(port), name, str(duration), *extra_args])
    time.sleep(0.3)
    return p


def main():
    replay = _paths.find_local_replays(n=1)
    if not replay:
        print("skipped: no locally-recorded Hacienda replay to test against")
        return 0
    basename = os.path.basename(replay[0])
    duration = load_replay(replay[0]).duration

    c = _paths.Checker()
    app = _paths.build_app()
    root = app.root

    print("\n1. connect before anything is loaded")
    fake = start_fake_stk(PORT, basename, duration)
    app.sync_port.set(str(PORT))
    app.sync_toggle()  # Connect button
    _paths.pump(app, 0.5)
    c.check(app.sync_connected, "sync_connected becomes True")
    c.check("connected" in app.sync_status_lbl.cget("text") or
            basename in app.sync_status_lbl.cget("text"),
            "status label reflects connection: " +
            app.sync_status_lbl.cget("text"))

    print("\n2. REPLAY message auto-loads the matching local file")
    _paths.pump(app, 0.3)
    c.check(app.replay is not None and
            os.path.basename(app.replay.path) == basename,
            "matching replay auto-loaded: " +
            (os.path.basename(app.replay.path) if app.replay else "none"))
    c.check(str(app.rp_play_btn.cget("state")) == "normal",
            "playback controls enabled")

    print("\n3. STATE flows in and moves the local head")
    _paths.pump(app, 0.6)
    c.check(app.rp_playing, "rp_playing follows the fake server's playing=True")
    c.check(app.rp_t > 0.2, f"rp_t is advancing (currently {app.rp_t:.2f})")

    print("\n4. local PAUSE (button) sends PAUSE and wins over stale STATE")
    app.rp_toggle()   # this is exactly what the pause button's command does
    c.check(not app.rp_playing, "rp_playing is False locally right away")
    _paths.pump(app, 0.4)
    c.check(not app.rp_playing,
            "still paused after 0.4s (remote didn't undo it in the holdoff "
            "window, and then genuinely paused itself)")

    print("\n5. local SEEK (scrub) sends SEEK")
    app.rp_pos.set(0.5)
    app.rp_scrub("0.5")   # this is exactly what dragging the scale does
    c.check(abs(app.rp_t - 0.5 * app.rp_duration()) < 0.05,
            f"rp_t jumped to the scrub position ({app.rp_t:.2f})")
    _paths.pump(app, 0.4)
    c.check(abs(app.rp_t - 0.5 * app.rp_duration()) < 0.3,
            f"holds near the scrub position after 0.4s ({app.rp_t:.2f}), "
            "not snapped back by a stale STATE")

    print("\n6. resume and let the fake server's real-time clock catch up")
    app.rp_toggle()
    _paths.pump(app, 1.0)
    c.check(app.rp_t > 0.5 * app.rp_duration() + 0.3,
            f"time keeps advancing after resume ({app.rp_t:.2f})")

    print("\n7. local RATE change sends RATE, fake server's clock speeds up")
    t0 = app.rp_t
    app.rp_rate.set("4x")     # this is exactly what picking "4x" does
    _paths.pump(app, 1.0)
    advanced = app.rp_t - t0
    c.check(advanced > 2.5,
            f"~4x real time in ~1s of wall clock (advanced {advanced:.2f}s)")

    print("\n8. reconnect after the fake server restarts")
    app.rp_toggle()  # pause, so the state is stable while the server is down
    _paths.pump(app, 0.3)
    fake.terminate()
    fake.wait(timeout=3)
    _paths.pump(app, 0.5)
    c.check(not app.sync_connected,
            "sync_connected goes False while STK is down")
    c.check("retrying" in app.sync_status_lbl.cget("text").lower() or
            "not reachable" in app.sync_status_lbl.cget("text").lower(),
            "status says it's retrying: " + app.sync_status_lbl.cget("text"))

    fake2 = start_fake_stk(PORT, basename, duration)
    _paths.pump(app, 3.0)
    c.check(app.sync_connected, "reconnects on its own once STK is back")

    print("\n9. Disconnect button actually stops the client")
    app.sync_toggle()
    _paths.pump(app, 0.3)
    c.check(app.sync_client is None, "sync_client is torn down")
    c.check(app.sync_btn.cget("text") == "Connect",
            "button reverts to Connect")
    fake2.terminate()
    fake2.wait(timeout=3)

    print("\n10. connecting when STK is watching a replay we don't have")
    fake3 = start_fake_stk(PORT, "no_such_replay_anywhere.replay", 50.0)
    app.replay = None
    app.sync_toggle()
    _paths.pump(app, 0.6)
    c.check(app.replay is None,
            "no crash, and it correctly didn't invent a replay to show")
    c.check("no_such_replay" in app.status.cget("text"),
            "status bar explains what STK is watching: " +
            app.status.cget("text"))
    app.sync_toggle()
    fake3.terminate(); fake3.wait(timeout=3)

    print("\n11. never sending a REPLAY line at all (STK idle in the menu)")
    fake4 = start_fake_stk(PORT, "", duration, extra_args=["--no-replay-line"])
    app.sync_toggle()
    _paths.pump(app, 0.5)
    c.check(app.sync_connected, "still connects fine with nothing to watch")
    app.sync_toggle()
    fake4.terminate(); fake4.wait(timeout=3)

    root.destroy()
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
