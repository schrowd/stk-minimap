from __future__ import annotations

import os
import queue
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import __version__
from ..game.discovery import default_patched_stk_binary
from ..rendering.styles import STYLES
from ..replay.parser import Replay
from ..sync.client import SyncClient
from ..sync.protocol import SYNC_DEFAULT_PORT
from ..tracks.discovery import (find_tracks, read_track_info, resolve_track,
                                track_is_addon, track_kind, track_renderable)
from ..tracks.framing import Framing
from .render_tab import RenderTabMixin
from .replay_tab import ReplayTabMixin
from .settings import FILTER_KINDS, FILTER_SOURCES, load_settings, save_settings
from .widgets import PREVIEW


class App(RenderTabMixin, ReplayTabMixin):
    def __init__(self, root, extra_dirs):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.tmpdirs: list[str] = []
        self.extra_dirs = list(extra_dirs)
        self.settings = load_settings()
        self.tracks = find_tracks(self.extra_dirs)
        self.meta: dict[str, dict] = {}
        self.scan_tracks()
        self.current = None          # (Image, Framing, Graph, TrackInfo)
        self.busy = False
        self.frame: Framing | None = None
        self.replay: Replay | None = None
        self.replay_b: Replay | None = None   # the one being compared
        self.rp_t = 0.0              # playback head, seconds
        self.rp_playing = False
        self.rp_last = 0.0           # monotonic clock at the last tick
        self.rp_scrubbing = False
        self.rp_cache: list = []     # route projected to canvas coords
        self.rp_drawn_lap = None     # which lap the static layer shows
        self.rot_job = None          # pending debounced rotation redraw
        self.splits_checks: list = []    # check lines for the loaded track
        self.splits_data: list = []      # last computed LapSplit list
        self.sync_client: SyncClient | None = None
        self.sync_connected = False
        self._sync_local_until = 0.0     # monotonic deadline, see holdoff
        self._sync_rate_guard = False    # suppress the rate trace's send
        root.title(f"STK Viewer {__version__}")
        root.minsize(880, 560)

        outer = ttk.Frame(root, padding=8)
        outer.pack(fill="both", expand=True)

        # ---- left: track list -------------------------------------
        left = ttk.Frame(outer)
        left.pack(side="left", fill="both", expand=False)
        ttk.Label(left, text="Track").pack(anchor="w")
        self.filter = tk.StringVar()
        ent = ttk.Entry(left, textvariable=self.filter, width=30)
        ent.pack(fill="x")
        ent.insert(0, "")
        self.filter.trace_add("write", lambda *_: self.refill())

        # remembered from last time; anything unrecognised falls back, so a
        # stale or edited settings file can't hide every track
        saved = self.settings
        kind = saved.get("filter_kind")
        src = saved.get("filter_source")

        f1 = ttk.Frame(left); f1.pack(fill="x", pady=(4, 0))
        self.f_kind = tk.StringVar(
            value=kind if kind in FILTER_KINDS else FILTER_KINDS[0])
        ttk.Combobox(f1, textvariable=self.f_kind, state="readonly", width=11,
                     values=FILTER_KINDS).pack(side="left", fill="x",
                                               expand=True)
        self.f_src = tk.StringVar(
            value=src if src in FILTER_SOURCES else FILTER_SOURCES[0])
        ttk.Combobox(f1, textvariable=self.f_src, state="readonly", width=11,
                     values=FILTER_SOURCES).pack(side="left", fill="x",
                                                 expand=True, padx=(4, 0))
        self.f_kind.trace_add("write", lambda *_: self.filters_changed())
        self.f_src.trace_add("write", lambda *_: self.filters_changed())

        self.f_names = tk.BooleanVar(value=bool(saved.get("show_names")))
        ttk.Checkbutton(left, text="Show in-game names",
                        variable=self.f_names,
                        command=self.filters_changed).pack(anchor="w",
                                                           pady=(3, 0))

        box = ttk.Frame(left)
        box.pack(fill="both", expand=True, pady=(4, 4))
        self.listbox = tk.Listbox(box, width=30, height=22,
                                  exportselection=False, activestyle="none")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.listbox.bind("<<ListboxSelect>>", lambda _e: self.preview())

        btns = ttk.Frame(left)
        btns.pack(fill="x")
        ttk.Button(btns, text="Add folder…",
                   command=self.add_folder).pack(side="left", expand=True, fill="x")
        ttk.Button(btns, text="Open .zip…",
                   command=self.open_zip).pack(side="left", expand=True, fill="x")

        # ---- right: preview + tabs ---------------------------------
        right = ttk.Frame(outer, padding=(10, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)

        # a Canvas rather than a Label: replay playback moves a marker every
        # frame, and moving a canvas item beats re-rasterising the image
        self.canvas = tk.Canvas(right, background="#2b2e33", width=PREVIEW,
                                height=PREVIEW, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.img_x = self.img_y = 0

        self.info = ttk.Label(right, text="Pick a track.", anchor="w",
                              justify="left")
        self.info.pack(fill="x", pady=(6, 4))

        # Two tabs rather than one ever-growing column: rendering options
        # and replay analysis are two different tasks, and stacking both
        # in one frame was outgrowing the window.  Canvas, info line and
        # the status bar below stay outside the notebook, so switching
        # tabs never hides the preview or an in-progress save/export.
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True, pady=(2, 0))
        tab_render = ttk.Frame(nb, padding=8)
        tab_replay = ttk.Frame(nb, padding=8)
        nb.add(tab_render, text="Render")
        nb.add(tab_replay, text="Replay")

        # ---- Render tab ---------------------------------------------
        self.style = tk.StringVar(value="clean")
        self.size = tk.IntVar(value=512)
        self.ss = tk.IntVar(value=4)
        self.v_title = tk.BooleanVar(value=False)
        self.v_checks = tk.BooleanVar(value=False)
        self.v_invis = tk.BooleanVar(value=False)
        self.v_rev = tk.BooleanVar(value=False)
        self.v_fit = tk.BooleanVar(value=False)
        self.v_full = tk.BooleanVar(value=False)

        r1 = ttk.Frame(tab_render); r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="Style").pack(side="left")
        cb = ttk.Combobox(r1, textvariable=self.style, width=10, state="readonly",
                          values=sorted(STYLES))
        cb.pack(side="left", padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", lambda _e: self.preview())
        ttk.Label(r1, text="Size").pack(side="left")
        sz = ttk.Combobox(r1, textvariable=self.size, width=7, state="readonly",
                          values=(256, 512, 1024, 2048))
        sz.pack(side="left", padx=(4, 12))
        ttk.Label(r1, text="Quality").pack(side="left")
        ttk.Spinbox(r1, from_=1, to=8, width=4,
                    textvariable=self.ss).pack(side="left", padx=(4, 12))
        ttk.Label(r1, text="Rotate").pack(side="left")
        self.rotate = tk.StringVar(value="0")
        self.rotate_spin = ttk.Spinbox(r1, from_=0, to=345, increment=15,
                                       width=5, wrap=True,
                                       textvariable=self.rotate)
        self.rotate_spin.pack(side="left", padx=(4, 0))
        # watch the variable rather than the widget: the spinbox's own
        # command only fires for its arrows, so a typed angle would be
        # ignored.  Debounced, or every keystroke would trigger a render.
        self.rotate.trace_add("write", lambda *_: self.rotate_changed())
        ttk.Label(r1, text="°").pack(side="left")
        self.rotate_lock_note = ttk.Label(r1, text="",
                                          foreground="#6b7280")
        self.rotate_lock_note.pack(side="left", padx=(6, 0))

        r2 = ttk.Frame(tab_render); r2.pack(fill="x", pady=2)
        for text, var in (("Track name", self.v_title),
                          ("Check lines", self.v_checks),
                          ("Hidden quads", self.v_invis),
                          ("Reverse", self.v_rev),
                          ("Crop to track", self.v_fit),
                          ("Full n-gons", self.v_full)):
            ttk.Checkbutton(r2, text=text, variable=var,
                            command=self.preview).pack(side="left", padx=(0, 10))

        r3 = ttk.Frame(tab_render); r3.pack(fill="x", pady=(10, 0))
        self.save_btn = ttk.Button(r3, text="Save PNG…", command=self.save)
        self.save_btn.pack(side="left")
        self.all_btn = ttk.Button(r3, text="Save every track…",
                                  command=self.save_all)
        self.all_btn.pack(side="left", padx=6)

        # ---- Replay tab -----------------------------------------------
        q1 = ttk.Frame(tab_replay); q1.pack(fill="x")
        ttk.Button(q1, text="Browse replays…",
                   command=self.browse_replays).pack(side="left")
        ttk.Button(q1, text="Open replay…",
                   command=self.open_replay).pack(side="left", padx=(4, 0))
        self.rp_cmp_btn = ttk.Button(
            q1, text="Compare with…",
            command=lambda: self.browse_replays(target="b"),
            state="disabled")
        self.rp_cmp_btn.pack(side="left", padx=(4, 0))
        self.rp_info = ttk.Label(q1, text="none loaded", anchor="w")
        self.rp_info.pack(side="left", padx=8, fill="x", expand=True)

        q2 = ttk.Frame(tab_replay); q2.pack(fill="x", pady=(5, 0))
        self.rp_back = ttk.Button(q2, text="⏮", width=3,
                                  command=self.rp_restart, state="disabled")
        self.rp_back.pack(side="left")
        self.rp_play_btn = ttk.Button(q2, text="▶", width=3,
                                      command=self.rp_toggle, state="disabled")
        self.rp_play_btn.pack(side="left", padx=(3, 8))
        self.rp_rate = tk.StringVar(value="1x")
        ttk.Combobox(q2, textvariable=self.rp_rate, width=5, state="readonly",
                     values=("0.1x", "0.25x", "0.5x", "1x", "2x",
                             "4x")).pack(side="left")
        self.rp_rate.trace_add("write", lambda *_: self.sync_rate_changed())
        self.rp_pos = tk.DoubleVar(value=0.0)
        self.rp_scale = ttk.Scale(q2, from_=0.0, to=1.0, variable=self.rp_pos,
                                  command=self.rp_scrub, state="disabled")
        self.rp_scale.pack(side="left", fill="x", expand=True, padx=8)
        self.rp_clock = ttk.Label(q2, text="0:00.0 / 0:00.0", width=17,
                                  anchor="e")
        self.rp_clock.pack(side="right")

        ttk.Label(tab_replay,
                 text="Space: play/pause    ←/→: step one frame    "
                      "Home/End: start/end", anchor="w",
                 foreground="#6b7280").pack(fill="x", pady=(3, 0))

        # Live sync with a patched SuperTuxKart (patches/PROTOCOL.md):
        # pausing, scrubbing or slowing a run in one window does the same
        # in the other.  Independent of everything else in this tab - the
        # viewer works exactly as before if this is never touched.
        ql = ttk.Frame(tab_replay); ql.pack(fill="x", pady=(5, 0))
        saved_bin = self.settings.get("stk_binary")
        initial_bin = (saved_bin if saved_bin and os.path.isfile(saved_bin)
                      else default_patched_stk_binary()) or ""
        self.stk_binary = tk.StringVar(value=initial_bin)
        self.stk_launch_btn = ttk.Button(ql, text="Launch SuperTuxKart",
                                         command=self.sync_launch_stk)
        self.stk_launch_btn.pack(side="left")
        ttk.Button(ql, text="…", width=2,
                  command=self.browse_stk_binary).pack(side="left", padx=(2, 8))
        self.stk_binary_lbl = ttk.Label(ql, text="", foreground="#6b7280")
        self.stk_binary_lbl.pack(side="left", fill="x", expand=True)
        self.update_stk_binary_label()

        qs = ttk.Frame(tab_replay); qs.pack(fill="x", pady=(5, 0))
        ttk.Label(qs, text="Sync with SuperTuxKart, port").pack(side="left")
        self.sync_port = tk.StringVar(
            value=str(self.settings.get("sync_port", SYNC_DEFAULT_PORT)))
        ttk.Entry(qs, textvariable=self.sync_port,
                 width=6).pack(side="left", padx=(4, 8))
        self.sync_btn = ttk.Button(qs, text="Connect",
                                   command=self.sync_toggle)
        self.sync_btn.pack(side="left")
        self.sync_status_lbl = ttk.Label(qs, text="not connected",
                                         foreground="#6b7280")
        self.sync_status_lbl.pack(side="left", padx=(8, 0))

        # One "colour by" choice rather than four layers that stack: drawing
        # speed, nitro and skids on the same line at once is what made the
        # map unreadable.  Laps stack too, so default to showing just the
        # one the playhead is in.
        q3 = ttk.Frame(tab_replay); q3.pack(fill="x", pady=(5, 0))
        ttk.Label(q3, text="Lap").pack(side="left")
        self.rp_lap = tk.StringVar(value="Follow")
        self.rp_lap_box = ttk.Combobox(q3, textvariable=self.rp_lap, width=7,
                                       state="readonly",
                                       values=("Follow", "All"))
        self.rp_lap_box.pack(side="left", padx=(4, 12))
        ttk.Label(q3, text="Colour").pack(side="left")
        self.rp_colour = tk.StringVar(value="Speed")
        ttk.Combobox(q3, textvariable=self.rp_colour, width=13,
                     state="readonly",
                     values=("Speed", "Nitro & skid",
                             "Plain")).pack(side="left", padx=(4, 12))
        self.rp_v_items = tk.BooleanVar(value=True)
        ttk.Checkbutton(q3, text="Items / zippers", variable=self.rp_v_items,
                        command=self.rp_redraw).pack(side="left")
        self.rp_lap.trace_add("write", lambda *_: self.rp_redraw())
        self.rp_colour.trace_add("write", lambda *_: self.rp_redraw())
        self.rp_readout = ttk.Label(tab_replay, text="", anchor="w",
                                    justify="left")
        self.rp_readout.pack(fill="x", pady=(5, 0))

        # ---- sector splits, from the track's own check lines -------
        sp = ttk.LabelFrame(tab_replay, text="Splits", padding=6)
        sp.pack(fill="both", expand=True, pady=(8, 0))
        self.splits_note = ttk.Label(sp, text="Load a replay to see "
                                               "sector splits.", anchor="w")
        self.splits_note.pack(fill="x")
        st_box = ttk.Frame(sp)
        st_box.pack(fill="both", expand=True, pady=(4, 4))
        self.splits_tree = ttk.Treeview(st_box, show="headings", height=5)
        st_sb = ttk.Scrollbar(st_box, orient="vertical",
                              command=self.splits_tree.yview)
        self.splits_tree.configure(yscrollcommand=st_sb.set)
        self.splits_tree.pack(side="left", fill="both", expand=True)
        st_sb.pack(side="right", fill="y")

        sp_btn = ttk.Frame(sp); sp_btn.pack(fill="x")
        ttk.Button(sp_btn, text="Export telemetry CSV…",
                   command=self.export_telemetry_csv).pack(side="left")
        ttk.Button(sp_btn, text="Export splits CSV…",
                   command=self.export_splits_csv).pack(side="left", padx=6)

        # ---- status bar: outside the notebook, always visible ------
        statusbar = ttk.Frame(right); statusbar.pack(fill="x", pady=(6, 0))
        self.status = ttk.Label(statusbar, text="", anchor="e")
        self.status.pack(side="right", fill="x", expand=True)

        # keyboard control for playback; guarded so typing in a text field
        # (the track search box, the rotate spinbox) is never hijacked.
        # Binding on root alone isn't enough: Button, Checkbutton, Scale,
        # Treeview and Listbox all have their own default bindings for
        # space/arrows (activate, nudge, navigate), which take priority
        # over a toplevel binding and swallow the keypress before it ever
        # reaches root - so pressing space while focus happens to be on,
        # say, the scrub slider you just dragged does nothing.  Bound
        # directly on every such widget too, returning "break" so the
        # widget's own action doesn't also fire alongside ours.
        self.bind_playback_keys(root)
        self.harden_playback_keys(root)

        self.refill()
        self._poll()
        if not self.tracks:
            self.info.configure(
                text="No SuperTuxKart tracks found.\n"
                     "Use “Add folder…” to point at your STK data\\tracks "
                     "folder, or “Open .zip…” for an addon.")
        root.protocol("WM_DELETE_WINDOW", self.quit)

    # -- helpers ------------------------------------------------------

    def quit(self):
        if self.sync_client:
            self.sync_client.stop()
        for d in self.tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
        self.root.destroy()

    def scan_tracks(self):
        """
        Read every track.xml once, for the display name, the type and
        whether it is an add-on.  ~2ms for a stock install, so it can just
        happen up front rather than on every keystroke in the filter box.
        """
        self.meta = {}
        for ident, path in self.tracks.items():
            try:
                ti = read_track_info(path)
            except SystemExit:
                continue
            self.meta[ident] = dict(name=ti.name or ident,
                                    kind=track_kind(ti),
                                    addon=track_is_addon(path),
                                    renderable=track_renderable(ti))

    def filters_changed(self):
        self.refill()
        self.settings.update(filter_kind=self.f_kind.get(),
                             filter_source=self.f_src.get(),
                             show_names=bool(self.f_names.get()))
        save_settings(self.settings)

    def refill(self):
        f = self.filter.get().strip().lower()
        kind = self.f_kind.get()
        src = self.f_src.get()
        use_names = self.f_names.get()

        keep = []
        for ident in self.tracks:
            md = self.meta.get(ident)
            if not md or not md["renderable"]:
                continue          # cutscenes and GP screens have no graph
            if kind != "All types" and md["kind"] != kind.lower():
                continue
            if src == "Built-in" and md["addon"]:
                continue
            if src == "Add-ons" and not md["addon"]:
                continue
            if f and f not in ident.lower() and f not in md["name"].lower():
                continue
            keep.append(ident)

        # sort by whatever the user is actually reading
        keep.sort(key=lambda i: (self.meta[i]["name"] if use_names
                                 else i).lower())
        self.shown = keep
        self.listbox.delete(0, "end")
        for ident in keep:
            md = self.meta[ident]
            label = md["name"] if use_names else ident
            if md["addon"]:
                label += "  (add-on)"
            self.listbox.insert("end", label)

    def selected(self):
        sel = self.listbox.curselection()
        return self.shown[sel[0]] if sel else None

    def set_busy(self, on, msg=""):
        self.busy = on
        state = "disabled" if on else "normal"
        self.save_btn.configure(state=state)
        self.all_btn.configure(state=state)
        self.status.configure(text=msg)
        self.root.update_idletasks()

    # -- actions ------------------------------------------------------

    def _work(self, fn, done):
        """
        Tk may only be touched from the thread running the main loop, so the
        worker never calls a widget - it posts to a queue that the UI drains
        on a timer.  (Calling root.after() from the worker looks like it
        works and then raises "main thread is not in main loop".)
        """
        def run():
            try:
                res = fn()
            except Exception as exc:                      # noqa: BLE001
                res = exc
            self.q.put(("done", (done, res)))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.status.configure(text=payload)
                elif kind == "sync-status":
                    self.sync_on_status(*payload)
                elif kind == "sync-state":
                    self.sync_on_state(*payload)
                elif kind == "sync-replay":
                    self.sync_on_replay(payload)
                elif kind == "sync-duration":
                    pass    # informational only: rp_duration() is derived
                            # from the local file, which parses the same
                            # data STK's DURATION describes
                elif kind == "sync-bye":
                    self.sync_on_bye()
                else:
                    fn, res = payload
                    fn(res)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def add_folder(self):
        d = filedialog.askdirectory(title="Folder holding STK tracks")
        if not d:
            return
        self.extra_dirs.insert(0, d)
        self.tracks = find_tracks(self.extra_dirs)
        self.scan_tracks()
        self.refill()
        self.status.configure(text=f"{len(self.shown)} tracks")

    def open_zip(self):
        f = filedialog.askopenfilename(title="Addon track archive",
                                       filetypes=[("Track archive", "*.zip")])
        if not f:
            return
        try:
            d = resolve_track(f, self.extra_dirs, self.tmpdirs)
        except SystemExit as exc:
            messagebox.showerror("Could not open", str(exc))
            return
        ti = read_track_info(d)
        self.tracks[ti.ident] = d
        self.meta[ti.ident] = dict(name=ti.name or ti.ident,
                                   kind=track_kind(ti),
                                   addon=True,      # opened by hand = add-on
                                   renderable=track_renderable(ti))
        self.refill()
        if ti.ident in self.shown:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(self.shown.index(ti.ident))
            self.preview()


def run_gui(extra_dirs: list[str]) -> int:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    app = App(root, extra_dirs)          # keep a reference: the timer
    root.mainloop()                      # callbacks are the only other
    del app                              # thing holding it
    return 0
