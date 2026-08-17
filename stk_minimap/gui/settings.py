from __future__ import annotations

import json
import os
import sys

# the two left-pane filters, kept here so saved settings can be validated
# against them - a hand-edited settings file should not be able to leave the
# track list permanently empty
FILTER_KINDS = ("All types", "Race", "Arena", "Soccer")
FILTER_SOURCES = ("All sources", "Built-in", "Add-ons")


def settings_path() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "stk-minimap", "settings.json")


def load_settings() -> dict:
    try:
        with open(settings_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    """Best effort - a read-only home is no reason to fail a render."""
    path = settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
    except OSError:
        pass
