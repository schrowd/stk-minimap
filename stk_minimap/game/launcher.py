from __future__ import annotations

import os
import subprocess


def launch_stk(binary: str, port: int) -> subprocess.Popen:
    """
    Starts a patched SuperTuxKart binary with --sync-port=<port> already set,
    so the one thing that actually broke this the first time it was tried -
    launching the game *without* the flag - can't happen from here.

    The game finds its data/ directory by walking up from the working
    directory (FileManager::discoverPaths), so it has to be launched from the
    checkout root, exactly like running it by hand needs `cd` first.
    build.sh always builds to <root>/build/bin/<exe>, so the root is three
    levels up: past the binary itself, then "bin", then "build".
    """
    root_dir = os.path.dirname(os.path.dirname(os.path.dirname(binary)))
    detach = ({"creationflags": subprocess.DETACHED_PROCESS |
                                subprocess.CREATE_NEW_PROCESS_GROUP}
             if os.name == "nt" else {"start_new_session": True})
    # stdout/stderr must not be PIPE: STK logs enough at startup to fill the
    # pipe buffer and deadlock before it ever gets to opening the sync port,
    # with nothing here to drain it.
    return subprocess.Popen([binary, f"--sync-port={port}"], cwd=root_dir,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **detach)
