#!/usr/bin/env python3
"""
Windows convenience launcher.

Double-clicking a .pyw runs it with pythonw.exe, so the GUI opens without a
console window behind it.  Keep this file next to stk_minimap.py.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stk_minimap import run_gui        # noqa: E402

raise SystemExit(run_gui([]))
