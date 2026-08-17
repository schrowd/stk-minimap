#!/usr/bin/env python3
"""
Interactive first-time setup for stk-minimap on Linux.

Run this once after cloning or downloading the repo:

    python3 install.py

Checks the Python dependencies, offers to install the missing ones with pip,
and optionally adds a "STK Minimap" entry to your applications menu / dock -
without needing to know about `python3 -m stk_minimap` or which directory it
has to be run from.

Windows and macOS aren't covered here yet:
  Windows - double-click "STK Minimap.pyw".
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
    if not ask("Add 'STK Minimap' to your applications list / dock?"):
        print("  skipped - launch any time with: "
              "python3 -m stk_minimap --gui")
        return
    sys.path.insert(0, str(REPO_ROOT))
    from stk_minimap.cli import install_desktop_entry
    install_desktop_entry()


def main() -> int:
    print(BANNER)

    if os.name != "posix" or sys.platform == "darwin":
        print("This setup script covers Linux only for now.")
        print("Windows: double-click 'STK Minimap.pyw'.")
        print("macOS:   see the Quick start section in README.md.")
        return 1

    if sys.version_info < (3, 9):
        print(f"Python {sys.version_info.major}.{sys.version_info.minor} "
              f"found; stk-minimap needs 3.9+.")
        return 1

    check_dependencies()
    offer_desktop_entry()

    print()
    print("Set up. From this folder:")
    print("  python3 -m stk_minimap --gui       # open the window")
    print("  python3 -m stk_minimap --list      # what tracks does it find?")
    print("  python3 -m stk_minimap hacienda    # render one to a PNG")
    return 0


if __name__ == "__main__":
    sys.exit(main())
