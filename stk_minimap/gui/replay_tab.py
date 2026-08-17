from __future__ import annotations

import math
import os
import subprocess
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..game.launcher import launch_stk
from .settings import save_settings
from ..replay.analysis import compute_splits, write_replay_csv, write_splits_csv
from ..replay.comparison import ghost_gap
from ..replay.parser import (default_replay_dirs, load_replay, replay_date,
                             replay_source, scan_replays)
from ..replay.playback import _runs_by, lap_frame_range
from ..rendering.styles import _KART_COLOURS, _SKID_COLOURS, _SKID_NAMES, _SPEED_RAMP
from ..sync.client import SyncClient
from ..sync.protocol import SYNC_DEADBAND, SYNC_DEFAULT_PORT, SYNC_LOCAL_HOLDOFF
from ..tracks.scene import load_checklines
from .widgets import _mmss, arrow_points


class ReplayTabMixin:
    """App methods for the Replay tab: playback, splits, and live sync with
    a patched SuperTuxKart (patches/PROTOCOL.md)."""

    def open_replay(self):
        dirs = default_replay_dirs()
        f = filedialog.askopenfilename(
            title="SuperTuxKart replay",
            initialdir=dirs[0] if dirs else None,
            filetypes=[("STK replay", "*.replay"), ("All files", "*")])
        if f:
            self.use_replay(f)

    def use_replay(self, f, quiet=False):
        """
        Load a replay as run A.

        `quiet` routes failures to the status bar instead of a modal
        dialog, for loads the user didn't ask for directly - following
        the game's replay over sync shouldn't throw an error box in
        someone's face for a track they happen not to have.
        """
        def failed(title, detail, short):
            if quiet:
                self.status.configure(text=short)
            else:
                messagebox.showerror(title, detail)

        try:
            rp = load_replay(f)
        except (SystemExit, OSError, ValueError) as exc:
            failed("Could not read replay", str(exc),
                   f"could not read {os.path.basename(f)}: {exc}")
            return
        if rp.track not in self.tracks:
            failed(
                "Track not found",
                f"This replay is on “{rp.track}”, which isn't in any of the "
                f"track folders I know about.\n\nUse “Add folder…” to point "
                f"me at it, then open the replay again.",
                f"{os.path.basename(f)} is on “{rp.track}”, which isn't "
                f"in any track folder I know about")
            return

        self.replay = rp
        self.replay_b = None          # a new run A invalidates the compare
        self.rp_t = 0.0
        self.rp_playing = False
        self.lock_rotation()
        self.rp_play_btn.configure(text="▶", state="normal")
        self.rp_back.configure(state="normal")
        self.rp_scale.configure(state="normal")
        self.rp_cmp_btn.configure(state="normal")
        who = ", ".join(k.name or k.ident or "?" for k in rp.karts)
        self.rp_info.configure(
            text=f"{os.path.basename(f)} - {who} on {rp.track}, "
                 f"{rp.mode}, {rp.laps} lap(s), best {_mmss(rp.min_time)}")

        # the lap picker only makes sense once we know the lap count
        self.rp_lap.set("Follow")
        self.rp_lap_box.configure(
            values=("Follow", "All") + tuple(str(i + 1)
                                             for i in range(max(1, rp.laps))))
        self.rp_drawn_lap = None

        # match the map to the replay, then select its track, clearing any
        # filter that would otherwise hide it
        self.v_rev.set(rp.reverse)
        if rp.track not in self.shown:
            self.filter.set("")
            self.f_kind.set("All types")
            self.f_src.set("All sources")
            self.refill()
        if rp.track in self.shown:
            i = self.shown.index(rp.track)
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(i)
            self.listbox.see(i)
        self.preview()               # redraws, then calls rp_draw_static
        self.rp_update()
        self.refresh_splits()

    def refresh_splits(self):
        """
        Sector splits for the loaded replay's primary kart, from the
        track's own check lines.  Shows a note instead of a table when
        there's nothing loaded or the track has none to split by.
        """
        self.splits_tree.delete(*self.splits_tree.get_children())
        self.splits_checks = []
        self.splits_data = []

        if not self.replay:
            self.splits_tree.configure(columns=())
            self.splits_note.configure(text="Load a replay to see sector "
                                             "splits.")
            return

        track_dir = self.tracks.get(self.replay.track)
        checks = load_checklines(track_dir) if track_dir else []
        kart = self.replay.karts[0]
        splits = compute_splits(kart, checks)
        self.splits_checks = checks
        self.splits_data = splits

        if not splits:
            self.splits_tree.configure(columns=())
            self.splits_note.configure(
                text="This track has no check lines to split by.")
            return

        n = len(splits[0].sectors)
        cols = ["lap"] + [f"s{i}" for i in range(n)] + ["total"]
        self.splits_tree.configure(columns=cols)
        self.splits_tree.heading("lap", text="Lap")
        self.splits_tree.column("lap", width=44, anchor="w", stretch=False)
        for i in range(n):
            self.splits_tree.heading(f"s{i}", text=f"S{i + 1}")
            self.splits_tree.column(f"s{i}", width=52, anchor="e",
                                    stretch=False)
        self.splits_tree.heading("total", text="Total")
        self.splits_tree.column("total", width=60, anchor="e",
                                stretch=False)

        best: list[float | None] = [None] * n
        for ls in splits:
            for i, s in enumerate(ls.sectors):
                if s is not None and (best[i] is None or s < best[i]):
                    best[i] = s
            cells = [f"{s:.2f}" if s is not None else "—"
                    for s in ls.sectors]
            tot = f"{ls.total:.2f}" if ls.total is not None else "—"
            self.splits_tree.insert("", "end",
                                    values=[f"Lap {ls.lap + 1}"] + cells + [tot])
        if len(splits) > 1 and all(b is not None for b in best):
            self.splits_tree.insert(
                "", "end", tags=("best",),
                values=["Best"] + [f"{b:.2f}" for b in best] +
                       [f"{sum(best):.2f}"])
            self.splits_tree.tag_configure("best", font=("TkDefaultFont",
                                                          9, "bold"))
        self.splits_note.configure(text="")

    def export_telemetry_csv(self):
        if not self.replay:
            return
        entries = [(label, kart) for label, kart, _c in self.rp_entries()]
        default = f"{self.replay.karts[0].name or self.replay.karts[0].ident or self.replay.track}_telemetry.csv"
        f = filedialog.asksaveasfilename(
            title="Export replay telemetry", defaultextension=".csv",
            initialfile=default, filetypes=[("CSV", "*.csv")])
        if not f:
            return
        try:
            write_replay_csv(f, entries)
        except OSError as exc:
            messagebox.showerror("Could not write CSV", str(exc))
            return
        self.status.configure(text=f"wrote {os.path.basename(f)}")

    def export_splits_csv(self):
        if not self.splits_data:
            messagebox.showinfo(
                "No splits",
                "There's nothing to export - load a replay on a track "
                "that has check lines first.")
            return
        default = f"{self.replay.karts[0].name or self.replay.karts[0].ident or self.replay.track}_splits.csv"
        f = filedialog.asksaveasfilename(
            title="Export sector splits", defaultextension=".csv",
            initialfile=default, filetypes=[("CSV", "*.csv")])
        if not f:
            return
        try:
            write_splits_csv(f, self.splits_data)
        except OSError as exc:
            messagebox.showerror("Could not write CSV", str(exc))
            return
        self.status.configure(text=f"wrote {os.path.basename(f)}")

    # -- keyboard control for playback ---------------------------------

    def _typing_target(self, event) -> bool:
        return isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Spinbox,
                                         ttk.Combobox))

    def on_key_space(self, event):
        if self._typing_target(event):
            return None
        self.rp_toggle()
        return "break"

    def on_key_step(self, event, direction: int):
        if self._typing_target(event):
            return None
        if not self.replay:
            return "break"
        if self.rp_playing:
            self.rp_toggle()      # pause first, so stepping is predictable
        kart = self.replay.karts[0]
        i = kart.frame_at(self.rp_t)
        j = max(0, min(len(kart) - 1, i + direction))
        self.rp_t = kart.time[j]
        self.sync_send(f"SEEK {self.rp_t:.3f}")
        self.rp_update()
        return "break"

    def on_key_home(self, event):
        if self._typing_target(event):
            return None
        if not self.replay:
            return "break"
        if self.rp_playing:
            self.rp_toggle()
        self.rp_restart()
        return "break"

    def on_key_end(self, event):
        if self._typing_target(event):
            return None
        if not self.replay:
            return "break"
        if self.rp_playing:
            self.rp_toggle()
        self.rp_t = self.rp_duration()
        self.sync_send(f"SEEK {self.rp_t:.3f}")
        self.rp_update()
        return "break"

    def bind_playback_keys(self, widget):
        """One place both the toplevel binding and the per-widget
        hardening pass call, so the key set can't drift out of sync."""
        widget.bind("<space>", self.on_key_space)
        widget.bind("<Left>", lambda e: self.on_key_step(e, -1))
        widget.bind("<Right>", lambda e: self.on_key_step(e, 1))
        widget.bind("<Home>", self.on_key_home)
        widget.bind("<End>", self.on_key_end)

    def harden_playback_keys(self, widget):
        """
        Rebind space/arrows/home/end directly on every widget known to
        have its own default action for them (Button and Checkbutton
        activate on space; Scale, Treeview and Listbox nudge/navigate on
        the arrows), so play/pause and frame-stepping work no matter
        which widget happens to have focus.  Text-entry widgets are
        skipped - on_key_* already no-ops for those, and they still need
        their own arrow-key and space behaviour for editing.
        """
        if isinstance(widget, (tk.Entry, ttk.Entry, ttk.Spinbox,
                               ttk.Combobox)):
            return
        if isinstance(widget, (ttk.Button, ttk.Checkbutton, ttk.Scale,
                               ttk.Treeview, tk.Listbox)):
            self.bind_playback_keys(widget)
        for child in widget.winfo_children():
            self.harden_playback_keys(child)

    # -- live sync with a patched SuperTuxKart -------------------------
    # See patches/PROTOCOL.md.  Everything below either sends a command
    # in response to something the *user* just did, or applies a STATE
    # that came from the game - never both for the same event, which is
    # what avoids the feedback loop the protocol document warns about.

    def update_stk_binary_label(self):
        p = self.stk_binary.get()
        self.stk_binary_lbl.configure(
            text=p if p else "not found - run patches/build.sh, or "
                             "click Launch to locate it")

    def browse_stk_binary(self):
        f = filedialog.askopenfilename(
            title="Locate the patched SuperTuxKart binary",
            filetypes=[("SuperTuxKart", "supertuxkart.exe" if os.name == "nt"
                                        else "supertuxkart"),
                      ("All files", "*")])
        if not f:
            return None
        self.stk_binary.set(f)
        self.settings["stk_binary"] = f
        save_settings(self.settings)
        self.update_stk_binary_label()
        return f

    def sync_launch_stk(self):
        """
        Starts the patched game with --sync-port already set, so the one
        thing that actually broke this the first time it was tried -
        launching the game *without* the flag - can't happen from here.
        """
        path = self.stk_binary.get()
        if not path or not os.path.isfile(path):
            path = self.browse_stk_binary()
            if not path:
                return
        try:
            port = int(self.sync_port.get())
        except ValueError:
            port = SYNC_DEFAULT_PORT
        try:
            launch_stk(path, port)
        except OSError as exc:
            messagebox.showerror("Couldn't launch SuperTuxKart", str(exc))
            return
        self.status.configure(
            text=f"launched SuperTuxKart with --sync-port={port}")
        if not self.sync_client:
            # SyncClient already retries every 2s, which is exactly what
            # a game that takes a few seconds to boot needs - no reason
            # to make the user click Connect separately right after.
            self.sync_start()

    def sync_toggle(self):
        if self.sync_client:
            self.sync_stop()
        else:
            self.sync_start()

    def sync_start(self):
        try:
            port = int(self.sync_port.get())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror("Sync", "Port must be a number from "
                                         "1 to 65535.")
            return
        self.settings["sync_port"] = port
        save_settings(self.settings)
        self.sync_client = SyncClient("127.0.0.1", port, self.sync_post)
        self.sync_client.start()
        self.sync_btn.configure(text="Disconnect")
        self.sync_status_lbl.configure(text="connecting…")

    def sync_stop(self):
        if self.sync_client:
            self.sync_client.stop()
            self.sync_client = None
        self.sync_connected = False
        self.sync_btn.configure(text="Connect")
        self.sync_status_lbl.configure(text="not connected")

    def sync_post(self, kind, payload):
        """Called from the client's own thread - must never touch Tk.
        Routes through the same queue _work()'s background jobs use,
        drained by _poll() on the main thread."""
        self.q.put((kind, payload))

    def sync_send(self, line: str):
        """
        Send a playback command if connected, and mark that a local
        action just happened either way.

        The holdoff (PROTOCOL.md rule: "a local user action wins for
        250ms") matters even for the very next STATE that arrives after
        this: without it, a heartbeat already in flight when the user
        pressed play could land a moment later and immediately override
        what they just did.
        """
        self._sync_local_until = time.monotonic() + SYNC_LOCAL_HOLDOFF
        if self.sync_client and self.sync_connected:
            self.sync_client.send(line)

    def sync_rate_changed(self):
        if self._sync_rate_guard:
            return          # this change came from an inbound STATE
        self.sync_send(f"RATE {self.rp_rate_value()}")

    def sync_on_status(self, kind, detail):
        if kind == "connecting":
            self.sync_status_lbl.configure(text="connecting…")
        elif kind == "connected":
            self.sync_connected = True
            self.sync_status_lbl.configure(
                text="connected - waiting for a replay")
        elif kind == "retrying":
            self.sync_connected = False
            self.sync_status_lbl.configure(
                text="SuperTuxKart not reachable - retrying…")
        elif kind == "error":
            self.sync_connected = False
            self.sync_status_lbl.configure(text=f"connection error: "
                                                f"{detail}")

    def sync_on_bye(self):
        # The socket itself is still open at this point; the client
        # notices it close on its next read and starts retrying on its
        # own, so there's nothing to do here beyond reflecting it.
        self.sync_connected = False
        self.sync_status_lbl.configure(text="SuperTuxKart closed the "
                                            "connection")

    def sync_on_replay(self, path):
        """
        STK told us which replay it's watching, either on connect or
        because it just loaded one; load the same file locally so there
        is something to sync against.

        This is what makes "start the game, pick a replay, and the map
        follows" work with nothing loaded here beforehand - so it has to
        handle arriving at any time, not just at connect, and has to be
        quiet about a replay it can't open.
        """
        base = os.path.basename(path)
        self.sync_status_lbl.configure(text=f"connected - {base}")
        if self.replay and os.path.basename(self.replay.path) == base:
            return          # already showing it; nothing to do
        found = self._sync_find_replay(base)
        if not found:
            self.status.configure(
                text=f"SuperTuxKart is watching {base} - open it here "
                     f"manually to see it on the map")
            return
        self.use_replay(found, quiet=True)
        if self.replay and os.path.basename(self.replay.path) == base:
            self.status.configure(text=f"following {base} from "
                                       f"SuperTuxKart")
            # use_replay() starts from 0 and paused, so let the next
            # heartbeat (100 ms at most) move it to wherever the game
            # actually is.  Deliberately NOT sync_send("PING"): that
            # would start a local-action holdoff and suppress the very
            # STATE being asked for.  Clearing the holdoff instead - the
            # replay just changed underneath us, so nothing the user did
            # a moment ago is worth defending.
            self._sync_local_until = 0.0

    def _sync_find_replay(self, base: str) -> str | None:
        dirs = default_replay_dirs()
        if self.replay:
            dirs = [os.path.dirname(self.replay.path)] + dirs
        for d in dirs:
            cand = os.path.join(d, base)
            if os.path.isfile(cand):
                return cand
        return None

    def sync_on_state(self, t: float, playing: bool, rate: float):
        if not self.sync_connected:
            self.sync_connected = True
        if time.monotonic() < self._sync_local_until:
            return          # rule 3: a local action just happened
        if not self.replay:
            return          # nothing loaded yet to move
        changed = False
        if abs(t - self.rp_t) > SYNC_DEADBAND:      # rule 2: deadband
            self.rp_t = max(0.0, min(t, self.rp_duration()))
            changed = True
        if playing != self.rp_playing:
            self.rp_playing = playing
            self.rp_play_btn.configure(text="⏸" if playing else "▶")
            if playing:
                self.rp_last = time.monotonic()
                self.root.after(20, self.rp_tick)
            changed = True
        if abs(rate - self.rp_rate_value()) > 1e-6:
            self._sync_rate_guard = True
            self.rp_rate.set(f"{rate:g}x")
            self._sync_rate_guard = False
        if changed:
            self.rp_update()

    def open_compare(self):
        """Load a second replay alongside the first."""
        if not self.replay:
            return
        dirs = default_replay_dirs()
        f = filedialog.askopenfilename(
            title="Second replay to compare",
            initialdir=os.path.dirname(self.replay.path) or
            (dirs[0] if dirs else None),
            filetypes=[("STK replay", "*.replay"), ("All files", "*")])
        if not f:
            return
        if f:
            self.use_compare(f)

    def use_compare(self, f):
        """Load a replay as run B, alongside A."""
        if not self.replay:
            return
        if os.path.abspath(f) == os.path.abspath(self.replay.path):
            messagebox.showinfo("Same replay",
                                "That's the replay already loaded.")
            return
        try:
            rp = load_replay(f)
        except (SystemExit, OSError, ValueError) as exc:
            messagebox.showerror("Could not read replay", str(exc))
            return
        if rp.track != self.replay.track:
            messagebox.showerror(
                "Different track",
                f"That run is on “{rp.track}”, but the one loaded is on "
                f"“{self.replay.track}”.\n\nTwo runs can only be compared "
                f"on the same track.")
            return
        self.replay_b = rp
        self.rp_t = 0.0
        self.rp_playing = False
        self.rp_play_btn.configure(text="▶")
        self.rp_info.configure(
            text=f"A {os.path.basename(self.replay.path)}  "
                 f"({_mmss(self.replay.min_time)})   vs   "
                 f"B {os.path.basename(f)}  ({_mmss(rp.min_time)})")
        self.rp_redraw()

    def browse_replays(self, target="a"):
        """
        A sortable table of every replay on the machine, including the
        world records and challenge ghosts that ship with the game.

        target "a" picks the run to watch; "b" picks one to compare it
        against, and starts narrowed to the track already loaded - only
        runs on the same track can be compared.
        """
        if target == "b" and not self.replay:
            return
        reps = scan_replays(default_replay_dirs())
        if not reps:
            messagebox.showinfo(
                "No replays",
                "I couldn't find any .replay files.\n\nSTK writes them "
                "when you finish a race with recording turned on, and "
                "ships world records in its own data folder.")
            return

        win = tk.Toplevel(self.root)
        win.title("Compare with…" if target == "b" else "Replays")
        win.minsize(760, 460)
        win.transient(self.root)

        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Search").pack(side="left")
        q = tk.StringVar()
        ttk.Entry(top, textvariable=q, width=24).pack(side="left", padx=(4, 12))
        ttk.Label(top, text="Show").pack(side="left")
        src = tk.StringVar(value="All")
        ttk.Combobox(top, textvariable=src, state="readonly", width=14,
                     values=("All", "World record", "Ghost", "Challenge",
                             "Yours")).pack(side="left", padx=4)

        cols = ("track", "time", "laps", "driver", "date", "source")
        heads = {"track": "Track", "time": "Time", "laps": "Laps",
                 "driver": "Driver", "date": "Date", "source": "Source"}
        widths = {"track": 210, "time": 80, "laps": 50, "driver": 150,
                  "date": 100, "source": 110}
        mid = ttk.Frame(win, padding=(8, 0))
        mid.pack(fill="both", expand=True)
        tree = ttk.Treeview(mid, columns=cols, show="headings",
                            selectmode="browse")
        for c in cols:
            tree.heading(c, text=heads[c],
                         command=lambda c=c: sort_by(c))
            tree.column(c, width=widths[c],
                        anchor="e" if c in ("time", "laps") else "w")
        vs = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vs.set)
        tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        detail = ttk.Label(win, text="", anchor="w", padding=(10, 4),
                           justify="left")
        detail.pack(fill="x")

        rows = []
        for r in reps:
            md = self.meta.get(r.track)
            rows.append(dict(
                path=r.path,
                track=(md["name"] if md else r.track),
                ident=r.track,
                time=r.min_time,
                laps=r.laps,
                driver=(r.names[0][1] if r.names else ""),
                date=replay_date(r.path),
                source=replay_source(r.path),
                info=r.info,
                known=md is not None))

        state = {"col": "time", "desc": False}

        def visible():
            text = q.get().strip().lower()
            want = src.get()
            out = []
            for r in rows:
                if want != "All" and r["source"] != want:
                    continue
                if text and text not in r["track"].lower() \
                        and text not in r["driver"].lower() \
                        and text not in os.path.basename(r["path"]).lower():
                    continue
                out.append(r)
            out.sort(key=lambda r: r[state["col"]], reverse=state["desc"])
            return out

        import datetime

        def repopulate(*_):
            tree.delete(*tree.get_children())
            for r in visible():
                tree.insert("", "end", iid=r["path"], values=(
                    r["track"] + ("" if r["known"] else "  (not installed)"),
                    _mmss(r["time"]) if r["time"] else "—",
                    r["laps"] or "—",
                    r["driver"],
                    datetime.datetime.fromtimestamp(
                        r["date"]).strftime("%Y-%m-%d") if r["date"] else "",
                    r["source"]))

        def sort_by(col, toggle=True):
            # clicking the current column reverses it; anything else, and
            # the initial sort, starts ascending
            state["desc"] = (not state["desc"]
                             if toggle and state["col"] == col else False)
            state["col"] = col
            for c in cols:
                arrow = "" if c != col else ("  ▾" if state["desc"] else "  ▴")
                tree.heading(c, text=heads[c] + arrow)
            repopulate()

        def selected_row():
            sel = tree.selection()
            if not sel:
                return None
            return next((r for r in rows if r["path"] == sel[0]), None)

        def on_select(_e=None):
            r = selected_row()
            if not r:
                return
            bits = [os.path.basename(r["path"])]
            if r["info"]:
                bits.append(r["info"])
            if not r["known"]:
                bits.append("track not installed - can't draw this one")
            detail.configure(text="\n".join(bits))
        tree.bind("<<TreeviewSelect>>", on_select)

        def load_a(_e=None):
            r = selected_row()
            if not r:
                return
            win.destroy()
            self.use_replay(r["path"])

        def load_b():
            r = selected_row()
            if not r:
                return
            win.destroy()
            self.use_compare(r["path"])

        def pick_file():
            win.destroy()
            (self.open_compare if target == "b" else self.open_replay)()

        # whichever slot the window was opened for is the double-click
        tree.bind("<Double-1>", (lambda _e: load_b()) if target == "b"
                  else load_a)
        q.trace_add("write", repopulate)
        src.trace_add("write", repopulate)

        btns = ttk.Frame(win, padding=8)
        btns.pack(fill="x")
        if target == "b":
            ttk.Button(btns, text="Compare",
                       command=load_b).pack(side="left")
            ttk.Button(btns, text="Load as the main run instead",
                       command=load_a).pack(side="left", padx=6)
        else:
            ttk.Button(btns, text="Load", command=load_a).pack(side="left")
            cmp_btn = ttk.Button(btns, text="Compare with loaded run",
                                 command=load_b)
            cmp_btn.pack(side="left", padx=6)
            if not self.replay:
                cmp_btn.configure(state="disabled")
        ttk.Button(btns, text="Pick a file…",
                   command=pick_file).pack(side="left", padx=6)
        ttk.Button(btns, text="Close",
                   command=win.destroy).pack(side="right")
        ttk.Label(btns, text=f"{len(rows)} replays").pack(side="right",
                                                          padx=10)
        if target == "b":
            # only same-track runs can be compared, so start there; the
            # search box still shows it, so it can be cleared or changed
            md = self.meta.get(self.replay.track)
            q.set(md["name"] if md else self.replay.track)
        sort_by("time", toggle=False)
        win.update_idletasks()

    def rp_px(self, kart):
        """Replay world positions -> canvas coordinates."""
        fr = self.frame
        out = []
        for x, z in zip(kart.x, kart.z):
            px, py = fr.to_px(x, z)
            out.append((self.img_x + px, self.img_y + py))
        return out

    def rp_on_screen(self) -> bool:
        """True when the map on display is the one the replay was set on."""
        return bool(self.replay and self.frame and self.current
                    and self.current[0] == self.replay.track)

    def rp_entries(self):
        """
        Every kart on show, as (label, kart, colour).

        One replay with one kart is the common case; a ghost race gives two
        karts in one replay, and "Compare with…" gives a second replay.
        Everything downstream just walks this list.
        """
        out = []
        reps = [r for r in (self.replay, self.replay_b) if r]
        for ri, rep in enumerate(reps):
            for ki, kart in enumerate(rep.karts):
                who = kart.name or kart.ident or f"kart {ki + 1}"
                label = f"{'AB'[ri]} {who}" if len(reps) > 1 else who
                colour = _KART_COLOURS[(ri * 3 + ki) % len(_KART_COLOURS)]
                out.append((label, kart, colour))
        return out

    def rp_duration(self) -> float:
        return max((r.duration for r in (self.replay, self.replay_b) if r),
                   default=0.0)

    def rp_wanted_lap(self):
        """Which lap the static layer should show; None means all of them."""
        sel = self.rp_lap.get()
        if sel == "All" or not self.replay:
            return None
        if sel == "Follow":
            k = self.replay.karts[0]
            return k.lap[k.frame_at(self.rp_t)] if k.lap else None
        try:
            return int(sel) - 1
        except ValueError:
            return None

    def rp_redraw(self):
        self.rp_drawn_lap = None
        self.rp_draw_static()

    def rp_draw_static(self):
        """The parts that don't move: route, nitro, skids, item uses."""
        self.canvas.delete("rp")
        if not self.rp_on_screen():
            return
        # cache the projected route: rp_update runs 50x a second and must
        # not reproject thousands of points each time
        entries = self.rp_entries()
        self.rp_cache = [self.rp_px(k) for _l, k, _c in entries]
        lap = self.rp_wanted_lap()
        self.rp_drawn_lap = lap
        mode = self.rp_colour.get()

        for ki, (_label, kart, base) in enumerate(entries):
            pts = self.rp_cache[ki]
            if len(pts) < 2:
                continue
            a, b = lap_frame_range(kart, lap)

            def seg(i0, i1, colour, width):
                i0, i1 = max(i0, a), min(i1, b)
                if i1 > i0:
                    self.canvas.create_line(
                        *[c for p in pts[i0:i1 + 1] for c in p],
                        fill=colour, width=width, tags="rp")

            if mode == "Speed":
                # where a run is slow is where it is losing time, and that
                # reads instantly off the map
                top = max(kart.speed) or 1.0
                for i0, i1, bucket in _runs_by(kart.speed, lambda s:
                                              min(7, int(8 * s / top))):
                    seg(i0, i1, _SPEED_RAMP[bucket], 3)
            elif mode == "Nitro & skid":
                seg(a, b, "#4a5568", 2)
                levels = [kart.skid_level(i) for i in range(len(kart))]
                for i0, i1, lv in _runs_by(levels, lambda v: v):
                    if lv >= 2:      # only charged skids are interesting
                        seg(i0, i1, _SKID_COLOURS[lv], 3)
                for i0, i1, on in _runs_by(kart.nitro_use, bool):
                    if on:
                        seg(i0, i1, "#39e0ff", 3)
            else:
                seg(a, b, base, 2)

            if self.rp_v_items.get():
                for i0, i1, on in _runs_by(kart.zipper, bool):
                    if on and a <= i0 <= b:
                        x, y = pts[i0]
                        self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4,
                                                outline="#ffd23f", width=2,
                                                tags="rp")
                for i in kart.item_uses():
                    if a <= i <= b:
                        x, y = pts[i]
                        self.canvas.create_polygon(
                            x, y - 5, x + 5, y, x, y + 5, x - 5, y,
                            fill="#ff4fd8", outline="", tags="rp")
        self.rp_update()

    def rp_rate_value(self) -> float:
        try:
            return float(self.rp_rate.get().rstrip("x"))
        except ValueError:
            return 1.0

    def rp_arrow(self, x, y, world_heading, r):
        """
        A kart-shaped arrow at (x, y) pointing along a *world* heading.

        The heading is turned by the same angle as the map, so the arrow
        keeps pointing down the track when the view is rotated, and the
        image's flipped Y is what makes the screen angle negative.
        """
        fr = self.frame
        fx, fz = math.sin(world_heading), math.cos(world_heading)
        if fr is not None and fr.angle:
            c, s = math.cos(fr.angle), math.sin(fr.angle)
            fx, fz = fx * c - fz * s, fx * s + fz * c
        return arrow_points(x, y, math.atan2(-fz, fx), r)

    def rp_head(self, kart, pts, t):
        """
        Interpolated position, plus the frame it came from and how far past
        it we are.

        Replays run at about 15fps with gaps up to 100ms, while playback
        redraws at 50. Without interpolation the marker sits still for
        several redraws and then jumps - at top speed that is a 4.5 unit
        hop. The game interpolates ghost positions for exactly this reason.
        """
        i = kart.frame_at(t)
        if i + 1 >= len(pts):
            return pts[i], i, 0.0
        t0, t1 = kart.time[i], kart.time[i + 1]
        f = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
        f = 0.0 if f < 0.0 else (1.0 if f > 1.0 else f)
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        return (x0 + (x1 - x0) * f, y0 + (y1 - y0) * f), i, f

    def rp_toggle(self):
        if not self.replay:
            return
        if self.rp_t >= self.rp_duration():
            self.rp_t = 0.0
        self.rp_playing = not self.rp_playing
        self.rp_play_btn.configure(text="⏸" if self.rp_playing else "▶")
        self.sync_send("PLAY" if self.rp_playing else "PAUSE")
        if self.rp_playing:
            self.rp_last = time.monotonic()
            self.root.after(20, self.rp_tick)

    def rp_restart(self):
        self.rp_t = 0.0
        self.sync_send("SEEK 0")
        self.rp_update()

    def rp_scrub(self, _v):
        if not self.replay or self.rp_scrubbing:
            return
        self.rp_t = self.rp_pos.get() * self.rp_duration()
        self.sync_send(f"SEEK {self.rp_t:.3f}")
        self.rp_update(from_scrub=True)

    def rp_tick(self):
        if not (self.rp_playing and self.replay):
            return
        now = time.monotonic()
        self.rp_t += (now - self.rp_last) * self.rp_rate_value()
        self.rp_last = now
        if self.rp_t >= self.rp_duration():
            self.rp_t = self.rp_duration()
            self.rp_playing = False
            self.rp_play_btn.configure(text="▶")
        self.rp_update()
        if self.rp_playing:
            self.root.after(20, self.rp_tick)

    def rp_update(self, from_scrub=False):
        """Move the kart marker and refresh the readouts."""
        if not self.rp_on_screen() or not self.rp_cache:
            return
        # in Follow mode the visible lap changes as the run progresses;
        # rp_draw_static calls back here once the new lap is drawn
        want = self.rp_wanted_lap()
        if want != self.rp_drawn_lap:
            self.rp_draw_static()
            return
        self.canvas.delete("rpnow")
        lines = []
        entries = self.rp_entries()
        for ki, (label, kart, colour) in enumerate(entries):
            if not len(kart) or ki >= len(self.rp_cache):
                continue
            pts = self.rp_cache[ki]
            (x, y), i, frac = self.rp_head(kart, pts, self.rp_t)

            # the tail ends at the interpolated head, not the last frame,
            # or it visibly lags behind the marker
            tail = pts[max(0, i - 22):i + 1] + [(x, y)]
            if len(tail) > 1:
                self.canvas.create_line(*[c for p in tail for c in p],
                                        fill="#ffffff", width=3,
                                        tags="rpnow")
            # each kart is drawn a little smaller than the one before, so
            # two runs sitting on the same corner still read as two: the
            # larger rim stays visible around the smaller disc
            r = max(4.5, 8.0 - 2.0 * ki)
            # nitro sits outside the skid ring so the two never collide
            if kart.nitro_use[i]:
                self.canvas.create_oval(x - r - 9, y - r - 9,
                                        x + r + 9, y + r + 9,
                                        outline="#39e0ff", width=3,
                                        tags="rpnow")
            level = kart.skid_level(i)
            if level:
                self.canvas.create_oval(x - r - 4, y - r - 4,
                                        x + r + 4, y + r + 4,
                                        outline=_SKID_COLOURS[level],
                                        width=3, tags="rpnow")
            self.canvas.create_polygon(
                *self.rp_arrow(x, y, kart.heading_at(i, frac), r),
                fill=colour, outline="#101010", width=2, tags="rpnow")

            flags = []
            if kart.nitro_use[i]:
                flags.append("NITRO")
            flags.append(_SKID_NAMES[level])
            if kart.zipper[i]:
                flags.append("ZIPPER")
            laps = max(rp.laps for rp in (self.replay, self.replay_b) if rp)
            lap = f"lap {kart.lap[i] + 1}/{laps}   " \
                if kart.lap and laps > 1 else ""
            speed = kart.speed[i]
            if i + 1 < len(kart):        # smooth the number too
                speed += (kart.speed[i + 1] - speed) * frac
            lines.append(
                f"{label}:  {lap}{speed * 3.6:5.1f} km/h   "
                f"nitro {kart.nitro_amount[i]:.0f}   "
                f"items {kart.item_amount[i]}   "
                f"{'  '.join(f for f in flags if f)}")

        gap = self.rp_gap(entries)
        if gap:
            lines.append(gap)
        self.rp_readout.configure(text="\n".join(lines))
        self.rp_clock.configure(
            text=f"{_mmss(self.rp_t)} / {_mmss(self.rp_duration())}")
        if not from_scrub:
            self.rp_scrubbing = True
            self.rp_pos.set(self.rp_t / (self.rp_duration() or 1.0))
            self.rp_scrubbing = False

    def rp_gap(self, entries) -> str:
        """
        How far apart two runs are, in seconds at the same point on track.

        Comparing positions at the same *time* only says who is ahead;
        what a run is actually worth is the time difference at the same
        *place*, which is what a ghost shows you.
        """
        if not (self.replay_b and len(entries) >= 2):
            return ""
        a_kart = entries[0][1]
        b_kart = next((k for _l, k, _c in entries[1:]
                       if k is not entries[0][1]), None)
        if b_kart is None:
            return ""
        i = a_kart.frame_at(self.rp_t)
        if a_kart.distance[i] <= 0:
            return "Δ  —"
        d = ghost_gap(a_kart, b_kart, self.rp_t)
        if d is None:
            return "Δ  B hasn't reached this point"
        who = "B behind" if d > 0 else "B ahead"
        return f"Δ  {who} by {abs(d):.2f}s at this point on track"
