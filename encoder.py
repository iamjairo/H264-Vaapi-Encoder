import subprocess
import json
import os
import glob
import collections
import threading
import queue
import time
from dataclasses import dataclass
from typing import Optional, Callable

VIDEO_EXTENSIONS = frozenset([
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".m4v", ".ts", ".mts", ".m2ts",
    ".mpg", ".mpeg", ".vob", ".3gp", ".ogv",
    ".rm", ".rmvb", ".divx", ".asf", ".f4v",
])


@dataclass
class EncodeJob:
    input_path: str
    output_path: str
    video_bitrate: str
    audio_bitrate: str
    replace_original: bool
    resolution_height: Optional[int] = None      # None = keep original
    selected_audio: Optional[list[int]] = None   # rel. indices; None = all
    selected_subtitles: Optional[list[int]] = None  # rel. indices; None = none


def probe_video(path: str) -> dict:
    """Return video stream metadata via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr}")
    data = json.loads(result.stdout)
    return data


def get_fps(path: str) -> float:
    """Return the frame rate of the first video stream."""
    try:
        data = probe_video(path)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                r_frame_rate = stream.get("r_frame_rate", "0/1")
                num, den = r_frame_rate.split("/")
                return float(num) / float(den) if float(den) != 0 else 0.0
    except Exception:
        pass
    return 0.0


def get_video_dimensions(path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream, or (0, 0)."""
    try:
        data = probe_video(path)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                return int(stream.get("width", 0)), int(stream.get("height", 0))
    except Exception:
        pass
    return 0, 0


def compute_output_dimensions(src_w: int, src_h: int, target_h: int) -> tuple[int, int]:
    """Return output (width, height) preserving aspect ratio for a given target height.

    The width is rounded to the nearest even number as required by most codecs.
    Example: 1440×1080 source → target_h=720 → 960×720  (4:3 preserved)
             1920×1080 source → target_h=720 → 1280×720  (16:9 preserved)
    """
    if src_h == 0:
        return 0, target_h
    out_w = int(round(src_w / src_h * target_h / 2)) * 2
    return out_w, target_h


def get_streams(path: str) -> tuple[list[dict], list[dict]]:
    """Return (audio_streams, subtitle_streams) for a file.

    Each audio dict:   {rel_idx, codec, language, title, channels, channel_layout, enabled}
    Each subtitle dict:{rel_idx, codec, language, title, enabled}
    """
    audio: list[dict] = []
    subtitles: list[dict] = []
    try:
        data = probe_video(path)
        a_idx = s_idx = 0
        for stream in data.get("streams", []):
            ctype = stream.get("codec_type", "")
            codec = stream.get("codec_name", "?")
            tags  = stream.get("tags", {})
            lang  = tags.get("language") or tags.get("LANGUAGE") or ""
            title = tags.get("title")    or tags.get("TITLE")    or ""
            if ctype == "audio":
                audio.append({
                    "rel_idx":        a_idx,
                    "codec":          codec,
                    "language":       lang,
                    "title":          title,
                    "channels":       stream.get("channels", 0),
                    "channel_layout": stream.get("channel_layout", ""),
                    "enabled":        True,
                })
                a_idx += 1
            elif ctype == "subtitle":
                subtitles.append({
                    "rel_idx":  s_idx,
                    "codec":    codec,
                    "language": lang,
                    "title":    title,
                    "enabled":  True,
                })
                s_idx += 1
    except Exception:
        pass
    return audio, subtitles


def get_bitrate_kbps(path: str) -> Optional[int]:
    """Return overall file bitrate in kbps via ffprobe, or None on failure.

    Only queries the container format header — no decoding, stays fast.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            br = json.loads(result.stdout).get("format", {}).get("bit_rate")
            if br:
                return max(1, int(br) // 1000)
    except Exception:
        pass
    return None


def scan_folder(
    folder: str,
    threshold_kbps: int,
    on_progress: Callable[[int, int, int, str], None],
    # (files_checked, total_files, found_count, current_filename)
    on_found: Callable[[str, int], None],   # (full_path, bitrate_kbps)
    is_cancelled: Callable[[], bool],
) -> None:
    """Recursively scan *folder* for video files whose bitrate > threshold_kbps.

    Designed to run in a background thread; all results are delivered via
    the provided callbacks which the caller should route through GLib.idle_add.
    """
    video_files: list[str] = []
    for root, _dirs, files in os.walk(folder):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                video_files.append(os.path.join(root, f))

    total = len(video_files)
    found = 0
    for idx, path in enumerate(video_files):
        if is_cancelled():
            break
        on_progress(idx, total, found, os.path.basename(path))
        kbps = get_bitrate_kbps(path)
        if kbps is not None and kbps > threshold_kbps:
            found += 1
            on_found(path, kbps)

    on_progress(total, total, found, "")   # final / done signal


def get_file_metadata(path: str) -> dict:
    """Return all display metadata in a single ffprobe call.

    Returned dict keys:
      audio          – list[dict]  (same format as get_streams)
      subtitles      – list[dict]
      width          – int
      height         – int
      video_kbps     – int | None
      audio_kbps     – int | None  (first audio stream)
    """
    out = dict(audio=[], subtitles=[], width=0, height=0,
               video_kbps=None, audio_kbps=None)
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return out
        data = json.loads(result.stdout)
        fmt  = data.get("format", {})

        overall_kbps: int | None = None
        raw_br = fmt.get("bit_rate")
        if raw_br:
            overall_kbps = max(1, int(raw_br) // 1000)

        a_idx = s_idx = 0
        for stream in data.get("streams", []):
            ctype = stream.get("codec_type", "")
            codec = stream.get("codec_name", "?")
            tags  = stream.get("tags", {})
            lang  = tags.get("language") or tags.get("LANGUAGE") or ""
            title = tags.get("title")    or tags.get("TITLE")    or ""

            if ctype == "video":
                out["width"]  = stream.get("width",  0)
                out["height"] = stream.get("height", 0)
                vbr = stream.get("bit_rate")
                if vbr:
                    out["video_kbps"] = max(1, int(vbr) // 1000)
            elif ctype == "audio":
                abr = stream.get("bit_rate")
                if abr and out["audio_kbps"] is None:
                    out["audio_kbps"] = max(1, int(abr) // 1000)
                out["audio"].append({
                    "rel_idx": a_idx, "codec": codec,
                    "language": lang, "title": title,
                    "channels": stream.get("channels", 0),
                    "channel_layout": stream.get("channel_layout", ""),
                    "enabled": True,
                })
                a_idx += 1
            elif ctype == "subtitle":
                out["subtitles"].append({
                    "rel_idx": s_idx, "codec": codec,
                    "language": lang, "title": title,
                    "enabled": True,
                })
                s_idx += 1

        # Fallback: derive video bitrate from overall minus audio.
        if out["video_kbps"] is None and overall_kbps:
            audio_kbps = out["audio_kbps"] or 0
            out["video_kbps"] = max(1, overall_kbps - audio_kbps)

    except Exception:
        pass
    return out


def get_duration(path: str) -> float:
    """Return duration in seconds."""
    try:
        data = probe_video(path)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                dur = stream.get("duration")
                if dur:
                    return float(dur)
        # fallback: format duration
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        fmt = json.loads(result.stdout).get("format", {})
        if "duration" in fmt:
            return float(fmt["duration"])
    except Exception:
        pass
    return 0.0


HIGH_FPS_THRESHOLD = 40.0


def find_vaapi_device() -> Optional[str]:
    """Return the first available DRI render node, or None if not found."""
    devices = sorted(glob.glob("/dev/dri/renderD*"))
    return devices[0] if devices else None


def _parse_bitrate_kbps(bitrate_str: str) -> int:
    """Convert a string like '4000k' or '4000' to integer kbps."""
    s = bitrate_str.strip().lower().rstrip("k")
    return int(s)


def _double_bitrate(bitrate_str: str) -> str:
    """Double a bitrate string, preserving the 'k' suffix."""
    kbps = _parse_bitrate_kbps(bitrate_str)
    return f"{kbps * 2}k"


def build_ffmpeg_cmd(job: EncodeJob, fps: float) -> list[str]:
    video_bitrate = job.video_bitrate
    audio_bitrate = job.audio_bitrate

    if fps >= HIGH_FPS_THRESHOLD:
        video_bitrate = _double_bitrate(video_bitrate)

    device = find_vaapi_device()

    if job.resolution_height is not None:
        # Scaling needed → pure software decode + CPU scaling + hwupload + HW encode.
        #
        # -init_hw_device vaapi=va:<dev>   create a named VAAPI device
        # -filter_hw_device va             give hwupload a device reference
        # scale=w=-2:h=H                   CPU resize (avoids scale_vaapi hang)
        # format=nv12                       ensure NV12 before upload
        # hwupload                          send frames to VAAPI GPU memory
        # h264_vaapi                        GPU encoder
        #
        # Note: no -hwaccel flags here; the device ref comes from
        # -filter_hw_device, not from the decoder context.
        if device:
            hw_args = ["-init_hw_device", f"vaapi=va:{device}",
                       "-filter_hw_device", "va"]
        else:
            hw_args = ["-init_hw_device", "vaapi=va",
                       "-filter_hw_device", "va"]
        vf_args = ["-vf",
                   f"scale=w=-2:h={job.resolution_height},format=nv12,hwupload"]
    else:
        # No scaling → full hardware-decode + hardware-encode pipeline.
        #
        #   -hwaccel_output_format vaapi    decoder leaves frames on GPU
        #   h264_vaapi                      encoder reads directly from GPU
        #
        # hwupload must NOT be used: frames are already on the GPU.
        hw_args = ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]
        if device:
            hw_args += ["-hwaccel_device", device]
        vf_args = []

    explicit_map = (
        job.selected_audio     is not None or
        job.selected_subtitles is not None
    )

    cmd = ["ffmpeg", "-y", *hw_args, "-i", job.input_path]

    if explicit_map:
        cmd += ["-map", "0:v:0"]
        for idx in (job.selected_audio or []):
            cmd += ["-map", f"0:a:{idx}"]
        for idx in (job.selected_subtitles or []):
            cmd += ["-map", f"0:s:{idx}"]

    cmd += [
        *vf_args,
        "-c:v", "h264_vaapi",
        "-b:v", video_bitrate,
        "-c:a", "aac",
        "-b:a", audio_bitrate,
    ]

    if explicit_map and job.selected_subtitles:
        cmd += ["-c:s", "mov_text"]

    cmd.append(job.output_path)
    return cmd


WATCHDOG_TIMEOUT = 30   # seconds of silence before we assume ffmpeg is hung


class Encoder:
    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def _run_ffmpeg(
        self,
        cmd: list[str],
        duration: float,
        on_progress: Callable[[float], None],
    ) -> tuple[bool, bool, collections.deque]:
        """Spawn ffmpeg and read its output.

        Returns (hung, cancelled, recent_lines).
          hung      – True if the process produced no output for WATCHDOG_TIMEOUT s
          cancelled – True if self._cancelled was set
          recent    – rolling window of the last 40 output lines
        """
        print("ffmpeg cmd:", " ".join(cmd), flush=True)

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        recent: collections.deque = collections.deque(maxlen=40)
        line_queue: queue.Queue = queue.Queue()

        def _reader():
            for line in self._process.stdout:
                line_queue.put(line)
            line_queue.put(None)   # EOF sentinel

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        last_activity = time.monotonic()
        hung = False

        while True:
            try:
                line = line_queue.get(timeout=1.0)
            except queue.Empty:
                if self._cancelled:
                    self._process.kill()
                    break
                if time.monotonic() - last_activity > WATCHDOG_TIMEOUT:
                    print("ffmpeg watchdog: no output for "
                          f"{WATCHDOG_TIMEOUT}s — killing process", flush=True)
                    self._process.kill()
                    hung = True
                    break
                continue

            if line is None:    # EOF — process finished
                break

            last_activity = time.monotonic()
            recent.append(line.rstrip())

            if self._cancelled:
                self._process.kill()
                break

            if duration > 0 and "time=" in line:
                try:
                    time_str = line.split("time=")[1].split()[0]
                    h, m, s = time_str.split(":")
                    elapsed = int(h) * 3600 + int(m) * 60 + float(s)
                    on_progress(min(elapsed / duration, 1.0))
                except Exception:
                    pass

        self._process.wait()
        return hung, self._cancelled, recent

    def encode(
        self,
        job: EncodeJob,
        on_progress: Callable[[float], None],   # 0.0–1.0
        on_done: Callable[[bool, str], None],   # success, message
    ):
        """Run encoding in a background thread."""
        self._cancelled = False

        def _run():
            try:
                fps = get_fps(job.input_path)
                duration = get_duration(job.input_path)
                cmd = build_ffmpeg_cmd(job, fps)
                hung, cancelled, recent = self._run_ffmpeg(cmd, duration, on_progress)

                if cancelled:
                    if os.path.exists(job.output_path):
                        os.remove(job.output_path)
                    on_done(False, "Abgebrochen")
                    return

                if hung:
                    if os.path.exists(job.output_path):
                        os.remove(job.output_path)
                    on_done(False, f"ffmpeg hängt (kein Fortschritt nach "
                            f"{WATCHDOG_TIMEOUT}s).")
                    return

                if self._process.returncode != 0:
                    if os.path.exists(job.output_path):
                        os.remove(job.output_path)
                    error_lines = _filter_error_lines(recent)
                    detail = ("\n".join(error_lines[-15:])
                              if error_lines else "(keine Details)")
                    on_done(False, f"ffmpeg Fehler (Code {self._process.returncode})"
                            f"\n\n{detail}")
                    return

                if job.replace_original:
                    os.replace(job.output_path, job.input_path)

                on_done(True, "")

            except Exception as exc:
                on_done(False, str(exc))

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()


def _filter_error_lines(recent: collections.deque) -> list[str]:
    return [
        l for l in recent
        if l
        and not l.startswith("frame=")
        and "time=" not in l
        and not l.startswith("size=")
        and not l.startswith("speed=")
    ]
