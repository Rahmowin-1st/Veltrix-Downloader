"""Media inspection and lossless delivery helpers; no network credentials here."""
from __future__ import annotations

import bisect
import json
import math
import os
import subprocess
import tempfile
from media_process import lines, run
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

# Container suffixes that already describe their bytes well enough for
# Telegram and ffmpeg. Anything else (for example Snapchat's ".IRZXSOY" CDN
# names) is renamed after the bytes have been probed.
KNOWN_MEDIA_SUFFIXES = {
    '.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif', '.heic', '.heif', '.bmp', '.tif', '.tiff',
    '.mp4', '.m4v', '.mov', '.mkv', '.webm', '.ts', '.avi', '.flv', '.3gp',
    '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.oga', '.flac', '.wav', '.mka', '.weba',
}
# Codecs that can be copied into an MP4 that Telegram's players understand on
# current clients, when converting to H.264 would cost more than the CPU budget.
MP4_COPY_VIDEO = {'h264', 'hevc', 'vp9', 'av1'}
MAX_ENCODE_SECONDS = float(os.getenv('MAX_ENCODE_SECONDS', '300'))
# Pixels per second one CPU core encodes with libx264 -preset veryfast.
ENCODE_PIXEL_RATE = float(os.getenv('ENCODE_PIXEL_RATE', '50000000'))


class InvalidMedia(RuntimeError):
    """A downloaded rendition did not contain readable media."""


class FileSlice:
    """A seekable part for multipart upload, without duplicating the source on disk."""

    def __init__(self, file, start: int, size: int):
        self.file = file
        self.start = start
        self.size = size
        self.seek(0)

    def tell(self) -> int:
        return self.file.tell() - self.start

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 1:
            offset += self.tell()
        elif whence == 2:
            offset += self.size
        elif whence != 0:
            raise ValueError("Invalid seek origin")
        if not 0 <= offset <= self.size:
            raise ValueError("Offset is outside the upload part")
        self.file.seek(self.start + offset)
        return offset

    def read(self, size: int = -1) -> bytes:
        remaining = self.size - self.tell()
        return self.file.read(remaining if size < 0 else min(size, remaining))


class CountingReader:
    """Report upload progress while httpx streams a file or a FileSlice."""

    def __init__(self, inner, on_read, name: str = ''):
        self.inner = inner
        self.on_read = on_read
        self.name = name or getattr(inner, 'name', '')
        # A real file lets httpx take its length from fstat. A slice must not
        # expose the descriptor because its length is not the whole file.
        if not isinstance(inner, FileSlice) and hasattr(inner, 'fileno'):
            self.fileno = inner.fileno

    def read(self, size: int = -1) -> bytes:
        data = self.inner.read(size)
        if data:
            self.on_read(len(data))
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.inner.seek(offset, whence)

    def tell(self) -> int:
        return self.inner.tell()


@lru_cache(maxsize=256)
def _probe(name: str, size: int, mtime: int) -> dict:
    result = run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", name],
        capture_output=True, text=True, timeout=60,
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


def available_cpus() -> float:
    """CPU share granted by the container, e.g. 0.1 on Render Free."""
    try:
        return max(0.05, float(os.environ['VELTRIX_CPUS']))
    except (KeyError, ValueError):
        pass
    try:
        quota, period = Path('/sys/fs/cgroup/cpu.max').read_text().split()[:2]
        if quota != 'max' and int(period) > 0:
            return max(0.05, int(quota) / int(period))
    except (OSError, ValueError):
        pass
    try:
        quota = int(Path('/sys/fs/cgroup/cpu/cpu.cfs_quota_us').read_text())
        period = int(Path('/sys/fs/cgroup/cpu/cpu.cfs_period_us').read_text())
        if quota > 0 and period > 0:
            return max(0.05, quota / period)
    except (OSError, ValueError):
        pass
    return float(os.cpu_count() or 1)


def _visual_stream(info: dict) -> dict:
    return next((s for s in info['streams'] if s.get('codec_type') == 'video'
                 and not s.get('disposition', {}).get('attached_pic')), {})


def _rate(value) -> float:
    try:
        rate = float(Fraction(str(value or '0')))
    except (ValueError, ZeroDivisionError):
        return 0.0
    return rate if math.isfinite(rate) else 0.0


def duration_of(info: dict) -> float:
    try:
        value = float(info.get('format', {}).get('duration') or 0)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value) or value <= 0:
        try:
            value = max(float(s.get('duration') or 0) for s in info.get('streams') or [{}])
        except (TypeError, ValueError):
            value = 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def video_info(path: Path) -> dict:
    """Display size and duration for Telegram's player, honouring rotation."""
    info = probe(path)
    video = _visual_stream(info)
    width, height = int(video.get('width') or 0), int(video.get('height') or 0)
    rotation = 0
    for item in video.get('side_data_list') or []:
        if isinstance(item, dict) and 'rotation' in item:
            try:
                rotation = int(float(item['rotation']))
            except (TypeError, ValueError):
                pass
    try:
        rotation = rotation or int(video.get('tags', {}).get('rotate') or 0)
    except (TypeError, ValueError):
        pass
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return {'width': width, 'height': height, 'duration': duration_of(info)}


def encode_seconds(info: dict, video: dict) -> float:
    """Estimated wall time of a libx264 conversion on this container's CPU share."""
    pixels = int(video.get('width') or 0) * int(video.get('height') or 0)
    fps = _rate(video.get('avg_frame_rate')) or _rate(video.get('r_frame_rate')) or 30.0
    fps = min(fps, 120.0)
    return pixels * fps * duration_of(info) / (ENCODE_PIXEL_RATE * available_cpus())


def canonical_suffix(path: Path) -> str:
    """Pick the file extension from the container and codecs actually present."""
    info = probe(path)
    names = set(str(info.get('format', {}).get('format_name') or '').split(','))
    codecs = {str(s.get('codec_name') or '') for s in info['streams']}
    brand = str(info.get('format', {}).get('tags', {}).get('major_brand') or '').strip()
    kind = media_kind(path)
    if kind in {'image', 'animation'}:
        if 'gif' in names or 'gif' in codecs:
            return '.gif'
        if brand in {'avif', 'avis'}:
            return '.avif'
        if brand in {'heic', 'heix', 'mif1', 'msf1', 'hevc', 'hevx'}:
            return '.heic'
        for codec, suffix in (('mjpeg', '.jpg'), ('png', '.png'), ('apng', '.png'), ('webp', '.webp'),
                              ('bmp', '.bmp'), ('tiff', '.tiff')):
            if codec in codecs:
                return suffix
    if 'mp3' in names:
        return '.mp3'
    if names & {'mov', 'mp4', 'm4a', '3gp'}:
        return '.m4a' if kind == 'audio' else '.mp4'
    if names & {'matroska', 'webm'}:
        if kind == 'audio':
            return '.webm' if codecs <= {'opus', 'vorbis'} else '.mka'
        return '.webm' if codecs <= {'vp8', 'vp9', 'av1', 'opus', 'vorbis'} else '.mkv'
    for name, suffix in (('ogg', '.ogg'), ('flac', '.flac'), ('wav', '.wav'), ('aac', '.aac'),
                         ('mpegts', '.ts'), ('flv', '.flv'), ('avi', '.avi')):
        if name in names:
            return suffix
    return '.mp4' if kind == 'video' else '.bin'


def with_canonical_suffix(path: Path) -> Path:
    """Rename CDN files such as 'media.IRZXSOY' after the bytes were validated."""
    if path.suffix.lower() in KNOWN_MEDIA_SUFFIXES:
        return path
    try:
        suffix = canonical_suffix(path)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
        return path
    target = path.with_name(path.name[:-len(path.suffix)] + suffix if path.suffix else path.name + suffix)
    if target.exists():
        target = path.with_name(f'{target.stem}_{os.getpid()}{suffix}')
    try:
        path.rename(target)
    except OSError:
        return path
    return target


def same_video_stream(first: Path, second: Path) -> bool:
    """Verify that two variants carry the same encoded video, ignoring audio and container."""
    def fingerprint(path):
        video = next(stream for stream in probe(path)['streams']
                     if stream.get('codec_type') == 'video' and not stream.get('disposition', {}).get('attached_pic'))
        codec = video.get('codec_name')
        normalize = ['-bsf:v', f'{codec}_mp4toannexb'] if codec in {'h264', 'hevc'} else []
        result = run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:V:0',
                      '-c:v', 'copy', *normalize, '-f', 'hash', '-'], capture_output=True, text=True,
                     check=True, timeout=60)
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
            capture_output=True, check=True, timeout=1800)
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
                            capture_output=True, timeout=1800)
    if result.returncode:
        output.unlink(missing_ok=True)
        return path
    return output


def _part_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    return suffix if suffix in {".mp4", ".m4a", ".mp3", ".mkv", ".webm", ".ogg", ".flac"} else ".mkv"


def _segment(path: Path, folder: Path, extra: list[str], suffix: str) -> tuple[int, list[Path]]:
    folder.mkdir(parents=True, exist_ok=True)
    faststart = (['-segment_format_options', 'movflags=+faststart']
                 if suffix in {'.mp4', '.m4a'} else [])
    result = run([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
        "-map", "0:V?", "-map", "0:a?", "-c", "copy", "-f", "segment", *extra,
        *faststart, "-reset_timestamps", "1", str(folder / ("part%05d" + suffix)),
    ], capture_output=True, text=True, timeout=3600)
    return result.returncode, sorted(folder.glob("part*"))


def cut_plan(path: Path, target: int) -> list[float]:
    """Keyframe times that keep every part's packet bytes under ``target``.

    Bytes are accumulated in half-second bins while the packet list is streamed,
    so a three-hour video needs only a few thousand counters in memory.
    """
    info = probe(path)
    anchor = _visual_stream(info) or next((s for s in info['streams'] if s.get('codec_type') == 'audio'), {})
    if 'index' not in anchor:
        return []
    anchor_index = int(anchor['index'])
    bins: dict[int, int] = {}
    keyframes: list[float] = []
    command = ['ffprobe', '-v', 'error', '-show_entries', 'packet=stream_index,pts_time,dts_time,size,flags',
               '-of', 'compact=p=0', str(path)]
    for line in lines(command, timeout=1800):
        fields = dict(item.split('=', 1) for item in line.strip().split('|') if '=' in item)
        try:
            stamp = fields.get('pts_time')
            stamp = float(stamp if stamp not in {None, '', 'N/A'} else fields.get('dts_time'))
            size = int(fields.get('size') or 0)
            index = int(fields.get('stream_index') or -1)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(stamp):
            continue
        bucket = max(0, int(stamp * 2))
        bins[bucket] = bins.get(bucket, 0) + size
        if index == anchor_index and 'K' in fields.get('flags', ''):
            keyframes.append(max(0.0, stamp))
    if not bins:
        return []
    keyframes = sorted(set(keyframes))
    if not keyframes or keyframes[0] > 0.5:
        keyframes.insert(0, 0.0)
    last_bucket = max(bins)
    totals = [0] * (last_bucket + 2)
    for bucket in range(last_bucket + 1):
        totals[bucket + 1] = totals[bucket] + bins.get(bucket, 0)

    def bytes_between(start: float, end: float | None) -> int:
        first = min(len(totals) - 1, max(0, int(start * 2)))
        last = len(totals) - 1 if end is None else min(len(totals) - 1, max(0, int(end * 2)))
        return totals[last] - totals[first]

    cuts: list[float] = []
    start = keyframes[0]
    for index, stamp in enumerate(keyframes[1:], 1):
        following = keyframes[index + 1] if index + 1 < len(keyframes) else None
        if bytes_between(start, following) > target and bytes_between(start, stamp) > 0:
            cuts.append(stamp)
            start = stamp
    return cuts


def lossless_parts(path: Path, dest: Path, limit: int) -> list[Path]:
    """Split on existing keyframes, preserving codecs; reject oversize segments."""
    if path.stat().st_size <= limit:
        return [path]
    duration = float(probe(path).get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("Cannot split a file with unknown duration without quality loss")
    dest.mkdir(parents=True, exist_ok=True)
    suffix = _part_suffix(path)
    # Plan the fewest parts from packet sizes; each part carries its own header.
    target = int(limit * .94)
    for attempt in range(3):
        try:
            cuts = cut_plan(path, target)
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
            break
        if not cuts:
            break
        folder = dest / f"plan{attempt}"
        times = ','.join(f'{max(0.0, cut - 0.0005):.6f}' for cut in cuts)
        code, parts = _segment(path, folder, ['-segment_times', times], suffix)
        if code == 0 and parts and all(0 < p.stat().st_size <= limit for p in parts):
            return parts
        for part in parts:
            part.unlink()
        target = int(target * .85)
    interval = max(0.25, duration / math.ceil(path.stat().st_size / (limit * .70)))
    for attempt in range(5):
        folder = dest / str(attempt)
        code, parts = _segment(path, folder, ['-segment_time', str(interval)], suffix)
        if code == 0 and parts and all(0 < p.stat().st_size <= limit for p in parts):
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


def _audio_options(streams: list[dict]) -> list[str]:
    options = ['-c:a', 'copy']
    for index, audio in enumerate(s for s in streams if s.get('codec_type') == 'audio'):
        if audio.get('codec_name') not in {'aac', 'mp3'}:
            options.extend([f'-c:a:{index}', 'aac', f'-b:a:{index}', '256k'])
    return options


def compatible_remux(path: Path, dest: Path) -> Path | None:
    """Copy HEVC/VP9/AV1 into a fast-start MP4 when a conversion is too costly."""
    info = probe(path)
    streams = info['streams']
    video = _visual_stream(info)
    if video.get('codec_name') not in MP4_COPY_VIDEO:
        return None
    audio_count = sum(s.get('codec_type') == 'audio' for s in streams)
    dest.mkdir(parents=True, exist_ok=True)
    output = dest / (path.stem + '_remux.mp4')
    tag = ['-tag:v', 'hvc1'] if video.get('codec_name') == 'hevc' else []
    result = run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(path), '-map', f"0:{video['index']}",
                  '-map', '0:a?', '-c:v', 'copy', *tag, *_audio_options(streams),
                  '-movflags', '+faststart', str(output)], capture_output=True, timeout=3600)
    try:
        if result.returncode or sum(s.get('codec_type') == 'audio' for s in probe(output)['streams']) != audio_count:
            raise RuntimeError('Remux lost a track')
    except (RuntimeError, OSError):
        output.unlink(missing_ok=True)
        return None
    return output


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
    if not copy_video and encode_seconds(info, video) > MAX_ENCODE_SECONDS:
        # A long HEVC/VP9/AV1 source would occupy a small CPU share for far
        # longer than the delivery deadline. Keep its frames and quality.
        remuxed = compatible_remux(path, dest)
        if remuxed is not None:
            return remuxed
        raise RuntimeError('Converting this video for Telegram playback would take too long')
    video_options = ["-c:v", "copy"] if copy_video else [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p",
    ]
    if not copy_video and (bitrate := compatibility_bitrate(path, info, video, upload_limit)):
        video_options.extend(['-maxrate:v', str(bitrate), '-bufsize:v', str(bitrate)])
    result = run([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "2", "-filter_threads", "1", "-i", str(path),
        "-map", f"0:{video['index']}", "-map", "0:a?", *video_options,
        "-threads:v", "2", *_audio_options(streams), "-movflags", "+faststart", str(output),
    ], capture_output=True, timeout=max(1800, int(MAX_ENCODE_SECONDS * 3)))
    if result.returncode or not streamable(output) or sum(
            s.get('codec_type') == 'audio' for s in probe(output)['streams']) != audio_count:
        raise RuntimeError("Could not prepare playable Telegram video")
    return output


def video_thumbnail(path: Path, dest: Path) -> Path | None:
    """One small JPEG preview (Telegram accepts at most 320 px and 200 kB)."""
    try:
        duration = duration_of(probe(path))
        dest.mkdir(parents=True, exist_ok=True)
        output = dest / (path.stem[:40] + '_thumb.jpg')
        for quality in (4, 8, 14):
            run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss', f'{min(1.0, duration / 3):.2f}', '-i', str(path),
                 '-frames:v', '1', '-vf', "scale='if(gt(iw,ih),320,-2)':'if(gt(iw,ih),-2,320)'",
                 '-q:v', str(quality), str(output)], capture_output=True, check=True, timeout=120)
            if 0 < output.stat().st_size <= 200_000:
                return output
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


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
                  *options, str(output)], capture_output=True, timeout=3600)
    if result.returncode:
        raise RuntimeError('Could not prepare playable audio')
    probe(output)
    return output


def mp3_bitrate(duration: float, limit: int, *, preferred: int = 320) -> int:
    """Highest standard MP3 bitrate (kbps) whose file fits one Telegram upload."""
    rates = [rate for rate in (320, 256, 192, 160, 128) if rate <= preferred] or [128]
    if duration <= 0:
        return rates[0]
    for rate in rates:
        # 2 % container/ID3 allowance.
        if rate * 1000 / 8 * duration * 1.02 <= limit:
            return rate
    return 192 if preferred >= 192 else rates[-1]
