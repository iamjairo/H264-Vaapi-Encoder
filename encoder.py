import subprocess
import json
import os
import glob
import collections
import threading
from dataclasses import dataclass
from typing import Optional, Callable


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

    # VAAPI pipeline strategy:
    #   -hwaccel vaapi          — activates VAAPI decode acceleration and
    #                             creates a shared device context that is
    #                             reused by hwupload and h264_vaapi encoder.
    #   -hwaccel_device <dev>   — explicit render node (omitted = auto).
    #   (no -hwaccel_output_format vaapi) — decoder outputs CPU frames so
    #                             that the hwupload in the filter chain has
    #                             something to actually upload.  Passing
    #                             -hwaccel_output_format vaapi causes the
    #                             decoder to output already-uploaded frames
    #                             and hwupload then returns EINVAL (exit 234).
    #   format=nv12|vaapi       — accept CPU-side nv12 OR pass-through
    #                             VAAPI frames transparently.
    #   hwupload                — upload CPU frames to the VAAPI device.
    #   scale_vaapi=w=-2:h=H   — optional hardware scaler.
    #   h264_vaapi              — hardware H.264 encoder.
    device = find_vaapi_device()
    hw_args = ["-hwaccel", "vaapi"]
    if device:
        hw_args += ["-hwaccel_device", device]

    if job.resolution_height is not None:
        vf = (f"format=nv12|vaapi,hwupload,"
              f"scale_vaapi=w=-2:h={job.resolution_height}")
    else:
        vf = "format=nv12|vaapi,hwupload"

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
        "-vf", vf,
        "-c:v", "h264_vaapi",
        "-b:v", video_bitrate,
        "-c:a", "aac",
        "-b:a", audio_bitrate,
    ]

    if explicit_map and job.selected_subtitles:
        cmd += ["-c:s", "mov_text"]

    cmd.append(job.output_path)
    return cmd


class Encoder:
    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        if self._process and self._process.poll() is None:
            self._process.terminate()

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

                # Log the exact command so it can be reproduced / debugged.
                print("ffmpeg cmd:", " ".join(cmd), flush=True)

                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # merge so we read both
                    text=True,
                    bufsize=1,
                )

                # Keep a rolling window of recent output for error reporting.
                recent = collections.deque(maxlen=40)

                for line in self._process.stdout:
                    recent.append(line.rstrip())
                    if self._cancelled:
                        break
                    # Parse ffmpeg progress from "time=HH:MM:SS.xx"
                    if duration > 0 and "time=" in line:
                        try:
                            time_str = line.split("time=")[1].split()[0]
                            h, m, s = time_str.split(":")
                            elapsed = int(h) * 3600 + int(m) * 60 + float(s)
                            progress = min(elapsed / duration, 1.0)
                            on_progress(progress)
                        except Exception:
                            pass

                self._process.wait()

                if self._cancelled:
                    if os.path.exists(job.output_path):
                        os.remove(job.output_path)
                    on_done(False, "Abgebrochen")
                    return

                if self._process.returncode != 0:
                    # Build a readable summary from the captured output.
                    # Skip the per-frame progress lines; keep everything else.
                    error_lines = [
                        l for l in recent
                        if l
                        and not l.startswith("frame=")
                        and "time=" not in l
                        and not l.startswith("size=")
                        and not l.startswith("speed=")
                    ]
                    detail = "\n".join(error_lines[-15:]) if error_lines else "(keine Details)"
                    on_done(False,
                            f"ffmpeg Fehler (Code {self._process.returncode})\n\n"
                            f"{detail}")
                    return

                if job.replace_original:
                    os.replace(job.output_path, job.input_path)

                on_done(True, "")

            except Exception as exc:
                on_done(False, str(exc))

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
