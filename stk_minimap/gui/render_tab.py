from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox

from PIL import ImageTk

from ..tracks.minimap import build
from .widgets import PREVIEW, _checkerboard, _gui_args


class RenderTabMixin:
    """App methods for the Render tab: style/size options, preview, save."""

    def rotate_value(self) -> float:
        """The spinbox is editable, so a typed value may be nonsense."""
        try:
            return float(self.rotate.get())
        except (ValueError, tk.TclError):
            return 0.0

    def rotate_changed(self):
        if self.rot_job is not None:
            try:
                self.root.after_cancel(self.rot_job)
            except tk.TclError:
                pass
        self.rot_job = self.root.after(300, self.rotate_apply)

    def rotate_apply(self):
        self.rot_job = None
        self.preview()

    def lock_rotation(self):
        """
        Pin rotation to 0 and stop it being changed, the moment a replay
        is loaded.  Rotate is the one control that can make what's on
        screen stop matching the real in-game minimap orientation - fine
        for a plain map you're turning into a diagram, not fine for a
        replay, which is only useful if it's trustworthy against what
        actually happened in the race.  There's no "close replay" action
        in this app, so once a replay is loaded the lock just stays on
        for the rest of the session.
        """
        self.rotate.set("0")
        self.rotate_spin.configure(state="disabled")
        self.rotate_lock_note.configure(
            text="(locked to match the in-game minimap while a replay "
                 "is loaded)")

    def args(self, size=None, ss=None):
        return _gui_args(style=self.style.get(),
                         size=size or self.size.get(),
                         supersample=ss or self.ss.get(),
                         title=self.v_title.get(),
                         show_invisible=self.v_invis.get(),
                         reverse=self.v_rev.get(),
                         fit=self.v_fit.get(),
                         full_polys=self.v_full.get(),
                         checklines=self.v_checks.get(),
                         rotate=self.rotate_value())

    def preview(self):
        ident = self.selected()
        if not ident or self.busy:
            return
        # matches the currently viewed replay's karts (both, if comparing)
        # so the canvas grows for a shortcut the same way the CLI does
        extra_karts = []
        if self.replay and self.replay.track == ident:
            extra_karts += self.replay.karts
            if self.replay_b and self.replay_b.track == ident:
                extra_karts += self.replay_b.karts

        # cheap settings: the preview only has to look right, not be final
        try:
            img, fr, g, ti = build(self.tracks[ident],
                                   self.args(size=PREVIEW, ss=2),
                                   extra_karts=extra_karts)
        except SystemExit as exc:
            self.info.configure(text=f"{ident}: {exc}")
            self.canvas.delete("all")
            return
        self.current = (ident, ti, g, fr)
        self.frame = fr
        shown = _checkerboard(img.size)
        shown.paste(img, mask=img.split()[3])
        self.photo = ImageTk.PhotoImage(shown)
        self.canvas.delete("all")
        cw = self.canvas.winfo_width() or shown.width
        ch = self.canvas.winfo_height() or shown.height
        self.img_x = max(0, (cw - shown.width) // 2)
        self.img_y = max(0, (ch - shown.height) // 2)
        self.canvas.create_image(self.img_x, self.img_y, anchor="nw",
                                 image=self.photo)
        if self.replay and self.replay.track == ident:
            self.rp_draw_static()
        vis = sum(1 for n in g.nodes if not n.invisible)
        kind = "arena / navmesh" if g.kind == "arena" else "driveline"
        self.info.configure(
            text=f"{ti.name}  [{ti.ident}]   {kind}   "
                 f"{len(g.nodes)} quads ({vis} visible)\n"
                 f"px = (x − {fr.origin_x:.2f}) × {fr.scaling:.4f}     "
                 f"py = {fr.height} − (z − {fr.origin_z:.2f}) "
                 f"× {fr.scaling:.4f}"
                 + ("   (after rotating about the centre)"
                    if fr.angle else ""))

    def save(self):
        ident = self.selected()
        if not ident or self.busy:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".png", filetypes=[("PNG image", "*.png")],
            initialfile=f"{ident}_minimap.png")
        if not path:
            return
        self.set_busy(True, "rendering…")

        def job():
            img, _fr, _g, _ti = build(self.tracks[ident], self.args())
            img.save(path)
            return path

        def done(res):
            self.set_busy(False, "")
            if isinstance(res, Exception):
                messagebox.showerror("Save failed", str(res))
            else:
                self.status.configure(text=f"saved {os.path.basename(res)}")

        self._work(job, done)

    def save_all(self):
        if self.busy or not self.tracks:
            return
        folder = filedialog.askdirectory(title="Save every minimap into…")
        if not folder:
            return
        self.set_busy(True, "rendering…")
        args = self.args()
        items = sorted(self.tracks.items())

        def job():
            ok = 0
            for i, (ident, path) in enumerate(items, 1):
                self.q.put(("status", f"{i}/{len(items)}  {ident}"))
                try:
                    img, _fr, _g, _ti = build(path, args)
                except SystemExit:
                    continue                     # cutscenes have no graph
                img.save(os.path.join(folder, f"{ident}_minimap.png"))
                ok += 1
            return ok

        def done(res):
            self.set_busy(False, "")
            if isinstance(res, Exception):
                messagebox.showerror("Save failed", str(res))
            else:
                self.status.configure(text=f"wrote {res} minimaps")

        self._work(job, done)
