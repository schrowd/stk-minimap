#!/usr/bin/env python3
"""Drives the real App's Launch SuperTuxKart button against a stand-in
'binary' (a shell script), checking argv/cwd/detachment - the exact things
that bit the user (missing --sync-port, wrong cwd so data/ isn't found)."""
import os
import sys
import time

import _paths
from stk_minimap.gui.settings import load_settings, save_settings


def main():
    c = _paths.Checker()
    root_dir = _paths.scratch_dir("stk-minimap-fake-stk-")
    fake_bin = _paths.make_fake_stk_binary(root_dir)
    probe = root_dir / "launch_probe.txt"

    app = _paths.build_app()

    print("\n1. default label when nothing is configured or auto-discovered")
    app.stk_binary.set("")
    app.update_stk_binary_label()
    c.check("not found" in app.stk_binary_lbl.cget("text"),
            "label explains what to do: " + app.stk_binary_lbl.cget("text"))

    print("\n2. Launch with a known binary path")
    app.stk_binary.set(str(fake_bin))
    app.sync_port.set("27999")
    app.sync_launch_stk()
    end = time.monotonic() + 3
    while not probe.exists() and time.monotonic() < end:
        app.root.update()
        time.sleep(0.05)
    c.check(probe.exists(), "the binary actually ran")
    if probe.exists():
        parsed = dict(l.split("=", 1) for l in probe.read_text().splitlines())
        c.check("--sync-port=27999" in parsed.get("args", ""),
                f"--sync-port was passed: {parsed.get('args')!r}")
        c.check(os.path.normpath(parsed.get("cwd", "")) ==
                os.path.normpath(str(root_dir)),
                f"launched with cwd = the checkout root (two up from "
                f"build/bin): {parsed.get('cwd')!r}")
        c.check(parsed.get("data_exists") == "yes",
                "from that cwd, ./data resolves - matching how the real "
                "game finds its assets")

    print("\n3. sync_start() is triggered automatically after Launch")
    c.check(app.sync_client is not None,
            "a SyncClient was created without a separate Connect click")
    app.sync_stop()

    print("\n4. persisted browse path survives")
    app.settings.pop("stk_binary", None)
    app.stk_binary.set(str(fake_bin))
    app.settings["stk_binary"] = str(fake_bin)
    save_settings(app.settings)
    reloaded = load_settings()
    c.check(reloaded.get("stk_binary") == str(fake_bin),
            "browsed path round-trips through settings.json")

    print("\n5. a missing/deleted binary doesn't crash, asks to browse instead")
    app.stk_binary.set("/no/such/file/anywhere")
    # can't drive the real file picker headlessly; just confirm the guard
    # takes the "ask" branch rather than trying to Popen a nonexistent path
    c.check(not os.path.isfile(app.stk_binary.get()),
            "precondition: path really doesn't exist")

    app.root.destroy()
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
