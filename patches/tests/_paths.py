"""
Shared setup for the sync test suite: path resolution, config isolation, and
the App/import boilerplate every test in this directory needs.

Import this before anything else - before tkinter, before importing
stk_viewer - since config isolation has to be in place before App.__init__
calls load_settings().
"""
import os
import pathlib
import sys
import tempfile

TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO = TESTS_DIR.parent.parent
FAKE_STK = TESTS_DIR / "fake_stk.py"

# so `import stk_viewer` (and its submodules) resolves to the package at the
# repo root, regardless of the caller's own working directory
sys.path.insert(0, str(REPO))

# Redirect settings.json to a throwaway directory so a test run can never
# write into the developer's real ~/.config/stk-viewer/settings.json - which
# is exactly what happened before this existed: a scratch "fake_binary" path
# ended up persisted in a live config and showing up in the GUI.
#
# HOME is deliberately left alone: default_patched_stk_binary() resolves
# through XDG_DATA_HOME / ~/.local/share, and moving HOME would hide a real
# patches/build.sh output that some tests exist to find.
_CONFIG_DIR = tempfile.mkdtemp(prefix="stk-viewer-test-config-")
os.environ["XDG_CONFIG_HOME"] = _CONFIG_DIR   # Linux/BSD branch
os.environ["APPDATA"] = _CONFIG_DIR           # Windows branch


def build_app(extra_dirs=None):
    """
    Constructs the real App (stk_viewer.gui.app.App) - the same class
    run_gui() builds a window around - with Tk.mainloop stubbed so
    constructing it never blocks.
    """
    import tkinter as tk

    from stk_viewer.gui.app import App
    tk.Tk.mainloop = lambda self, n=0: None
    root = tk.Tk()
    return App(root, extra_dirs or [])


def pump(app, seconds, step=0.01):
    """Runs the real Tk event loop for roughly this long, in small slices -
    which is what actually drains self.q via the App's own
    self.root.after(80, self._poll) chain.  A stubbed loop would prove
    nothing about real key/command handling."""
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.root.update()
        time.sleep(step)


def wait_for(app, pred, timeout=5.0, step=0.02):
    """Pumps the event loop until pred() is true or timeout elapses."""
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.root.update()
        time.sleep(step)
        if pred():
            return True
    return False


def scratch_dir(prefix="stk-viewer-test-"):
    """A fresh throwaway directory for a test's own temp files (logs,
    stand-in binaries, probe files), instead of a hardcoded /tmp path."""
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix))


def stk_binary_root(binary: str) -> pathlib.Path:
    """Given a path to a patched supertuxkart binary at the usual
    <root>/build/bin/<exe> layout, returns <root> - the working directory
    the game must be launched from so its data/ resolves (see
    stk_viewer.game.launcher.launch_stk)."""
    return pathlib.Path(binary).resolve().parent.parent.parent


def make_fake_stk_binary(root: pathlib.Path) -> pathlib.Path:
    """
    Writes a stand-in "supertuxkart" binary at <root>/build/bin/supertuxkart
    - the same layout patches/build.sh produces - that records its own argv
    and cwd to <root>/launch_probe.txt instead of actually starting a game.

    Used to test the Launch button's process-spawning logic (the right flag,
    the right working directory) without needing the real ~700MB engine
    build.
    """
    bindir = root / "build" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    script = bindir / "supertuxkart"
    script.write_text(
        "#!/usr/bin/env bash\n"
        '# Stands in for the real STK binary in test_launch_button.py.\n'
        'out="$(dirname "$0")/../../launch_probe.txt"\n'
        "{\n"
        '    echo "argv0=$0"\n'
        '    echo "args=$*"\n'
        '    echo "cwd=$(pwd)"\n'
        '    echo "data_exists=$([ -d ./data ] && echo yes || echo no)"\n'
        "} > \"$out\"\n"
        "sleep 5\n")
    script.chmod(0o755)
    return script


class Checker:
    """Tiny pass/fail tally shared by every test in this directory, so each
    one doesn't reimplement the same three lines with module globals."""
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print(f"  ok   {label}")
        else:
            self.failed += 1
            print(f"  FAIL {label}")
        return cond

    def summary(self):
        print(f"\n{self.passed} passed, {self.failed} failed")
        return 0 if self.failed == 0 else 1


def find_local_replays(track="hacienda", n=2):
    """
    Up to n distinct locally-recorded replays on the given track.  Returns a
    list, possibly shorter than n (even empty) - this depends on the
    developer actually having recorded runs, so callers should skip rather
    than fail if there aren't enough.
    """
    from stk_viewer.replay.parser import default_replay_dirs, replay_header

    found = []
    for d in default_replay_dirs():
        for f in sorted(pathlib.Path(d).glob("*.replay")):
            try:
                rp = replay_header(str(f))
            except (SystemExit, OSError, ValueError):
                continue
            if rp.track == track:
                found.append(str(f))
            if len(found) >= n:
                return found
    return found
