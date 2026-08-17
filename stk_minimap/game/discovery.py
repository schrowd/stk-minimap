from __future__ import annotations

import os
import sys


def stk_minimap_data_dir() -> str:
    """
    Where patches/build.sh drops a patched checkout, and where
    default_patched_stk_binary() looks for the result - same per-platform
    base directory build.sh uses, so building is all that's needed for the
    GUI's Launch button to find the binary on its own.
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "stk-minimap")


def default_patched_stk_binary() -> str | None:
    """The binary patches/build.sh produces at its default --dir, if it's
    there."""
    name = "supertuxkart.exe" if os.name == "nt" else "supertuxkart"
    cand = os.path.join(stk_minimap_data_dir(), "stk-code", "build", "bin", name)
    return cand if os.path.isfile(cand) else None
