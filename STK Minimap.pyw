#!/usr/bin/env python3
"""
Windows convenience launcher.  Keep this file at the repo root, next to the
stk_minimap package folder.

Double-clicking a .pyw on Windows runs it with pythonw.exe, which has no
console attached, so the GUI opens on its own.

The extension means nothing on Linux or macOS - there is no console-less
python there, and you will still get the terminal you launched it from.  Use
`python3 -m stk_minimap --gui` instead, or a .desktop entry.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stk_minimap.cli import _launch_gui        # noqa: E402

raise SystemExit(_launch_gui([]))
