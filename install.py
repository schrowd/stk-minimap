#!/usr/bin/env python3
"""
Interactive first-time setup for stk-viewer on Linux.

Run this once after cloning or downloading the repo:

    python3 install.py

Checks the Python dependencies, offers to install the missing ones with pip,
optionally adds a "STK Viewer" entry to your applications menu / dock -
without needing to know about `python3 -m stk_viewer` or which directory it
has to be run from - and optionally clones and builds the patched
SuperTuxKart used for live replay sync (patches/build.sh).

Windows and macOS aren't covered here yet:
  Windows - double-click "STK Viewer.pyw".
  macOS   - see the Quick start section in README.md.
A PowerShell/.bat equivalent for Windows is planned.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

BANNER = '         __  __              _                       \n   _____/ /_/ /__     _   __(_)__ _      _____  _____\n  / ___/ __/ //_/____| | / / / _ \\ | /| / / _ \\/ ___/\n (__  ) /_/ ,< /_____/ |/ / /  __/ |/ |/ /  __/ /    \n/____/\\__/_/|_|      |___/_/\\___/|__/|__/\\___/_/     \n                                                     \n'


def have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def ask(question: str, default_yes: bool = True) -> bool:
    suffix = "[Y/n] " if default_yes else "[y/N] "
    try:
        reply = input(f"{question} {suffix}").strip().lower()
    except EOFError:
        return default_yes
    if not reply:
        return default_yes
    return reply.startswith("y")


def pip_install(*packages: str) -> bool:
    cmd = [sys.executable, "-m", "pip", "install", "--user", *packages]
    print("  $ " + " ".join(cmd))
    return subprocess.call(cmd) == 0


# same three package-manager guesses as README's Quick start section
_TK_HINTS = (
    ("pacman", "sudo pacman -S tk"),
    ("apt", "sudo apt install python3-tk python3-pil.imagetk"),
    ("dnf", "sudo dnf install python3-tkinter"),
)


def tk_install_hint() -> str:
    for mgr, cmd in _TK_HINTS:
        if shutil.which(mgr):
            return cmd
    return "install tkinter through your distro's package manager"


def check_dependencies() -> None:
    print("Checking dependencies...")
    missing = [pkg for pkg, mod in (("pillow", "PIL"), ("numpy", "numpy"))
               if not have(mod)]
    if missing:
        print(f"  missing: {', '.join(missing)}")
        if ask(f"  Install {' and '.join(missing)} now with pip --user?"):
            if not pip_install(*missing):
                print("  pip install failed - install them by hand and "
                      "re-run this script.")
        else:
            print("  skipped - rendering and the GUI won't work until "
                  "these are installed.")
    else:
        print("  Pillow : found")
        print("  numpy  : found (optional, for sharper downscaling)")

    if have("tkinter"):
        print("  tkinter: found")
    else:
        print("  tkinter: not found - needed for the GUI and the desktop "
              "entry below to actually open anything.")
        print(f"    {tk_install_hint()}")


def offer_desktop_entry() -> None:
    print()
    if not ask("Add 'STK Viewer' to your applications list / dock?"):
        print("  skipped - launch any time with: "
              "python3 -m stk_viewer --gui")
        return
    sys.path.insert(0, str(REPO_ROOT))
    from stk_viewer.cli import install_desktop_entry
    install_desktop_entry()


def offer_patches() -> None:
    """
    patches/build.sh already does the actual work - clones stk-code, applies
    the four patches, builds - and is safe to re-run.  This just decides
    whether to run it, since unlike everything else above it's slow (a git
    clone plus a C++ build, several minutes at best) and needs its own
    toolchain (git, cmake, ninja or make, a C++ compiler) that build.sh
    checks for itself.
    """
    print()
    print("Optional: build a patched SuperTuxKart for live two-way replay")
    print("sync (docs/SYNCNOTES.md) - lets you pause, scrub or slow a run")
    print("in either window and the other follows. Needs git, cmake, a C++")
    print("compiler, and a few minutes; nothing here is required to use")
    print("stk-viewer otherwise.")
    script = REPO_ROOT / "patches" / "build.sh"
    if not ask("Clone and build it now (patches/build.sh)?",
               default_yes=False):
        print(f"  skipped - run {script} yourself whenever you want it")
        return
    if not script.is_file():
        print(f"  {script} not found - skipping")
        return
    print(f"  $ bash {script}")
    if subprocess.call(["bash", str(script)]) != 0:
        print("  patches/build.sh exited with an error - see the output "
              "above; stk-viewer itself is unaffected.")


def main() -> int:
    print(BANNER)

    if os.name != "posix" or sys.platform == "darwin":
        print("This setup script covers Linux only for now.")
        print("Windows: double-click 'STK Viewer.pyw'.")
        print("macOS:   see the Quick start section in README.md.")
        return 1

    if sys.version_info < (3, 9):
        print(f"Python {sys.version_info.major}.{sys.version_info.minor} "
              f"found; stk-viewer needs 3.9+.")
        return 1

    check_dependencies()
    offer_desktop_entry()
    offer_patches()

    print()
    print("Set up. From this folder:")
    print("  python3 -m stk_viewer --gui       # open the window")
    print("  python3 -m stk_viewer --list      # what tracks does it find?")
    print("  python3 -m stk_viewer hacienda    # render one to a PNG")
    return 0


if __name__ == "__main__":
    sys.exit(main())
