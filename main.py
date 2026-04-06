#!/usr/bin/env python3
"""H264 VAAPI Encoder – GTK3 GUI"""

import os
import threading
import gi
from urllib.parse import unquote

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, GdkPixbuf, Pango

from encoder import (
    Encoder, EncodeJob,
    get_fps, get_video_dimensions, compute_output_dimensions,
    get_streams, HIGH_FPS_THRESHOLD,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VIDEO_BITRATES = [
    ("500 kbps",   "500k"),
    ("1000 kbps",  "1000k"),
    ("2000 kbps",  "2000k"),
    ("4000 kbps",  "4000k"),
    ("6000 kbps",  "6000k"),
    ("8000 kbps",  "8000k"),
    ("12000 kbps", "12000k"),
    ("16000 kbps", "16000k"),
    ("20000 kbps", "20000k"),
]

AUDIO_BITRATES = [
    ("64 kbps",  "64k"),
    ("96 kbps",  "96k"),
    ("128 kbps", "128k"),
    ("192 kbps", "192k"),
    ("256 kbps", "256k"),
    ("320 kbps", "320k"),
]

DEFAULT_VIDEO_IDX = 3   # 4000 kbps
DEFAULT_AUDIO_IDX = 2   # 128 kbps

# (label, target_height_or_None)
RESOLUTIONS = [
    ("Original (beibehalten)", None),
    ("480p  (854 × 480)",       480),
    ("720p  (1280 × 720)",      720),
    ("1080p (1920 × 1080)",    1080),
    ("1440p (2560 × 1440)",    1440),
    ("4K    (3840 × 2160)",    2160),
]
DEFAULT_RES_IDX = 0   # Original

# TreeView columns
COL_FILENAME    = 0
COL_DIRECTORY   = 1
COL_STATUS      = 2
COL_PROGRESS    = 3
COL_FULLPATH    = 4
COL_AUDIO_LABEL = 5   # summary text, e.g. "2 Spuren" / "1/3 aktiv"
COL_SUB_LABEL   = 6   # summary text, e.g. "Keine" / "2 Spuren"

STATUS_PENDING  = "Ausstehend"
STATUS_ENCODING = "Wird kodiert…"
STATUS_DONE     = "Fertig"
STATUS_ERROR    = "Fehler"
STATUS_CANCELLED = "Abgebrochen"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_output_path(
    input_path: str,
    output_dir: str,
    use_source_dir: bool,
    replace_original: bool,
    custom_suffix: str,
) -> str:
    """Compute the output file path from settings."""
    base, _ = os.path.splitext(input_path)
    base_name = os.path.basename(base)
    src_dir   = os.path.dirname(input_path)

    if use_source_dir:
        target_dir = src_dir
    else:
        target_dir = output_dir

    if replace_original:
        # Write to a temp name, then replace_original logic swaps it later.
        # Use same dir as source so os.replace works across mount points.
        return os.path.join(src_dir, f".{base_name}_tmp_enc.mp4")
    else:
        out_name = f"{base_name}{custom_suffix}.mp4"
        return os.path.join(target_dir, out_name)


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="H264 VAAPI Encoder")
        self.set_default_size(900, 640)
        self.set_border_width(0)
        self.connect("delete-event", self._on_close)

        self._encoder = Encoder()
        self._queue: list[str] = []   # paths in order
        self._current_index: int = -1
        self._encoding_active = False
        # {path: (audio_list, subtitle_list)} – mutable dicts with "enabled" key
        self._file_streams: dict[str, tuple[list, list]] = {}

        self._build_ui()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(vbox)

        # ---- Toolbar ---------------------------------------------------
        toolbar = Gtk.Toolbar()
        toolbar.get_style_context().add_class(Gtk.STYLE_CLASS_PRIMARY_TOOLBAR)
        vbox.pack_start(toolbar, False, False, 0)

        btn_add = Gtk.ToolButton()
        btn_add.set_label("Dateien hinzufügen")
        btn_add.set_icon_name("document-open")
        btn_add.connect("clicked", self._on_add_files)
        toolbar.insert(btn_add, -1)

        btn_remove = Gtk.ToolButton()
        btn_remove.set_label("Entfernen")
        btn_remove.set_icon_name("list-remove")
        btn_remove.connect("clicked", self._on_remove_selected)
        toolbar.insert(btn_remove, -1)

        sep = Gtk.SeparatorToolItem()
        sep.set_expand(True)
        sep.set_draw(False)
        toolbar.insert(sep, -1)

        self._btn_encode = Gtk.ToolButton()
        self._btn_encode.set_label("Kodieren starten")
        self._btn_encode.set_icon_name("media-playback-start")
        self._btn_encode.connect("clicked", self._on_start_encode)
        toolbar.insert(self._btn_encode, -1)

        self._btn_cancel = Gtk.ToolButton()
        self._btn_cancel.set_label("Abbrechen")
        self._btn_cancel.set_icon_name("process-stop")
        self._btn_cancel.set_sensitive(False)
        self._btn_cancel.connect("clicked", self._on_cancel)
        toolbar.insert(self._btn_cancel, -1)

        # ---- Main area: paned (list | settings) ------------------------
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_border_width(8)
        paned.set_position(520)
        vbox.pack_start(paned, True, True, 0)

        # Left: file list
        paned.pack1(self._build_file_list(), True, True)
        # Right: settings
        paned.pack2(self._build_settings(), False, False)

        # ---- Status bar ------------------------------------------------
        status_box = Gtk.Box(spacing=8)
        status_box.set_border_width(4)
        vbox.pack_start(status_box, False, False, 0)

        self._status_label = Gtk.Label(label="Bereit")
        self._status_label.set_halign(Gtk.Align.START)
        status_box.pack_start(self._status_label, True, True, 0)

        self._global_progress = Gtk.ProgressBar()
        self._global_progress.set_size_request(200, -1)
        status_box.pack_end(self._global_progress, False, False, 0)

    def _build_file_list(self) -> Gtk.Widget:
        frame = Gtk.Frame(label="Eingabedateien")
        frame.set_shadow_type(Gtk.ShadowType.IN)

        # Model: filename, directory, status, progress (0–100), full path,
        #        audio-summary, subtitle-summary
        self._store = Gtk.ListStore(str, str, str, int, str, str, str)

        tv = Gtk.TreeView(model=self._store)
        tv.set_reorderable(True)
        tv.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self._treeview = tv

        def col(title, idx, expand=False):
            cell = Gtk.CellRendererText()
            cell.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
            c = Gtk.TreeViewColumn(title, cell, text=idx)
            c.set_expand(expand)
            c.set_resizable(True)
            tv.append_column(c)

        col("Dateiname",  COL_FILENAME,  expand=True)
        col("Verzeichnis", COL_DIRECTORY, expand=False)
        col("Status",     COL_STATUS)

        # Progress column
        prog_cell = Gtk.CellRendererProgress()
        prog_col = Gtk.TreeViewColumn("Fortschritt", prog_cell, value=COL_PROGRESS)
        prog_col.set_min_width(100)
        tv.append_column(prog_col)

        # Audio tracks column (clickable summary)
        audio_cell = Gtk.CellRendererText()
        audio_cell.set_property("foreground", "#2266cc")
        audio_cell.set_property("underline", Pango.Underline.SINGLE)
        self._col_audio = Gtk.TreeViewColumn("Audiospuren", audio_cell,
                                             text=COL_AUDIO_LABEL)
        self._col_audio.set_min_width(90)
        tv.append_column(self._col_audio)

        # Subtitle tracks column (clickable summary)
        sub_cell = Gtk.CellRendererText()
        sub_cell.set_property("foreground", "#2266cc")
        sub_cell.set_property("underline", Pango.Underline.SINGLE)
        self._col_sub = Gtk.TreeViewColumn("Untertitel", sub_cell,
                                           text=COL_SUB_LABEL)
        self._col_sub.set_min_width(80)
        tv.append_column(self._col_sub)

        tv.connect("button-press-event", self._on_treeview_button_press)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(tv)

        # Drag-and-drop target
        try:
            from gi.repository import Gdk
            tv.drag_dest_set(
                Gtk.DestDefaults.ALL,
                [Gtk.TargetEntry.new("text/uri-list", 0, 0)],
                Gdk.DragAction.COPY,
            )
            tv.connect("drag-data-received", self._on_drag_data)
        except Exception:
            pass

        frame.add(sw)
        return frame

    def _build_settings(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_border_width(4)

        # ---- Output path -----------------------------------------------
        out_frame = Gtk.Frame(label="Ausgabepfad")
        out_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        out_box.set_border_width(8)
        out_frame.add(out_box)
        outer.pack_start(out_frame, False, False, 0)

        # "Use source directory" checkbox
        self._chk_src_dir = Gtk.CheckButton(
            label="Im Quellverzeichnis speichern"
        )
        self._chk_src_dir.set_active(True)
        self._chk_src_dir.connect("toggled", self._on_src_dir_toggled)
        out_box.pack_start(self._chk_src_dir, False, False, 0)

        # Custom output dir row
        dir_row = Gtk.Box(spacing=4)
        self._entry_outdir = Gtk.Entry()
        self._entry_outdir.set_placeholder_text("Ausgabeverzeichnis wählen…")
        self._entry_outdir.set_sensitive(False)
        dir_row.pack_start(self._entry_outdir, True, True, 0)
        btn_browse = Gtk.Button(label="…")
        btn_browse.connect("clicked", self._on_browse_outdir)
        dir_row.pack_start(btn_browse, False, False, 0)
        self._btn_browse_outdir = btn_browse
        self._btn_browse_outdir.set_sensitive(False)
        out_box.pack_start(dir_row, False, False, 0)

        Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)

        # Output naming
        naming_frame = Gtk.Frame(label="Ausgabename")
        naming_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        naming_box.set_border_width(8)
        naming_frame.add(naming_box)
        outer.pack_start(naming_frame, False, False, 0)

        self._radio_new_name = Gtk.RadioButton.new_with_label(
            None, "Neuen Namen verwenden"
        )
        self._radio_replace = Gtk.RadioButton.new_with_label_from_widget(
            self._radio_new_name, "Quelldatei ersetzen (Original löschen)"
        )
        self._radio_new_name.connect("toggled", self._on_naming_toggled)
        naming_box.pack_start(self._radio_new_name, False, False, 0)
        naming_box.pack_start(self._radio_replace, False, False, 0)

        suffix_row = Gtk.Box(spacing=4)
        suffix_row.pack_start(Gtk.Label(label="Suffix:"), False, False, 0)
        self._entry_suffix = Gtk.Entry()
        self._entry_suffix.set_text("_h264")
        self._entry_suffix.set_width_chars(10)
        suffix_row.pack_start(self._entry_suffix, False, False, 0)
        naming_box.pack_start(suffix_row, False, False, 0)
        self._suffix_row = suffix_row

        # ---- Bitrate settings ------------------------------------------
        br_frame = Gtk.Frame(label="Bitrate-Einstellungen")
        br_grid = Gtk.Grid()
        br_grid.set_column_spacing(8)
        br_grid.set_row_spacing(8)
        br_grid.set_border_width(8)
        br_frame.add(br_grid)
        outer.pack_start(br_frame, False, False, 0)

        br_grid.attach(Gtk.Label(label="Video-Bitrate:"), 0, 0, 1, 1)
        self._combo_vbr = Gtk.ComboBoxText()
        for label, _ in VIDEO_BITRATES:
            self._combo_vbr.append_text(label)
        self._combo_vbr.set_active(DEFAULT_VIDEO_IDX)
        br_grid.attach(self._combo_vbr, 1, 0, 1, 1)

        br_grid.attach(Gtk.Label(label="Audio-Bitrate:"), 0, 1, 1, 1)
        self._combo_abr = Gtk.ComboBoxText()
        for label, _ in AUDIO_BITRATES:
            self._combo_abr.append_text(label)
        self._combo_abr.set_active(DEFAULT_AUDIO_IDX)
        br_grid.attach(self._combo_abr, 1, 1, 1, 1)

        lbl_res = Gtk.Label(label="Auflösung:")
        lbl_res.set_halign(Gtk.Align.START)
        br_grid.attach(lbl_res, 0, 2, 1, 1)
        self._combo_res = Gtk.ComboBoxText()
        for label, _ in RESOLUTIONS:
            self._combo_res.append_text(label)
        self._combo_res.set_active(DEFAULT_RES_IDX)
        br_grid.attach(self._combo_res, 1, 2, 1, 1)

        res_note = Gtk.Label()
        res_note.set_markup(
            '<small><i>Nicht-16:9-Quellen werden automatisch\n'
            'im Originalseitenverhältnis skaliert.</i></small>'
        )
        res_note.set_halign(Gtk.Align.START)
        br_grid.attach(res_note, 0, 3, 2, 1)

        # High-FPS note
        note = Gtk.Label()
        note.set_markup(
            f'<small><i>Hinweis: Bei ≥{HIGH_FPS_THRESHOLD} fps wird die\n'
            f'Video-Bitrate automatisch verdoppelt.</i></small>'
        )
        note.set_halign(Gtk.Align.START)
        br_grid.attach(note, 0, 4, 2, 1)

        outer.pack_end(Gtk.Box(), True, True, 0)  # spacer
        return outer

    # ------------------------------------------------------------------
    # Signal Handlers
    # ------------------------------------------------------------------

    def _on_close(self, *_):
        if self._encoding_active:
            self._encoder.cancel()
        Gtk.main_quit()

    def _on_add_files(self, *_):
        dialog = Gtk.FileChooserDialog(
            title="Videodateien auswählen",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN,   Gtk.ResponseType.OK,
        )
        dialog.set_select_multiple(True)

        filt = Gtk.FileFilter()
        filt.set_name("Videodateien")
        for ext in ["*.mp4", "*.mkv", "*.avi", "*.mov", "*.wmv",
                    "*.flv", "*.webm", "*.m4v", "*.ts", "*.mts"]:
            filt.add_pattern(ext)
        dialog.add_filter(filt)

        all_filt = Gtk.FileFilter()
        all_filt.set_name("Alle Dateien")
        all_filt.add_pattern("*")
        dialog.add_filter(all_filt)

        if dialog.run() == Gtk.ResponseType.OK:
            for path in dialog.get_filenames():
                self._add_file(path)
        dialog.destroy()

    def _on_remove_selected(self, *_):
        sel = self._treeview.get_selection()
        model, paths = sel.get_selected_rows()
        # Remove in reverse order to keep iters valid
        for path in reversed(paths):
            it = model.get_iter(path)
            full = model.get_value(it, COL_FULLPATH)
            if full in self._queue:
                self._queue.remove(full)
            self._file_streams.pop(full, None)
            model.remove(it)

    def _on_src_dir_toggled(self, btn):
        active = btn.get_active()
        self._entry_outdir.set_sensitive(not active)
        self._btn_browse_outdir.set_sensitive(not active)

    def _on_naming_toggled(self, btn):
        use_new = self._radio_new_name.get_active()
        self._suffix_row.set_sensitive(use_new)

    def _on_browse_outdir(self, *_):
        dialog = Gtk.FileChooserDialog(
            title="Ausgabeverzeichnis wählen",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN,   Gtk.ResponseType.OK,
        )
        if dialog.run() == Gtk.ResponseType.OK:
            self._entry_outdir.set_text(dialog.get_filename())
        dialog.destroy()

    def _on_drag_data(self, widget, drag_context, x, y, data, info, time):
        from gi.repository import GLib
        for uri in data.get_uris():
            uri = uri.strip()
            if not uri:
                continue
            try:
                # GLib properly decodes percent-encoded URIs (e.g. spaces → %20)
                path, _ = GLib.filename_from_uri(uri)
            except Exception:
                path = unquote(uri.removeprefix("file://"))
            if os.path.isfile(path):
                self._add_file(path)
        # Signal the drag source that the drop was handled successfully.
        Gtk.drag_finish(drag_context, True, False, time)

    def _on_start_encode(self, *_):
        if not self._queue:
            self._show_error("Keine Dateien in der Liste.")
            return

        use_src_dir       = self._chk_src_dir.get_active()
        replace_orig      = self._radio_replace.get_active()
        output_dir        = self._entry_outdir.get_text().strip()
        custom_suffix     = self._entry_suffix.get_text().strip()
        video_bitrate     = VIDEO_BITRATES[self._combo_vbr.get_active()][1]
        audio_bitrate     = AUDIO_BITRATES[self._combo_abr.get_active()][1]
        resolution_height = RESOLUTIONS[self._combo_res.get_active()][1]

        if not use_src_dir and not output_dir:
            self._show_error("Bitte ein Ausgabeverzeichnis auswählen.")
            return

        self._jobs: list[EncodeJob] = []
        for path in self._queue:
            out_path = make_output_path(
                input_path=path,
                output_dir=output_dir,
                use_source_dir=use_src_dir,
                replace_original=replace_orig,
                custom_suffix=custom_suffix,
            )
            audio_streams, sub_streams = self._file_streams.get(path, ([], []))
            sel_audio = [s["rel_idx"] for s in audio_streams if s["enabled"]]
            sel_subs   = [s["rel_idx"] for s in sub_streams  if s["enabled"]]
            # Use explicit mapping only if stream info was available
            sel_audio_arg = sel_audio if audio_streams else None
            sel_subs_arg  = sel_subs  if sub_streams  else None
            self._jobs.append(
                EncodeJob(
                    input_path=path,
                    output_path=out_path,
                    video_bitrate=video_bitrate,
                    audio_bitrate=audio_bitrate,
                    replace_original=replace_orig,
                    resolution_height=resolution_height,
                    selected_audio=sel_audio_arg,
                    selected_subtitles=sel_subs_arg,
                )
            )

        self._encoding_active = True
        self._btn_encode.set_sensitive(False)
        self._btn_cancel.set_sensitive(True)
        self._current_index = 0
        self._encode_next()

    def _on_cancel(self, *_):
        self._encoder.cancel()
        self._btn_cancel.set_sensitive(False)
        self._status_label.set_text("Wird abgebrochen…")

    # ------------------------------------------------------------------
    # Encoding Logic
    # ------------------------------------------------------------------

    def _encode_next(self):
        if self._current_index >= len(self._jobs):
            self._encoding_done()
            return

        job  = self._jobs[self._current_index]
        path = job.input_path

        # Find row in store
        row_iter = self._find_row(path)
        if row_iter:
            self._store.set_value(row_iter, COL_STATUS, STATUS_ENCODING)
            self._store.set_value(row_iter, COL_PROGRESS, 0)

        fps = get_fps(path)
        fps_note = f", HFR {fps:.1f} fps → Bitrate x2" if fps >= HIGH_FPS_THRESHOLD else ""

        target_h = job.resolution_height
        if target_h is not None:
            src_w, src_h = get_video_dimensions(path)
            out_w, out_h = compute_output_dimensions(src_w, src_h, target_h)
            res_note = f", {out_w}×{out_h}"
        else:
            res_note = ""

        self._status_label.set_text(
            f"Kodiere {self._current_index + 1}/{len(self._jobs)}: "
            f"{os.path.basename(path)}{res_note}{fps_note}"
        )

        def on_progress(frac):
            GLib.idle_add(self._update_progress, path, frac)

        def on_done(success, msg):
            GLib.idle_add(self._job_done, path, success, msg)

        self._encoder.encode(job, on_progress, on_done)

    def _update_progress(self, path: str, frac: float):
        row_iter = self._find_row(path)
        if row_iter:
            self._store.set_value(row_iter, COL_PROGRESS, int(frac * 100))
        # Global progress
        total = len(self._jobs)
        done  = self._current_index
        global_frac = (done + frac) / total if total else 0
        self._global_progress.set_fraction(global_frac)

    def _job_done(self, path: str, success: bool, msg: str):
        row_iter = self._find_row(path)
        if row_iter:
            if success:
                self._store.set_value(row_iter, COL_STATUS,   STATUS_DONE)
                self._store.set_value(row_iter, COL_PROGRESS, 100)
            elif msg == "Abgebrochen":
                self._store.set_value(row_iter, COL_STATUS,   STATUS_CANCELLED)
            else:
                self._store.set_value(row_iter, COL_STATUS,   f"{STATUS_ERROR}: {msg}")

        if not success and msg != "Abgebrochen":
            # Continue with next file even on error
            pass

        self._current_index += 1
        if self._encoding_active:
            self._encode_next()

    def _encoding_done(self):
        self._encoding_active = False
        self._btn_encode.set_sensitive(True)
        self._btn_cancel.set_sensitive(False)
        self._global_progress.set_fraction(1.0)
        self._status_label.set_text("Alle Aufgaben abgeschlossen.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _add_file(self, path: str):
        if path in self._queue:
            return
        self._queue.append(path)
        # Placeholder until the background probe completes.
        self._file_streams[path] = ([], [])
        row_ref = Gtk.TreeRowReference.new(
            self._store,
            self._store.get_path(
                self._store.append([
                    os.path.basename(path),
                    os.path.dirname(path),
                    STATUS_PENDING,
                    0,
                    path,
                    "Lädt…",
                    "Lädt…",
                ])
            ),
        )

        # Probe streams off the main thread so D&D / UI never blocks.
        def _probe():
            audio, subs = get_streams(path)

            def _apply():
                self._file_streams[path] = (audio, subs)
                tp = row_ref.get_path()
                if tp:
                    it = self._store.get_iter(tp)
                    self._store.set_value(it, COL_AUDIO_LABEL,
                                         self._stream_summary(audio))
                    self._store.set_value(it, COL_SUB_LABEL,
                                         self._stream_summary(subs))
                return False  # run once

            GLib.idle_add(_apply)

        threading.Thread(target=_probe, daemon=True).start()

    def _find_row(self, path: str):
        it = self._store.get_iter_first()
        while it:
            if self._store.get_value(it, COL_FULLPATH) == path:
                return it
            it = self._store.iter_next(it)
        return None

    # ------------------------------------------------------------------
    # Stream selection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stream_summary(streams: list[dict]) -> str:
        """Return a one-line summary for the column cell."""
        if not streams:
            return "–"
        total   = len(streams)
        enabled = sum(1 for s in streams if s["enabled"])
        if enabled == 0:
            return f"Keine ({total})"
        if enabled == total:
            return f"Alle ({total})"
        return f"{enabled}/{total} aktiv"

    @staticmethod
    def _stream_label(stream: dict, stype: str) -> str:
        """Human-readable label for a single stream checkbox."""
        lang  = stream.get("language", "")
        title = stream.get("title", "")
        codec = stream.get("codec", "?")
        name  = title or lang or "unbekannt"
        idx   = stream["rel_idx"] + 1
        if stype == "audio":
            ch = stream.get("channels", 0)
            layout = stream.get("channel_layout", "")
            ch_str = layout if layout else (f"{ch}ch" if ch else "")
            return f"Spur {idx}: {name}  [{codec}, {ch_str}]"
        else:
            return f"Spur {idx}: {name}  [{codec}]"

    def _update_stream_summary(self, file_path: str, tree_path):
        """Recalculate and write summary strings back to the ListStore."""
        it = self._store.get_iter(tree_path)
        if not it:
            return
        audio, subs = self._file_streams.get(file_path, ([], []))
        self._store.set_value(it, COL_AUDIO_LABEL, self._stream_summary(audio))
        self._store.set_value(it, COL_SUB_LABEL,   self._stream_summary(subs))

    def _on_treeview_button_press(self, widget, event):
        """Open a stream-selection popover when the audio/subtitle column is clicked."""
        if event.button != 1:
            return False
        result = widget.get_path_at_pos(int(event.x), int(event.y))
        if result is None:
            return False
        tree_path, column, _cx, _cy = result
        if column is self._col_audio:
            self._show_stream_popover(widget, tree_path, "audio")
            return True
        if column is self._col_sub:
            self._show_stream_popover(widget, tree_path, "subtitle")
            return True
        return False

    def _show_stream_popover(self, treeview, tree_path, stype: str):
        it        = self._store.get_iter(tree_path)
        file_path = self._store.get_value(it, COL_FULLPATH)
        audio, subs = self._file_streams.get(file_path, ([], []))
        streams   = audio if stype == "audio" else subs
        col       = self._col_audio if stype == "audio" else self._col_sub
        title_str = "Audiospuren" if stype == "audio" else "Untertitel"

        popover = Gtk.Popover()
        popover.set_relative_to(treeview)
        cell_rect = treeview.get_cell_area(tree_path, col)
        popover.set_pointing_to(cell_rect)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.set_border_width(10)

        hdr = Gtk.Label()
        hdr.set_markup(f"<b>{title_str}</b>")
        hdr.set_halign(Gtk.Align.START)
        outer.pack_start(hdr, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        outer.pack_start(sep, False, False, 2)

        if not streams:
            lbl = Gtk.Label(label="Keine Spuren gefunden.")
            lbl.set_sensitive(False)
            outer.pack_start(lbl, False, False, 0)
        else:
            for stream in streams:
                label = self._stream_label(stream, stype)
                chk   = Gtk.CheckButton(label=label)
                chk.set_active(stream["enabled"])

                def _on_toggle(btn, s=stream, fp=file_path, tp=tree_path):
                    s["enabled"] = btn.get_active()
                    self._update_stream_summary(fp, tp)

                chk.connect("toggled", _on_toggle)
                outer.pack_start(chk, False, False, 0)

        popover.add(outer)
        popover.show_all()
        popover.popup()

    def _show_error(self, message: str):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=message,
        )
        dlg.run()
        dlg.destroy()


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main():
    win = MainWindow()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
