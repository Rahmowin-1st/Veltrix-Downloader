"""Media inspection and lossless delivery helpers; no network credentials here."""
from __future__ import annotations

import json
import math
import subprocess
import tempfile
from media_process import run
from fractions import Fraction
from functools import lru_cache
from pathlib import Path


class InvalidMedia(RuntimeError):
    """A downloaded rendition did not contain readable media."""


@lru_cache(maxsize=256)
def _probe(name: str, size: int, mtime: int) -> dict:
    result = run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", name],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise InvalidMedia("Invalid or incomplete media file")
    info = json.loads(result.stdout)
    if not info.get("streams"):
        raise InvalidMedia("File has no media streams")
    return info


def probe(path: Path) -> dict:
    stat = path.stat()
    return _probe(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def same_video_stream(first: Path, second: Path) -> bool:
    """Verify that two variants carry the same encoded video, ignoring audio and container."""
    def fingerprint(path):
        video = next(stream for stream in probe(path)['streams']
                     if stream.get('codec_type') == 'video' and not stream.get('disposition', {}).get('attached_pic'))
        codec = video.get('codec_name')
        normalize = ['-bsf:v', f'{codec}_mp4toannexb'] if codec in {'h264', 'hevc'} else []
        result = run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:V:0',
                      '-c:v', 'copy', *normalize, '-f', 'hash', '-'], capture_output=True, text=True,
                     check=True, timeout=30)
        return codec, result.stdout.strip()

    try:
        expected = fingerprint(first)
        return bool(expected[1]) and expected == fingerprint(second)
    except (RuntimeError, OSError, subprocess.SubprocessError, StopIteration):
        return False


def restore_variant_audio(best: Path, audible: Path, dest: Path) -> Path:
    """Keep the larger video while borrowing audio from the same item's variant.

    Call only for variants belonging to one exact media item. Different timelines
    cannot safely share audio; in that case retain the complete audible variant.
    """
    first, second = probe(best), probe(audible)
    def video(info):
        return next((s for s in info['streams'] if s.get('codec_type') == 'video'
                     and not s.get('disposition', {}).get('attached_pic')), {})
    one, two = video(first), video(second)
    if (one.get('width', 0) * one.get('height', 0) <= two.get('width', 0) * two.get('height', 0)
            or not any(s.get('codec_type') == 'audio' for s in second['streams'])):
        return audible
    duration = float(first.get('format', {}).get('duration') or 0)
    other_duration = float(second.get('format', {}).get('duration') or 0)
    if duration <= 0 or abs(duration - other_duration) > max(.25, duration * .01):
        return audible
    dest.mkdir(parents=True, exist_ok=True)
    output = dest / 'restored.mkv'
    try:
        run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(best), '-i', str(audible),
             '-map', '0:V:0', '-map', '1:a:0', '-c', 'copy', '-shortest', str(output)],
            capture_output=True, check=True, timeout=900)
        final = probe(output)
        if (not any(s.get('codec_type') == 'audio' for s in final['streams'])
                or abs(float(final['format'].get('duration') or 0) - duration) > max(.25, duration * .01)):
            raise RuntimeError('Restoring audio changed the video duration')
        return output
    except (RuntimeError, OSError, subprocess.SubprocessError):
        output.unlink(missing_ok=True)
        return audible


def media_kind(path: Path) -> str:
    info = probe(path)
    streams = info["streams"]
    video = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audio = any(s.get("codec_type") == "audio" for s in streams)
    container = info.get('format', {})
    fmt = container.get('format_name', '')
    brand = (container.get('tags', {}).get('major_brand') or '').strip()
    if video:
        frames = str(video[0].get("nb_frames") or "1")
        if 'gif' in fmt or 'apng' in fmt or brand in {'avis', 'msf1'}:
            return "animation"
        if video[0].get('codec_name') == 'webp':
            if (frames.isdigit() and int(frames) > 1) or animated_webp(path):
                return 'animation'
        if not audio and brand in {'avif', 'mif1', 'heic', 'heix', 'hevc', 'hevx'}:
            return 'image'
        if not audio and ("image2" in fmt or "_pipe" in fmt or video[0].get("codec_name") in {"png", "mjpeg", "webp"}):
            if float(info.get("format", {}).get("duration", 0) or 0) <= 0.1:
                return "image"
        return "video"
    if audio:
        return "audio"
    raise RuntimeError("Unsupported media streams")


def animated_webp(path: Path) -> bool:
    # WebP's VP8X feature byte is available even on ffprobe builds that only
    # recognize the container and cannot decode its animated frames.
    with path.open('rb') as media:
        header = media.read(21)
    return (len(header) == 21 and header[:4] == b'RIFF' and header[8:16] == b'WEBPVP8X'
            and bool(header[20] & 0x02))


def webp_video(path: Path, dest: Path) -> Path:
    """Decode complete WebP frames and retain their order and timestamps."""
    from PIL import Image

    dest.mkdir(parents=True, exist_ok=True)
    output = dest / (path.stem + '_animation.mp4')
    with Image.open(path) as image, tempfile.TemporaryDirectory(dir=dest) as directory:
        root = Path(directory)
        with (root / 'frames.ffconcat').open('w', encoding='utf-8') as manifest:
            manifest.write('ffconcat version 1.0\n')
            for index in range(image.n_frames):
                image.seek(index)
                image.load()
                frame = Image.new('RGBA', image.size, (0, 0, 0, 255))
                frame.alpha_composite(image.convert('RGBA'))
                filename = f'{index:06d}.png'
                frame.convert('RGB').save(root / filename, compress_level=1)
                duration = max(1, image.info.get('duration', 100)) / 1000
                manifest.write(f'file {filename}\noption framerate 1000\nduration {duration:.3f}\n')
            manifest.write(f'file {filename}\noption framerate 1000\n')
        run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'concat', '-safe', '0',
             '-i', str(root / 'frames.ffconcat'), '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
             '-fps_mode', 'vfr', '-enc_time_base', '1/1000', '-movflags', '+faststart', str(output)],
            capture_output=True, check=True, timeout=900)
    if not streamable(output):
        raise RuntimeError('Could not prepare WebP animation')
    return output


def streamable(path: Path) -> bool:
    info = probe(path)
    videos = [s for s in info['streams'] if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
    audios = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    return (path.suffix.lower() == '.mp4' and 'mp4' in info['format'].get('format_name', '').split(',') and bool(videos)
            and all(s.get("codec_name") == "h264" and s.get("pix_fmt") in {"yuv420p", "yuvj420p"} for s in videos)
            and all(s.get("codec_name") in {"aac", "mp3"} for s in audios))


def prepare_video(path: Path, dest: Path) -> Path:
    """Remux compatible tracks to MP4 without re-encoding a single frame."""
    info = probe(path)
    codecs = {s.get("codec_name") for s in info["streams"] if s.get("codec_type") in {"video", "audio"}}
    if streamable(path) or not codecs.issubset({'h264', 'aac', 'mp3'}):
        return path
    dest.mkdir(parents=True, exist_ok=True)
    output = dest / (path.stem + ".mp4")
    result = run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
                             "-map", "0:V:0", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart", str(output)],
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
        result = run([
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
            "-map", "0:V?", "-map", "0:a?", "-c", "copy", "-f", "segment",
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


def compatibility_bitrate(path: Path, info: dict, video: dict, limit: int | None) -> int:
    """Bound an already-needed H.264 encode if one upload can retain a healthy bitrate."""
    if not limit or path.stat().st_size > limit:
        return 0
    try:
        duration = float(info.get('format', {}).get('duration') or 0)
        fps = float(Fraction(video.get('avg_frame_rate') or '0'))
        source_rate = int(video.get('bit_rate') or 0)
        audio_rate = sum(
            int(track.get('bit_rate') or 256_000) if track.get('codec_name') in {'aac', 'mp3'} else 256_000
            for track in info['streams'] if track.get('codec_type') == 'audio'
        )
        pixels = int(video.get('width') or 0) * int(video.get('height') or 0)
        if not math.isfinite(duration) or min(duration, pixels, fps) <= 0:
            return 0
        if not source_rate:
            source_rate = max(0, path.stat().st_size * 8 / duration - audio_rate)
        # Reserve space for the MP4 container and one second of VBV headroom.
        ceiling = int((limit * .95 * 8 - audio_rate * duration) / (duration + 1))
        quality_floor = max(pixels * fps * .04, source_rate * 1.5)
        return ceiling if 0 < quality_floor <= ceiling <= 20_000_000 else 0
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        return 0


def native_video(path: Path, dest: Path, *, upload_limit: int | None = None) -> Path:
    """Prefer remuxing; encode incompatible codecs for Telegram video playback."""
    if animated_webp(path):
        return webp_video(path, dest)
    info = probe(path)
    streams = info['streams']
    audio_count = sum(s.get('codec_type') == 'audio' for s in streams)
    final = prepare_video(path, dest)
    if streamable(final):
        if sum(s.get('codec_type') == 'audio' for s in probe(final)['streams']) != audio_count:
            raise RuntimeError('Video audio track was lost during remux')
        return final
    dest.mkdir(parents=True, exist_ok=True)
    output = dest / (path.stem + "_playable.mp4")
    video = next(s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic"))
    copy_video = video.get("codec_name") == "h264" and video.get("pix_fmt") in {"yuv420p", "yuvj420p"}
    video_options = ["-c:v", "copy"] if copy_video else [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p",
    ]
    if not copy_video and (bitrate := compatibility_bitrate(path, info, video, upload_limit)):
        video_options.extend(['-maxrate:v', str(bitrate), '-bufsize:v', str(bitrate)])
    audio_options = ['-c:a', 'copy']
    for index, audio in enumerate(s for s in streams if s.get('codec_type') == 'audio'):
        if audio.get('codec_name') not in {'aac', 'mp3'}:
            audio_options.extend([f'-c:a:{index}', 'aac', f'-b:a:{index}', '256k'])
    result = run([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "2", "-filter_threads", "1", "-i", str(path),
        "-map", f"0:{video['index']}", "-map", "0:a?", *video_options,
        "-threads:v", "2", *audio_options, "-movflags", "+faststart", str(output),
    ], capture_output=True, timeout=1800)
    if result.returncode or not streamable(output) or sum(
            s.get('codec_type') == 'audio' for s in probe(output)['streams']) != audio_count:
        raise RuntimeError("Could not prepare playable Telegram video")
    return output


def native_audio(path: Path, dest: Path) -> Path:
    """Choose the audio container from bytes, never just the CDN filename."""
    info = probe(path)
    track = next(s for s in info['streams'] if s.get('codec_type') == 'audio')
    codec = track.get('codec_name')
    fmt = info.get('format', {}).get('format_name', '').split(',')
    if codec == 'mp3' and path.suffix.lower() == '.mp3' and 'mp3' in fmt:
        return path
    if codec == 'aac' and path.suffix.lower() == '.m4a' and 'mov' in fmt:
        return path
    suffix = '.m4a' if codec == 'aac' else '.mp3'
    options = ['-c:a', 'copy'] if codec in {'aac', 'mp3'} else ['-c:a', 'libmp3lame', '-b:a', '320k']
    dest.mkdir(parents=True, exist_ok=True)
    output = dest / ('audio_native' + suffix)
    result = run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(path), '-map', '0:a:0', '-vn',
                  *options, str(output)], capture_output=True, timeout=900)
    if result.returncode:
        raise RuntimeError('Could not prepare playable audio')
    probe(output)
    return output
