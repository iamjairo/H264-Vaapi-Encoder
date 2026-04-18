#!/usr/bin/env python3
"""H264 VAAPI Encoder – GTK3 GUI"""

import json
import os
import threading
import gi
from urllib.parse import unquote

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, GObject, Pango
import subprocess
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf
from typing import Optional

from encoder import (
    Encoder, EncodeJob,
    get_fps, get_video_dimensions, compute_output_dimensions,
    get_file_metadata, scan_folder, HIGH_FPS_THRESHOLD,
    VIDEO_EXTENSIONS,
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
    ("Original (beibehalten)", None),   # stream-copy audio
    ("64 kbps",  "64k"),
    ("96 kbps",  "96k"),
    ("128 kbps", "128k"),
    ("192 kbps", "192k"),
    ("256 kbps", "256k"),
    ("320 kbps", "320k"),
]

DEFAULT_VIDEO_IDX = 3   # 4000 kbps
DEFAULT_AUDIO_IDX = 0   # Original (beibehalten)

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
COL_AUDIO_LABEL = 5
COL_SUB_LABEL   = 6
COL_RESOLUTION  = 7   # e.g. "1920×1080"
COL_VID_BITRATE = 8   # e.g. "4.3 Mbps"
COL_AUD_BITRATE = 9   # e.g. "128 kbps"
COL_FPS         = 10  # e.g. "59.94"
COL_DURATION    = 11  # e.g. "1:23:45"
COL_STOP_MARKER = 12  # "⏹" when stop-after is set, "" otherwise


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_kbps(kbps: int | None) -> str:
    if kbps is None:
        return "–"
    if kbps >= 10_000:
        return f"{kbps / 1000:.1f} Mbps"
    return f"{kbps:,} kbps".replace(",", "\u202f")   # narrow no-break space


def _fmt_resolution(w: int, h: int) -> str:
    return f"{w}×{h}" if w and h else "–"


def _fmt_fps(fps: float) -> str:
    if fps <= 0:
        return "–"
    # Show decimal only when it's not a whole number
    return f"{fps:.2f}".rstrip("0").rstrip(".") + " fps"


def _fmt_duration(secs: float) -> str:
    if secs <= 0:
        return "–"
    total = int(secs)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

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
    keep_name: bool,
    custom_suffix: str,
) -> str:
    """Compute the output file path from settings."""
    src_dir   = os.path.dirname(input_path)
    base, ext = os.path.splitext(input_path)
    base_name = os.path.basename(base)
    orig_name = os.path.basename(input_path)   # full original filename

    target_dir = src_dir if use_source_dir else output_dir

    if replace_original:
        # Temp file in the source directory so os.replace works atomically.
        return os.path.join(src_dir, f".{base_name}_tmp_enc.mp4")
    if keep_name:
        # Same filename, different directory — no conflict with the source.
        return os.path.join(target_dir, orig_name)
    out_name = f"{base_name}{custom_suffix}.mp4"
    return os.path.join(target_dir, out_name)


# ---------------------------------------------------------------------------
# Queue persistence
# ---------------------------------------------------------------------------

QUEUE_FILE = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "h264-vaapi-encoder",
    "queue.txt",
)


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="H264 VAAPI Encoder")
        self.set_default_size(1200, 800)
        self.set_border_width(0)
        self.connect("delete-event", self._on_close)

        self._encoder = Encoder()
        self._queue: list[str] = []   # paths in order
        self._current_index: int = -1
        self._encoding_active = False
        self._file_streams: dict[str, tuple[list, list]] = {}
        self._completed: set[str] = set()   # successfully encoded paths
        # Per-file setting overrides.  Keys: video_bitrate, audio_bitrate,
        # resolution_height, fps_limit, rotation.  Absent key = use global.
        self._file_settings: dict[str, dict] = {}
        self._preview_path: Optional[str] = None    # currently previewed path
        self._stop_after_path: Optional[str] = None # stop encoding queue after this file

        self._build_ui()
        self._restore_queue()

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

        btn_scan = Gtk.ToolButton()
        btn_scan.set_label("Ordner scannen")
        btn_scan.set_icon_name("folder-saved-search")
        btn_scan.connect("clicked", self._on_scan_folder)
        toolbar.insert(btn_scan, -1)

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
        # Right: settings + preview (vertical split)
        settings_scroll = Gtk.ScrolledWindow()
        settings_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        settings_scroll.add(self._build_settings())

        right_pane = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        right_pane.pack1(settings_scroll, True, True)
        right_pane.pack2(self._build_preview_panel(), False, False)
        right_pane.set_position(560)
        paned.pack2(right_pane, False, False)

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

        # columns: filename, directory, status, progress, full-path,
        #          audio-label(hidden), sub-label(hidden),
        #          resolution, vid-bitrate, aud-bitrate, fps, duration
        self._store = Gtk.ListStore(str, str, str, int, str,
                                    str, str, str, str, str,
                                    str, str, str)

        tv = Gtk.TreeView(model=self._store)
        tv.set_reorderable(True)
        tv.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self._treeview = tv

        def col(title, idx):
            cell = Gtk.CellRendererText()
            cell.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
            c = Gtk.TreeViewColumn(title, cell, text=idx)
            c.set_expand(True)
            c.set_resizable(True)
            tv.append_column(c)

        # Stop-after marker column (very narrow, no header text)
        stop_cell = Gtk.CellRendererText()
        stop_cell.set_property("foreground", "#e74c3c")
        stop_col = Gtk.TreeViewColumn("", stop_cell, text=COL_STOP_MARKER)
        stop_col.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        stop_col.set_resizable(False)
        tv.append_column(stop_col)

        col("Dateiname",   COL_FILENAME)
        col("Verzeichnis", COL_DIRECTORY)
        col("Status",      COL_STATUS)

        # Progress column
        prog_cell = Gtk.CellRendererProgress()
        prog_col = Gtk.TreeViewColumn("Fortschritt", prog_cell, value=COL_PROGRESS)
        prog_col.set_expand(True)
        prog_col.set_resizable(True)
        tv.append_column(prog_col)

        def _auto_col(title, col_idx):
            """Metadata column: auto-sized to content, no extra expand."""
            cell = Gtk.CellRendererText()
            cell.set_property("xalign", 1.0)
            c = Gtk.TreeViewColumn(title, cell, text=col_idx)
            c.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
            c.set_resizable(True)
            tv.append_column(c)

        _auto_col("Auflösung",    COL_RESOLUTION)
        _auto_col("Video-Bitrate", COL_VID_BITRATE)
        _auto_col("Audio-Bitrate", COL_AUD_BITRATE)
        _auto_col("FPS",           COL_FPS)
        _auto_col("Länge",         COL_DURATION)

        tv.connect("button-press-event", self._on_treeview_button_press)
        tv.connect("row-activated",      self._on_row_activated)
        tv.get_selection().connect("changed", self._on_selection_changed)

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
        self._radio_same_name = Gtk.RadioButton.new_with_label_from_widget(
            self._radio_new_name, "Gleichen Dateinamen behalten"
        )
        self._radio_replace = Gtk.RadioButton.new_with_label_from_widget(
            self._radio_new_name, "Quelldatei ersetzen (Original löschen)"
        )
        # "Same name" only makes sense when output goes to a different dir;
        # "Replace" only makes sense when output stays in the source dir.
        # set_no_show_all prevents show_all() from overriding visibility;
        # we then set the correct initial state explicitly (source-dir is
        # the default → replace visible, same-name hidden).
        self._radio_same_name.set_no_show_all(True)
        self._radio_same_name.set_visible(False)
        self._radio_replace.set_no_show_all(True)
        self._radio_replace.set_visible(True)

        self._radio_new_name.connect("toggled", self._on_naming_toggled)
        self._radio_same_name.connect("toggled", self._on_naming_toggled)
        naming_box.pack_start(self._radio_new_name, False, False, 0)
        naming_box.pack_start(self._radio_same_name, False, False, 0)
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

        # FPS option
        self._chk_fps_limit = Gtk.CheckButton(
            label=f"HFR-Videos (>{int(HIGH_FPS_THRESHOLD)} fps) auf 30 fps begrenzen")
        br_grid.attach(self._chk_fps_limit, 0, 4, 2, 1)

        # High-FPS note
        note = Gtk.Label()
        note.set_markup(
            f'<small><i>Ohne Begrenzung: ≥{int(HIGH_FPS_THRESHOLD)} fps\n'
            f'→ Video-Bitrate wird verdoppelt.</i></small>'
        )
        note.set_halign(Gtk.Align.START)
        br_grid.attach(note, 0, 5, 2, 1)

        # ---- Post-encoding action --------------------------------------
        action_frame = Gtk.Frame(label="Aktion nach Kodierung")
        action_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        action_box.set_border_width(8)
        action_frame.add(action_box)

        self._radio_action_nothing = Gtk.RadioButton.new_with_label(
            None, "Nichts tun")
        self._radio_action_quit = Gtk.RadioButton.new_with_label_from_widget(
            self._radio_action_nothing, "Programm schließen")
        self._radio_action_shutdown = Gtk.RadioButton.new_with_label_from_widget(
            self._radio_action_nothing, "Computer herunterfahren")

        action_box.pack_start(self._radio_action_nothing,  False, False, 0)
        action_box.pack_start(self._radio_action_quit,     False, False, 0)
        action_box.pack_start(self._radio_action_shutdown, False, False, 0)

        outer.pack_start(action_frame, False, False, 0)
        outer.pack_end(Gtk.Box(), True, True, 0)  # spacer
        return outer

    def _build_preview_panel(self) -> Gtk.Widget:
        frame = Gtk.Frame(label="Vorschau")
        frame.set_shadow_type(Gtk.ShadowType.IN)
        frame.set_size_request(-1, 230)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        box.set_vexpand(True)

        self._preview_image = Gtk.Image()
        self._preview_image.set_no_show_all(True)

        self._preview_label = Gtk.Label()
        self._preview_label.set_markup("<i>Kein Video ausgewählt</i>")
        self._preview_label.set_sensitive(False)

        box.pack_start(self._preview_image, False, False, 0)
        box.pack_start(self._preview_label, False, False, 0)

        frame.add(box)
        return frame

    # ------------------------------------------------------------------
    # Signal Handlers
    # ------------------------------------------------------------------

    def _on_close(self, *_):
        if self._encoding_active:
            self._encoder.cancel()
        Gtk.main_quit()

    def _on_scan_folder(self, *_):
        dlg = ScanDialog(parent=self)
        dlg.connect("files-selected", self._on_scan_files_selected)
        dlg.show_all()

    def _on_scan_files_selected(self, _dlg, paths: list):
        for path in paths:
            self._add_file(path)
        n = len(paths)
        self._status_label.set_text(
            f"{n} Datei{'en' if n != 1 else ''} aus Scan zur Liste hinzugefügt."
        )

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
            self._file_settings.pop(full, None)
            self._completed.discard(full)
            model.remove(it)
        self._save_queue()

    def _on_src_dir_toggled(self, btn):
        use_src = btn.get_active()
        self._entry_outdir.set_sensitive(not use_src)
        self._btn_browse_outdir.set_sensitive(not use_src)

        # Source-dir mode: "Quelldatei ersetzen" available, "Gleichen Namen"
        # hidden (would be identical to "ersetzen" in the same folder).
        # Custom-dir mode: "Gleichen Namen" available, "ersetzen" greyed out.
        self._radio_replace.set_sensitive(use_src)
        self._radio_replace.set_visible(use_src)
        self._radio_same_name.set_sensitive(not use_src)
        self._radio_same_name.set_visible(not use_src)

        # If the now-hidden option was selected, fall back to "Neuen Namen".
        if use_src and self._radio_same_name.get_active():
            self._radio_new_name.set_active(True)
        if not use_src and self._radio_replace.get_active():
            self._radio_new_name.set_active(True)

    def _on_naming_toggled(self, btn):
        # Suffix only relevant when "Neuen Namen verwenden" is active.
        self._suffix_row.set_sensitive(self._radio_new_name.get_active())

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
                path, _ = GLib.filename_from_uri(uri)
            except Exception:
                path = unquote(uri.removeprefix("file://"))
            if os.path.isfile(path):
                self._add_file(path)
            elif os.path.isdir(path):
                for root, _dirs, files in os.walk(path):
                    for f in sorted(files):
                        if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                            self._add_file(os.path.join(root, f))
        Gtk.drag_finish(drag_context, True, False, time)

    def _on_start_encode(self, *_):
        if not self._queue:
            self._show_error("Keine Dateien in der Liste.")
            return

        use_src_dir          = self._chk_src_dir.get_active()
        replace_orig         = self._radio_replace.get_active()
        keep_name            = self._radio_same_name.get_active()
        output_dir           = self._entry_outdir.get_text().strip()
        custom_suffix        = self._entry_suffix.get_text().strip()
        global_video_bitrate = VIDEO_BITRATES[self._combo_vbr.get_active()][1]
        global_audio_bitrate = AUDIO_BITRATES[self._combo_abr.get_active()][1]
        global_resolution    = RESOLUTIONS[self._combo_res.get_active()][1]
        global_fps_limit     = 30 if self._chk_fps_limit.get_active() else None

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
                keep_name=keep_name,
                custom_suffix=custom_suffix,
            )
            fs = self._file_settings.get(path, {})
            # Per-file settings override global; absent key → use global.
            video_bitrate     = fs["video_bitrate"]     if "video_bitrate"     in fs else global_video_bitrate
            audio_bitrate     = fs["audio_bitrate"]     if "audio_bitrate"     in fs else global_audio_bitrate
            resolution_height = fs["resolution_height"] if "resolution_height" in fs else global_resolution
            fps_limit         = fs["fps_limit"]         if "fps_limit"         in fs else global_fps_limit
            rotation          = fs.get("rotation", 0)

            audio_streams, sub_streams = self._file_streams.get(path, ([], []))
            sel_audio = [s["rel_idx"] for s in audio_streams if s["enabled"]]
            sel_subs  = [s["rel_idx"] for s in sub_streams  if s["enabled"]]
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
                    rotation=rotation,
                    fps_limit=fps_limit,
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
                self._completed.add(path)
                self._save_queue()   # remove finished file from persistent list
            elif msg == "Abgebrochen":
                self._store.set_value(row_iter, COL_STATUS,   STATUS_CANCELLED)
            else:
                # Show first line in the column (space is limited) and open a
                # dialog with the full output so the user can read the details.
                first_line = msg.splitlines()[0]
                self._store.set_value(row_iter, COL_STATUS,
                                      f"{STATUS_ERROR}: {first_line}")
                self._show_error_detail(os.path.basename(path), msg)

        self._current_index += 1
        if self._encoding_active:
            if success and self._stop_after_path == path:
                self._set_stop_after(None)
                self._encoding_done()
            else:
                self._encode_next()

    def _encoding_done(self):
        self._encoding_active = False
        self._btn_encode.set_sensitive(True)
        self._btn_cancel.set_sensitive(False)
        self._global_progress.set_fraction(1.0)
        self._status_label.set_text("Alle Aufgaben abgeschlossen.")
        if self._stop_after_path:
            self._set_stop_after(None)

        if self._radio_action_quit.get_active():
            Gtk.main_quit()
        elif self._radio_action_shutdown.get_active():
            subprocess.Popen(["systemctl", "poweroff"])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _add_file(self, path: str):
        if path in self._queue:
            return
        self._queue.append(path)
        self._file_streams[path] = ([], [])
        self._save_queue()   # persist immediately so a crash loses nothing
        row_ref = Gtk.TreeRowReference.new(
            self._store,
            self._store.get_path(
                self._store.append([
                    os.path.basename(path),
                    os.path.dirname(path),
                    STATUS_PENDING,
                    0,
                    path,
                    "Lädt…", "Lädt…",         # audio / sub labels
                    "–", "–", "–", "–", "–",  # resolution / vid-br / aud-br / fps / duration
                    "",                       # stop marker
                ])
            ),
        )

        # Single ffprobe call in background — fills streams AND tech info.
        def _probe():
            meta = get_file_metadata(path)

            def _apply():
                self._file_streams[path] = (meta["audio"], meta["subtitles"])
                tp = row_ref.get_path()
                if tp:
                    it = self._store.get_iter(tp)
                    self._store.set_value(it, COL_AUDIO_LABEL,
                                         self._stream_summary(meta["audio"]))
                    self._store.set_value(it, COL_SUB_LABEL,
                                         self._stream_summary(meta["subtitles"]))
                    self._store.set_value(it, COL_RESOLUTION,
                                         _fmt_resolution(meta["width"], meta["height"]))
                    self._store.set_value(it, COL_VID_BITRATE,
                                         _fmt_kbps(meta["video_kbps"]))
                    self._store.set_value(it, COL_AUD_BITRATE,
                                         _fmt_kbps(meta["audio_kbps"]))
                    self._store.set_value(it, COL_FPS,
                                         _fmt_fps(meta["fps"]))
                    self._store.set_value(it, COL_DURATION,
                                         _fmt_duration(meta["duration_secs"]))
                return False

            GLib.idle_add(_apply)

        threading.Thread(target=_probe, daemon=True).start()

    # ------------------------------------------------------------------
    # Queue persistence
    # ------------------------------------------------------------------

    def _save_queue(self):
        """Write pending queue + per-file settings to QUEUE_FILE as JSON."""
        try:
            os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
            data = {
                "queue": [
                    {
                        "path": p,
                        "settings": self._file_settings.get(p, {}),
                    }
                    for p in self._queue
                    if p not in self._completed
                ]
            }
            with open(QUEUE_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
        except Exception as exc:
            print(f"[queue] Fehler beim Speichern: {exc}", flush=True)

    @staticmethod
    def _load_queue() -> list[tuple[str, dict]]:
        """Return (path, settings) pairs from QUEUE_FILE that still exist."""
        try:
            with open(QUEUE_FILE, encoding="utf-8") as fh:
                raw = fh.read()
            try:
                data = json.loads(raw)
                return [
                    (e["path"], e.get("settings", {}))
                    for e in data.get("queue", [])
                    if os.path.isfile(e.get("path", ""))
                ]
            except (json.JSONDecodeError, KeyError):
                # Legacy plain-text format (one path per line)
                return [
                    (line.strip(), {})
                    for line in raw.splitlines()
                    if line.strip() and os.path.isfile(line.strip())
                ]
        except FileNotFoundError:
            return []
        except Exception as exc:
            print(f"[queue] Fehler beim Laden: {exc}", flush=True)
            return []

    def _restore_queue(self):
        """Add persisted pending paths back into the queue on startup."""
        entries = self._load_queue()
        if not entries:
            return
        for path, settings in entries:
            self._file_settings[path] = settings
            self._add_file(path)
        self._status_label.set_text(
            f"{len(entries)} Datei(en) aus vorheriger Sitzung wiederhergestellt."
        )

    def _set_stop_after(self, path: Optional[str]):
        """Set (or clear) the stop-after marker. Pass None to clear."""
        # Clear old marker
        if self._stop_after_path:
            it = self._find_row(self._stop_after_path)
            if it:
                self._store.set_value(it, COL_STOP_MARKER, "")
        self._stop_after_path = path
        # Set new marker
        if path:
            it = self._find_row(path)
            if it:
                self._store.set_value(it, COL_STOP_MARKER, "⏹")

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

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def _on_selection_changed(self, selection):
        model, paths = selection.get_selected_rows()
        if len(paths) == 1:
            it = model.get_iter(paths[0])
            path = model.get_value(it, COL_FULLPATH)
            self._show_preview(path)
        else:
            self._clear_preview()

    def _show_preview(self, path: str):
        if self._preview_path == path:
            return
        self._preview_path = path
        self._preview_label.show()
        self._preview_image.hide()
        self._preview_label.set_markup("<i>Lädt Vorschau…</i>")

        def _load():
            pixbuf = self._extract_thumbnail(path, max_w=580, max_h=220)
            def _apply():
                if self._preview_path != path:
                    return False   # selection changed while loading
                if pixbuf:
                    self._preview_image.set_from_pixbuf(pixbuf)
                    self._preview_image.show()
                    self._preview_label.hide()
                else:
                    self._preview_label.set_markup("<i>Vorschau nicht verfügbar</i>")
                return False
            GLib.idle_add(_apply)

        threading.Thread(target=_load, daemon=True).start()

    def _clear_preview(self):
        self._preview_path = None
        self._preview_image.hide()
        self._preview_label.show()
        self._preview_label.set_markup("<i>Kein Video ausgewählt</i>")

    @staticmethod
    def _extract_thumbnail(path: str, max_w: int = 580, max_h: int = 220):
        """Return a GdkPixbuf thumbnail or None on failure."""
        try:
            result = subprocess.run(
                ["ffmpeg", "-ss", "00:00:05", "-i", path,
                 "-vframes", "1",
                 "-vf", f"scale={max_w}:{max_h}:force_original_aspect_ratio=decrease",
                 "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                capture_output=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout:
                loader = GdkPixbuf.PixbufLoader.new_with_type("png")
                loader.write(result.stdout)
                loader.close()
                return loader.get_pixbuf()
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Play / Context menu
    # ------------------------------------------------------------------

    def _on_row_activated(self, treeview, tree_path, column):
        """Double-click: open file with default video player."""
        it = self._store.get_iter(tree_path)
        path = self._store.get_value(it, COL_FULLPATH)
        self._play_file(path)

    @staticmethod
    def _play_file(path: str):
        try:
            subprocess.Popen(["xdg-open", path],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _on_treeview_button_press(self, widget, event):
        """Right-click → context menu."""
        from gi.repository import Gdk
        if event.button != 3:
            return False
        result = widget.get_path_at_pos(int(event.x), int(event.y))
        if result is None:
            return False
        tree_path, _col, _cx, _cy = result
        # Ensure the right-clicked row is selected
        sel = widget.get_selection()
        if not sel.path_is_selected(tree_path):
            sel.unselect_all()
            sel.select_path(tree_path)
        it = self._store.get_iter(tree_path)
        file_path = self._store.get_value(it, COL_FULLPATH)
        self._show_context_menu(widget, event, tree_path, file_path)
        return True

    def _show_context_menu(self, treeview, event, tree_path, file_path: str):
        menu = Gtk.Menu()
        menu.attach_to_widget(treeview, None)
        fs = self._file_settings.get(file_path, {})

        # ---- Play -------------------------------------------------------
        item_play = Gtk.MenuItem(label="▶  Abspielen")
        item_play.connect("activate", lambda _: self._play_file(file_path))
        menu.append(item_play)

        menu.append(Gtk.SeparatorMenuItem())

        # ---- Audio tracks -----------------------------------------------
        audio, subs = self._file_streams.get(file_path, ([], []))

        audio_item = Gtk.MenuItem(label="Audiospuren")
        if audio:
            audio_sub = Gtk.Menu()
            for stream in audio:
                label = self._stream_label(stream, "audio")
                chk = Gtk.CheckMenuItem(label=label)
                chk.set_active(stream["enabled"])
                chk.connect("toggled",
                            lambda btn, s=stream, fp=file_path, tp=tree_path:
                            self._on_stream_toggle(btn, s, fp, tp))
                audio_sub.append(chk)
            audio_item.set_submenu(audio_sub)
        else:
            audio_item.set_sensitive(False)
        menu.append(audio_item)

        # ---- Subtitle tracks --------------------------------------------
        sub_item = Gtk.MenuItem(label="Untertitel")
        if subs:
            sub_menu = Gtk.Menu()
            for stream in subs:
                label = self._stream_label(stream, "subtitle")
                chk = Gtk.CheckMenuItem(label=label)
                chk.set_active(stream["enabled"])
                chk.connect("toggled",
                            lambda btn, s=stream, fp=file_path, tp=tree_path:
                            self._on_stream_toggle(btn, s, fp, tp))
                sub_menu.append(chk)
            sub_item.set_submenu(sub_menu)
        else:
            sub_item.set_sensitive(False)
        menu.append(sub_item)

        menu.append(Gtk.SeparatorMenuItem())

        # ---- Rotation ---------------------------------------------------
        menu.append(self._make_radio_submenu(
            title="Drehung",
            options=[("Keine Drehung", 0),
                     ("90° im Uhrzeigersinn", 90),
                     ("90° gegen Uhrzeigersinn", -90)],
            current=fs.get("rotation", 0),
            global_label=None,          # rotation has no "global" option
            on_select=lambda v, fp=file_path:
                self._file_override_set(fp, "rotation", v),
        ))

        menu.append(Gtk.SeparatorMenuItem())

        # ---- Per-file encoding settings ---------------------------------
        menu.append(self._make_radio_submenu(
            title="Video-Bitrate",
            options=VIDEO_BITRATES,
            current=fs.get("video_bitrate", "GLOBAL"),
            global_label="Global verwenden",
            on_select=lambda v, fp=file_path:
                self._file_override_set(fp, "video_bitrate", v),
        ))

        menu.append(self._make_radio_submenu(
            title="Audio-Bitrate",
            options=AUDIO_BITRATES,
            current=fs.get("audio_bitrate", "GLOBAL") if "audio_bitrate" in fs else "GLOBAL",
            global_label="Global verwenden",
            on_select=lambda v, fp=file_path:
                self._file_override_set(fp, "audio_bitrate", v),
        ))

        menu.append(self._make_radio_submenu(
            title="Auflösung",
            options=RESOLUTIONS,
            current=fs.get("resolution_height", "GLOBAL") if "resolution_height" in fs else "GLOBAL",
            global_label="Global verwenden",
            on_select=lambda v, fp=file_path:
                self._file_override_set(fp, "resolution_height", v),
        ))

        menu.append(self._make_radio_submenu(
            title="FPS",
            options=[("Original behalten", None), ("Auf 30 fps begrenzen", 30)],
            current=fs.get("fps_limit", "GLOBAL") if "fps_limit" in fs else "GLOBAL",
            global_label="Global verwenden",
            on_select=lambda v, fp=file_path:
                self._file_override_set(fp, "fps_limit", v),
        ))

        menu.append(Gtk.SeparatorMenuItem())

        # ---- Stop after this file ---------------------------------------
        is_stop = self._stop_after_path == file_path
        stop_label = "⏹  Nach dieser Datei stoppen ✓" if is_stop else "⏹  Nach dieser Datei stoppen"
        item_stop = Gtk.MenuItem(label=stop_label)
        item_stop.connect(
            "activate",
            lambda _, fp=file_path: self._set_stop_after(None if self._stop_after_path == fp else fp),
        )
        menu.append(item_stop)

        menu.append(Gtk.SeparatorMenuItem())

        # ---- Remove from list -------------------------------------------
        item_remove = Gtk.MenuItem(label="Aus Liste entfernen")
        item_remove.connect("activate", lambda _, fp=file_path:
                            self._remove_file(fp))
        menu.append(item_remove)

        menu.show_all()
        menu.popup_at_pointer(event)

    def _make_radio_submenu(self, title: str, options: list, current,
                            global_label: Optional[str],
                            on_select) -> Gtk.MenuItem:
        """Build a MenuItem with a radio submenu.

        options  – list of (label, value) tuples
        current  – currently selected value, or "GLOBAL" sentinel
        global_label – if not None, prepend a "Global verwenden" radio item
        on_select(value) – called with the chosen value (or "GLOBAL")
        """
        parent = Gtk.MenuItem(label=title)
        sub = Gtk.Menu()
        buttons: list[tuple[Gtk.RadioMenuItem, object]] = []

        first = None
        if global_label is not None:
            r = Gtk.RadioMenuItem(label=global_label)
            first = r
            sub.append(r)
            buttons.append((r, "GLOBAL"))

        for lbl, val in options:
            if first is None:
                r = Gtk.RadioMenuItem(label=lbl)
                first = r
            else:
                r = Gtk.RadioMenuItem.new_with_label_from_widget(first, lbl)
            sub.append(r)
            buttons.append((r, val))

        # Set active state before connecting signals to avoid spurious calls
        activated = False
        for btn, val in buttons:
            if current == "GLOBAL" and val == "GLOBAL":
                btn.set_active(True)
                activated = True
                break
            if current != "GLOBAL" and val == current:
                btn.set_active(True)
                activated = True
                break
        if not activated and buttons:
            buttons[0][0].set_active(True)

        def _connect(btn, val):
            def _on_toggle(b):
                if b.get_active():
                    on_select(val)
                    self._save_queue()
            btn.connect("toggled", _on_toggle)

        for btn, val in buttons:
            _connect(btn, val)

        parent.set_submenu(sub)
        return parent

    def _file_override_set(self, path: str, key: str, value):
        """Set or clear a per-file setting override and persist the queue."""
        if value == "GLOBAL":
            if path in self._file_settings:
                self._file_settings[path].pop(key, None)
                if not self._file_settings[path]:
                    del self._file_settings[path]
        else:
            self._file_settings.setdefault(path, {})[key] = value

    def _remove_file(self, path: str):
        """Remove a single file from the queue and the list store."""
        if self._stop_after_path == path:
            self._stop_after_path = None   # row is gone, no need to clear the cell
        it = self._find_row(path)
        if it:
            self._store.remove(it)
        if path in self._queue:
            self._queue.remove(path)
        self._file_streams.pop(path, None)
        self._file_settings.pop(path, None)
        self._completed.discard(path)
        if self._preview_path == path:
            self._clear_preview()
        self._save_queue()

    def _on_stream_toggle(self, btn, stream: dict, file_path: str, tree_path):
        stream["enabled"] = btn.get_active()
        it = self._store.get_iter(tree_path)
        if it:
            audio, subs = self._file_streams.get(file_path, ([], []))
            self._store.set_value(it, COL_AUDIO_LABEL, self._stream_summary(audio))
            self._store.set_value(it, COL_SUB_LABEL,   self._stream_summary(subs))

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

    def _show_error_detail(self, filename: str, full_msg: str):
        """Show a dialog with the complete ffmpeg error output."""
        dlg = Gtk.Dialog(
            title=f"Fehler – {filename}",
            transient_for=self,
            modal=True,
        )
        dlg.set_default_size(640, 380)
        dlg.add_button("Schließen", Gtk.ResponseType.CLOSE)

        area = dlg.get_content_area()
        area.set_border_width(12)
        area.set_spacing(8)

        lbl = Gtk.Label()
        lbl.set_markup("<b>ffmpeg-Ausgabe:</b>")
        lbl.set_halign(Gtk.Align.START)
        area.pack_start(lbl, False, False, 0)

        tv = Gtk.TextView()
        tv.set_editable(False)
        tv.set_monospace(True)
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv.get_buffer().set_text(full_msg)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(tv)
        area.pack_start(sw, True, True, 0)

        # Scroll to the bottom so the last (most relevant) lines are visible.
        def _scroll_end(_):
            adj = sw.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
        dlg.connect("show", _scroll_end)

        dlg.show_all()
        dlg.run()
        dlg.destroy()


# ---------------------------------------------------------------------------
# Scan Dialog
# ---------------------------------------------------------------------------

class ScanDialog(Gtk.Window):
    """Stand-alone window that scans a folder tree and collects videos by bitrate."""

    # Custom signal to hand selected paths back to the main window.
    __gsignals__ = {
        "files-selected": (
            GObject.SignalFlags.RUN_FIRST, None, (object,)
        ),
    }

    # Result-store column indices
    _C_CHECK  = 0
    _C_NAME   = 1
    _C_DIR    = 2
    _C_KBPS   = 3
    _C_PATH   = 4

    def __init__(self, parent: Gtk.Window):
        super().__init__(title="Ordner nach Videos scannen")
        self.set_transient_for(parent)
        self.set_destroy_with_parent(True)
        self.set_default_size(740, 560)
        self.set_border_width(0)

        self._cancel_flag = threading.Event()
        self._scanning    = False

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)

        # ---- Settings bar ---------------------------------------------
        bar = Gtk.Box(spacing=8)
        bar.set_border_width(10)
        root.pack_start(bar, False, False, 0)

        bar.pack_start(Gtk.Label(label="Ordner:"), False, False, 0)
        self._entry_folder = Gtk.Entry()
        self._entry_folder.set_placeholder_text("Ordner auswählen…")
        self._entry_folder.set_hexpand(True)
        bar.pack_start(self._entry_folder, True, True, 0)

        btn_browse = Gtk.Button(label="Durchsuchen…")
        btn_browse.connect("clicked", self._on_browse)
        bar.pack_start(btn_browse, False, False, 0)

        bar2 = Gtk.Box(spacing=8)
        bar2.set_border_width(10)
        bar2.set_margin_top(0)
        root.pack_start(bar2, False, False, 0)

        bar2.pack_start(Gtk.Label(label="Bitrate-Schwelle:"), False, False, 0)
        adj = Gtk.Adjustment(value=7000, lower=100, upper=200000,
                             step_increment=500, page_increment=5000)
        self._spin = Gtk.SpinButton(adjustment=adj, climb_rate=500, digits=0)
        self._spin.set_width_chars(8)
        bar2.pack_start(self._spin, False, False, 0)
        bar2.pack_start(Gtk.Label(label="kbps  –  Videos"), False, False, 0)
        hint = Gtk.Label(label="mit höherer Bitrate werden gefunden")
        hint.set_sensitive(False)
        bar2.pack_start(hint, False, False, 0)

        # Scan / Stop buttons
        btn_box = Gtk.Box(spacing=6)
        btn_box.set_border_width(10)
        btn_box.set_margin_top(0)
        root.pack_start(btn_box, False, False, 0)

        self._btn_scan = Gtk.Button(label="▶  Scannen starten")
        self._btn_scan.get_style_context().add_class("suggested-action")
        self._btn_scan.connect("clicked", self._on_scan)
        btn_box.pack_start(self._btn_scan, False, False, 0)

        self._btn_stop = Gtk.Button(label="■  Stopp")
        self._btn_stop.set_sensitive(False)
        self._btn_stop.connect("clicked", self._on_stop)
        btn_box.pack_start(self._btn_stop, False, False, 0)

        # ---- Progress -------------------------------------------------
        prog_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        prog_box.set_border_width(10)
        prog_box.set_margin_top(0)
        root.pack_start(prog_box, False, False, 0)

        self._prog_label = Gtk.Label(label=" ")
        self._prog_label.set_halign(Gtk.Align.START)
        prog_box.pack_start(self._prog_label, False, False, 0)

        self._prog_bar = Gtk.ProgressBar()
        prog_box.pack_start(self._prog_bar, False, False, 0)

        # ---- Results list ---------------------------------------------
        # store: selected(bool), filename, directory, bitrate_kbps, full_path
        self._store = Gtk.ListStore(bool, str, str, int, str)

        tv = Gtk.TreeView(model=self._store)
        tv.set_headers_clickable(True)

        # Checkbox column
        chk_cell = Gtk.CellRendererToggle()
        chk_cell.connect("toggled", self._on_row_toggled)
        chk_col = Gtk.TreeViewColumn("", chk_cell, active=self._C_CHECK)
        chk_col.set_fixed_width(32)
        tv.append_column(chk_col)

        # Filename
        name_cell = Gtk.CellRendererText()
        name_cell.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
        name_col = Gtk.TreeViewColumn("Dateiname", name_cell, text=self._C_NAME)
        name_col.set_expand(True)
        name_col.set_resizable(True)
        tv.append_column(name_col)

        # Directory
        dir_cell = Gtk.CellRendererText()
        dir_cell.set_property("ellipsize", Pango.EllipsizeMode.START)
        dir_col = Gtk.TreeViewColumn("Verzeichnis", dir_cell, text=self._C_DIR)
        dir_col.set_min_width(140)
        dir_col.set_resizable(True)
        tv.append_column(dir_col)

        # Bitrate (rendered as formatted string)
        br_cell = Gtk.CellRendererText()
        br_cell.set_property("xalign", 1.0)
        br_col = Gtk.TreeViewColumn("Bitrate", br_cell)
        br_col.set_cell_data_func(br_cell, self._render_bitrate)
        br_col.set_min_width(100)
        tv.append_column(br_col)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(tv)
        root.pack_start(sw, True, True, 0)

        # ---- Bottom bar -----------------------------------------------
        bot = Gtk.Box(spacing=8)
        bot.set_border_width(10)
        root.pack_start(bot, False, False, 0)

        self._summary = Gtk.Label(label="Keine Ergebnisse.")
        self._summary.set_halign(Gtk.Align.START)
        bot.pack_start(self._summary, True, True, 0)

        btn_all = Gtk.Button(label="Alle")
        btn_all.connect("clicked", lambda *_: self._set_all(True))
        bot.pack_start(btn_all, False, False, 0)

        btn_none = Gtk.Button(label="Keine")
        btn_none.connect("clicked", lambda *_: self._set_all(False))
        bot.pack_start(btn_none, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        bot.pack_start(sep, False, False, 4)

        self._btn_add = Gtk.Button(label="In Queue übernehmen")
        self._btn_add.get_style_context().add_class("suggested-action")
        self._btn_add.set_sensitive(False)
        self._btn_add.connect("clicked", self._on_add_to_queue)
        bot.pack_start(self._btn_add, False, False, 0)

        btn_close = Gtk.Button(label="Schließen")
        btn_close.connect("clicked", lambda *_: self.destroy())
        bot.pack_start(btn_close, False, False, 0)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _render_bitrate(_col, cell, model, it, _):
        kbps = model.get_value(it, ScanDialog._C_KBPS)
        if kbps >= 10000:
            cell.set_property("text", f"{kbps / 1000:.1f} Mbps")
        else:
            cell.set_property("text", f"{kbps:,} kbps".replace(",", "\u202f"))

    def _set_all(self, state: bool):
        it = self._store.get_iter_first()
        while it:
            self._store.set_value(it, self._C_CHECK, state)
            it = self._store.iter_next(it)
        self._refresh_summary()

    def _refresh_summary(self):
        total    = len(self._store)
        selected = sum(1 for row in self._store if row[self._C_CHECK])
        self._summary.set_text(
            f"{total} Video{'s' if total != 1 else ''} gefunden  ·  "
            f"{selected} ausgewählt"
        )
        self._btn_add.set_sensitive(selected > 0)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_browse(self, *_):
        dlg = Gtk.FileChooserDialog(
            title="Ordner auswählen",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OPEN,   Gtk.ResponseType.OK)
        if dlg.run() == Gtk.ResponseType.OK:
            self._entry_folder.set_text(dlg.get_filename())
        dlg.destroy()

    def _on_row_toggled(self, _cell, path_str):
        it = self._store.get_iter(path_str)
        self._store.set_value(it, self._C_CHECK,
                              not self._store.get_value(it, self._C_CHECK))
        self._refresh_summary()

    def _on_stop(self, *_):
        self._cancel_flag.set()
        self._btn_stop.set_sensitive(False)

    def _on_scan(self, *_):
        folder = self._entry_folder.get_text().strip()
        if not folder:
            return
        if not os.path.isdir(folder):
            dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                    message_type=Gtk.MessageType.ERROR,
                                    buttons=Gtk.ButtonsType.OK,
                                    text=f'Ordner nicht gefunden:\n{folder}')
            dlg.run(); dlg.destroy()
            return

        self._store.clear()
        self._cancel_flag.clear()
        self._scanning = True
        self._btn_scan.set_sensitive(False)
        self._btn_stop.set_sensitive(True)
        self._btn_add.set_sensitive(False)
        self._prog_bar.set_fraction(0)
        self._summary.set_text("Scanne…")

        threshold = int(self._spin.get_value())

        def _on_progress(checked, total, found, current):
            GLib.idle_add(self._update_progress, checked, total, found, current)

        def _on_found(path, kbps):
            GLib.idle_add(self._add_result, path, kbps)

        def _run():
            scan_folder(
                folder=folder,
                threshold_kbps=threshold,
                on_progress=_on_progress,
                on_found=_on_found,
                is_cancelled=self._cancel_flag.is_set,
            )
            GLib.idle_add(self._scan_finished)

        threading.Thread(target=_run, daemon=True).start()

    def _on_add_to_queue(self, *_):
        paths = [row[self._C_PATH] for row in self._store if row[self._C_CHECK]]
        if paths:
            self.emit("files-selected", paths)
            n = len(paths)
            self._summary.set_text(
                f"{n} Datei{'en' if n != 1 else ''} zur Konvertierungsliste hinzugefügt."
            )
            self._btn_add.set_sensitive(False)

    # ------------------------------------------------------------------
    # Background-thread callbacks (always called via GLib.idle_add)
    # ------------------------------------------------------------------

    def _update_progress(self, checked, total, found, current):
        if total > 0:
            self._prog_bar.set_fraction(checked / total)
            label = (f"Geprüft: {checked} / {total}  ·  "
                     f"Gefunden: {found}"
                     + (f"  ·  {current}" if current else ""))
        else:
            label = "Keine Videodateien gefunden."
        self._prog_label.set_text(label)
        return False

    def _add_result(self, path, kbps):
        self._store.append([
            True,
            os.path.basename(path),
            os.path.dirname(path),
            kbps,
            path,
        ])
        self._refresh_summary()
        return False

    def _scan_finished(self):
        self._scanning = False
        self._btn_scan.set_sensitive(True)
        self._btn_stop.set_sensitive(False)
        self._prog_bar.set_fraction(1.0)
        total = len(self._store)
        if total == 0:
            self._prog_label.set_text("Scan abgeschlossen – keine Videos über dem Schwellenwert.")
            self._summary.set_text("Keine Ergebnisse.")
        else:
            self._prog_label.set_text("Scan abgeschlossen.")
        return False


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main():
    win = MainWindow()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
