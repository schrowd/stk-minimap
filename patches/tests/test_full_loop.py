#!/usr/bin/env python3
"""
The capstone: a fresh App with NO prior settings, against the real
build.sh-built binary at its real default location.  Confirms the whole
convenience loop: auto-discovery finds the binary, one click launches it with
the right flag, and sync connects on its own - without the user ever typing a
path or a --sync-port flag by hand.

Needs a real patched build at the default patches/build.sh location; skips
if none is found.
"""
import os
import subprocess
import sys
import time

import _paths
from stk_minimap.game.discovery import default_patched_stk_binary


def main():
    expected = default_patched_stk_binary()
    if not expected:
        print("skipped: no patched build at the default location - "
              "run patches/build.sh first")
        return 0

    c = _paths.Checker()
    c.check(True, f"default_patched_stk_binary() finds the build.sh "
                  f"output: {expected}")

    app = _paths.build_app()
    c.check(app.stk_binary.get() == expected,
            "the App auto-populated the field with it, no settings needed")
    c.check("not found" not in app.stk_binary_lbl.cget("text"),
            "label shows the real path, not the 'not found' hint")

    print("\nclicking Launch (no port typed beyond the default, no path "
          "browsed)...")
    if app.sync_client:
        app.sync_toggle()  # ensure clean start
    app.sync_launch_stk()

    _paths.wait_for(app, lambda: app.sync_connected, timeout=20, step=0.1)

    c.check(app.sync_connected,
            "connected to the real launched game with zero manual setup")
    c.check("connected" in app.sync_status_lbl.cget("text"),
            "status label: " + app.sync_status_lbl.cget("text"))

    # tidy up: disconnect and kill the game we just launched
    app.sync_toggle()
    subprocess.run(["pkill", "-x", "-u", str(os.getuid()), "supertuxkart"])
    app.root.destroy()

    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
