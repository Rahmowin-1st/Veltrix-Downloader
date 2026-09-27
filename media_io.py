"""Media inspection and lossless delivery helpers; no network credentials here."""
from __future__ import annotations

import json
import math
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=256)
def _probe(name: str, size: int, mtime: int) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", name],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Invalid or incomplete media file")
    info = json.loads(result.stdout)
    if not info.get("streams"):
        raise RuntimeError("File has no media streams")
    return info


def probe(path: Path) -> dict:
    stat = path.stat()
    return _probe(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def media_kind(path: Path) -> str:
    info = probe(path)
    streams = info["streams"]
    video = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audio = any(s.get("codec_type") == "audio" for s in streams)
    fmt = info.get("format", {}).get("format_name", "")
    if video:
        if "gif" in fmt or (video[0].get("codec_name") == "webp" and int(video[0].get("nb_frames", 1) or 1) > 1):
            return "animation"
        if not audio and ("image2" in fmt or "_pipe" in fmt or video[0].get("codec_name") in {"png", "mjpeg", "webp"}):
            if float(info.get("format", {}).get("duration", 0) or 0) <= 0.1:
                return "image"
        return "video"
    if audio:
        return "audio"
    raise RuntimeError("Unsupported media streams")


def streamable(path: Path) -> bool:
    info = probe(path)
    videos = [s for s in info["streams"] if s.get("codec_type") == "video"]
    audios = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    return (path.suffix.lower() == ".mp4" and bool(videos)
            and all(s.get("codec_name") == "h264" for s in videos)
            and all(s.get("codec_name") in {"aac", "mp3"} for s in audios))


def prepare_video(path: Path, dest: Path) -> Path:
    """Remux compatible tracks to MP4 without re-encoding a single frame."""
    info = probe(path)
    codecs = {s.get("codec_name") for s in info["streams"] if s.get("codec_type") in {"video", "audio"}}
    if path.suffix.lower() == ".mp4" or not codecs.issubset({"h264", "aac", "mp3"}):
        return path
    dest.mkdir(parents=True, exist_ok=True)
    output = dest / (path.stem + ".mp4")
    result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
                             "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart", str(output)],
                            capture_output=True, timeout=900)
    if result.returncode:
        output.unlink(missing_ok=True)
        return path
    return output


def lossless_parts(path: Path, dest: Path, limit: int) -> list[Path]:
    """Split on existing keyframes, preserving codecs; reject oversize segments."""
    if path.stat().st_size <= limit:
        return [path]
    duration = float(probe(path).get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("Cannot split a file with unknown duration without quality loss")
    dest.mkdir(parents=True, exist_ok=True)
    interval = max(0.25, duration / math.ceil(path.stat().st_size / (limit * .70)))
    ext = path.suffix.lower() if path.suffix.lower() in {".mp4", ".m4a", ".mp3", ".mkv", ".webm", ".ogg", ".flac"} else ".mkv"
    for attempt in range(5):
        folder = dest / str(attempt)
        folder.mkdir(exist_ok=True)
        result = subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
            "-map", "0:v?", "-map", "0:a?", "-c", "copy", "-f", "segment",
            "-segment_time", str(interval), "-reset_timestamps", "1",
            str(folder / ("part%05d" + ext)),
        ], capture_output=True, text=True, timeout=900)
        parts = sorted(folder.glob("part*"))
        if result.returncode == 0 and parts and all(0 < p.stat().st_size <= limit for p in parts):
            return parts
        for part in parts:
            part.unlink()
        interval /= 2
    raise RuntimeError("Telegram limit: source keyframes are too large. A local Bot API server allows larger original files.")


def native_video(path: Path, dest: Path) -> Path:
    """Prefer remuxing; encode incompatible codecs for Telegram video playback."""
    final = prepare_video(path, dest)
    if streamable(final):
        return final
    dest.mkdir(parents=True, exist_ok=True)
    output = dest / (path.stem + "_playable.mp4")
    result = subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "18", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", str(output),
    ], capture_output=True, timeout=1800)
    if result.returncode or not streamable(output):
        raise RuntimeError("Could not prepare playable Telegram video")
    return output
