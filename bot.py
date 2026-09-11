#!/usr/bin/env python3
"""Veltrix Downloader backend.

Public-media downloader for YouTube, Instagram, Snapchat and Pinterest.
Optimized for Telegram hosted Bot API limits and small Render instances.
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import math
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL

load_dotenv()
VERSION = "3.0.0"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PROXY = (os.getenv("PROXY") or os.getenv("HTTPS_PROXY") or "").strip()
MAX_BYTES = int(os.getenv("TELEGRAM_MAX_BYTES", str(48 * 1024 * 1024)))
MAX_SOURCE_BYTES = int(os.getenv("MAX_SOURCE_BYTES", str(1024 * 1024 * 1024)))
MAX_PHOTO_BYTES = int(os.getenv("MAX_PHOTO_BYTES", str(9 * 1024 * 1024)))
MAX_GALLERY_ITEMS = max(1, min(int(os.getenv("MAX_GALLERY_ITEMS", "20")), 40))
MAX_CONCURRENT_JOBS = max(1, min(int(os.getenv("MAX_CONCURRENT_JOBS", "1")), 3))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
USERS_FILE = DATA_DIR / "users.json"

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("veltrix")
_user_lock = Lock()
_job_locks: dict[int, asyncio.Lock] = {}
_global_sem: asyncio.Semaphore | None = None

URL_RE = re.compile(r"(https?://[^\s<>\"']+)|(www\.[^\s<>\"']+)", re.I)
YT_ID_RE = re.compile(r"(?:v=|/shorts/|/live/|youtu\.be/)([A-Za-z0-9_-]{11})")
PLATFORMS = {
    "youtube": {"label": "YouTube", "hosts": ("youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com")},
    "instagram": {"label": "Instagram", "hosts": ("instagram.com", "instagr.am")},
    "snapchat": {"label": "Snapchat", "hosts": ("snapchat.com", "snap.com")},
    "pinterest": {"label": "Pinterest", "hosts": ("pinterest.com", "pinterest.co", "pin.it")},
}
VIDEO_PRESETS = {
    "best": {"label": "Best", "height": 4320},
    "2160": {"label": "4K", "height": 2160},
    "1440": {"label": "1440p", "height": 1440},
    "1080": {"label": "1080p", "height": 1080},
    "720": {"label": "720p", "height": 720},
    "480": {"label": "480p", "height": 480},
    "360": {"label": "360p", "height": 360},
}
AUDIO_PRESETS = {
    "mp3_320": {"label": "MP3 320", "codec": "mp3", "bitrate": "320"},
    "mp3_192": {"label": "MP3 192", "codec": "mp3", "bitrate": "192"},
    "mp3_128": {"label": "MP3 128", "codec": "mp3", "bitrate": "128"},
    "m4a": {"label": "M4A", "codec": "m4a", "bitrate": "192"},
}
DEFAULT_QUALITY = "720"
AUTO_MODE = "auto"


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg"):
        return
    try:
        import imageio_ffmpeg
        src = Path(imageio_ffmpeg.get_ffmpeg_exe())
        bindir = Path("/tmp/veltrix-bin")
        bindir.mkdir(parents=True, exist_ok=True)
        dst = bindir / "ffmpeg"
        if not dst.exists():
            try:
                dst.symlink_to(src)
            except OSError:
                shutil.copy2(src, dst)
                dst.chmod(0o755)
        os.environ["PATH"] = f"{bindir}:{os.environ.get('PATH', '')}"
    except Exception as exc:
        log.warning("ffmpeg unavailable: %s", exc)


def deno_runtime() -> str | None:
    try:
        import deno
        path = str(deno.find_deno_bin())
        return path if Path(path).exists() else None
    except Exception as exc:
        log.warning("Deno unavailable: %s", exc)
        return None


ensure_ffmpeg()
DENO_BIN = deno_runtime()


def global_sem() -> asyncio.Semaphore:
    global _global_sem
    if _global_sem is None:
        _global_sem = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    return _global_sem


def job_lock(uid: int) -> asyncio.Lock:
    if uid not in _job_locks:
        _job_locks[uid] = asyncio.Lock()
    return _job_locks[uid]


def load_users() -> dict:
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8")) if USERS_FILE.exists() else {}
    except Exception:
        return {}


def save_users(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(USERS_FILE)


def user_row(uid: int) -> dict:
    with _user_lock:
        row = load_users().get(str(uid)) or {}
        quality = row.get("quality") or DEFAULT_QUALITY
        if quality not in VIDEO_PRESETS:
            quality = DEFAULT_QUALITY
        return {"quality": quality, "last_url": row.get("last_url") or ""}


def patch_user(uid: int, **fields: str) -> dict:
    with _user_lock:
        users = load_users()
        row = users.get(str(uid)) or {}
        row.update({k: v for k, v in fields.items() if v is not None})
        if row.get("quality") not in VIDEO_PRESETS:
            row["quality"] = DEFAULT_QUALITY
        users[str(uid)] = row
        save_users(users)
        return row


def extract_url(text: str) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text.strip())
    if not match:
        return None
    url = match.group(0).rstrip(").,]}>\"'")
    if url.startswith("www."):
        url = "https://" + url
    yt = YT_ID_RE.search(url)
    if yt:
        return f"https://www.youtube.com/watch?v={yt.group(1)}"
    return url


def platform_of(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.netloc or "").split("@")[-1].split(":")[0].lower().removeprefix("www.")
    for key, meta in PLATFORMS.items():
        if any(host == suffix or host.endswith("." + suffix) for suffix in meta["hosts"]):
            return key
    return None


def platform_label(url: str) -> str:
    key = platform_of(url)
    return PLATFORMS[key]["label"] if key else "Unsupported"


def extractor_candidates(url: str) -> list[str]:
    """Return clean public URL variants without leaking tracking parameters."""
    platform = platform_of(url)
    out = [url]
    try:
        parsed = urlparse(url)
        if platform == "instagram":
            match = re.search(r"/(reel|reels|p|tv)/([A-Za-z0-9_-]+)", parsed.path, re.I)
            if match:
                kind = "reel" if match.group(1).lower() in {"reel", "reels"} else match.group(1).lower()
                out.append(f"https://www.instagram.com/{kind}/{match.group(2)}/")
        elif platform == "snapchat":
            match = re.search(r"/spotlight/([A-Za-z0-9_]+)", parsed.path, re.I)
            if match:
                snap_id = match.group(1)
                out.append(f"https://www.snapchat.com/spotlight/{snap_id}")
                out.append(f"https://snapchat.com/spotlight/{snap_id}")
    except Exception:
        pass
    return list(dict.fromkeys(out))


def public_page_candidates(url: str) -> list[str]:
    out = extractor_candidates(url)
    if platform_of(url) == "instagram":
        try:
            parsed = urlparse(url)
            match = re.search(r"/(reel|reels|p|tv)/([A-Za-z0-9_-]+)", parsed.path, re.I)
            if match:
                kind = "reel" if match.group(1).lower() in {"reel", "reels"} else match.group(1).lower()
                code = match.group(2)
                out.extend([
                    f"https://www.instagram.com/{kind}/{code}/embed/",
                    f"https://www.instagram.com/{kind}/{code}/embed/captioned/",
                ])
        except Exception:
            pass
    return list(dict.fromkeys(out))


def safe_remote_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except ValueError:
        return False


def format_duration(value: Any) -> str:
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return "—"
    if seconds <= 0:
        return "—"
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def base_ydl_opts(tmpdir: str) -> dict[str, Any]:
    out = Path(tmpdir) / "ytdl"
    out.mkdir(parents=True, exist_ok=True)
    opts: dict[str, Any] = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 6,
        "fragment_retries": 6,
        "extractor_retries": 3,
        "socket_timeout": 25,
        "concurrent_fragment_downloads": 4,
        "outtmpl": str(out / "%(extractor)s_%(id)s_%(title).80B.%(ext)s"),
        "restrictfilenames": True,
        "overwrites": True,
        "cachedir": False,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Mobile Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if PROXY:
        opts["proxy"] = PROXY
    if DENO_BIN:
        opts["js_runtimes"] = {"deno": {"path": DENO_BIN}}
    return opts


def probe_media(url: str) -> dict[str, Any]:
    tmp = tempfile.mkdtemp(prefix="vx_probe_")
    try:
        opts = base_ydl_opts(tmp)
        opts["skip_download"] = True
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        if info.get("_type") in {"playlist", "multi_video"}:
            entries = [e for e in (info.get("entries") or []) if e]
            if len(entries) == 1:
                info = entries[0]
        return {
            "title": str(info.get("title") or info.get("description") or "Media")[:180],
            "duration": info.get("duration") or 0,
            "format_count": len(info.get("formats") or []),
            "thumbnail": str(info.get("thumbnail") or ""),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def preview_media(url: str) -> dict[str, Any]:
    """Best-effort preview metadata. Never blocks the actual download path."""
    try:
        return probe_media(url)
    except Exception as first:
        log.info("preview extractor unavailable: %s", str(first)[:160])
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Mobile Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for page_url in public_page_candidates(url):
        try:
            with httpx.Client(headers=headers, follow_redirects=True, timeout=12, proxy=PROXY or None) as client:
                response = client.get(page_url)
                response.raise_for_status()
            title = ""
            title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text, flags=re.I | re.S)
            if title_match:
                title = re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip()
            thumbs = og_values(response.text, {"og:image", "og:image:url", "twitter:image"})
            return {
                "title": title[:180] or "Media",
                "duration": 0,
                "format_count": 0,
                "thumbnail": urljoin(str(response.url), thumbs[0]) if thumbs else "",
            }
        except Exception as exc:
            log.info("preview page unavailable: %s", str(exc)[:120])
    return {"title": "Media", "duration": 0, "format_count": 0, "thumbnail": ""}


def files_in(root: Path) -> list[Path]:
    if not root.exists():
        return []
    skip = {".json", ".vtt", ".srt", ".part", ".ytdl", ".nfo", ".txt"}
    result = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() not in skip and p.stat().st_size > 0]
    result.sort(key=lambda p: (p.stat().st_size, p.name), reverse=True)
    return result


def video_chain(height: int) -> list[str]:
    if height >= 4000:
        return ["bv*+ba/b", "best", "b"]
    return [
        f"bv*[height<={height}]+ba/b[height<={height}]",
        f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/b[height<={height}][ext=mp4]",
        f"best[height<={height}]",
        "18",
        "best",
        "b",
    ]


def download_ytdlp(url: str, mode: str, tmpdir: str) -> list[Path]:
    base = base_ydl_opts(tmpdir)
    attempts: list[tuple[str, dict[str, Any]]] = []
    if mode == AUTO_MODE:
        # Preference: 720p -> 1080p -> best <=720 -> 480p -> 360p -> any best.
        # Social platforms often expose only one combined stream, so each rung
        # includes both split A/V and combined-file fallbacks.
        auto_chain = [
            "bv*[height=720]+ba/b[height=720]",
            "bv*[height=1080]+ba/b[height=1080]",
            "bv*[height<=720]+ba/b[height<=720]",
            "best[height<=720]",
            "bv*[height=480]+ba/b[height=480]",
            "best[height<=480]",
            "bv*[height=360]+ba/b[height=360]",
            "best[height<=360]",
            "18",
            "best",
            "b",
        ]
        attempts = [(fmt, {}) for fmt in auto_chain]
    elif mode == "original":
        attempts = [("best", {}), ("bv*+ba/b", {})]
    elif mode in VIDEO_PRESETS:
        attempts = [(fmt, {}) for fmt in video_chain(int(VIDEO_PRESETS[mode]["height"]))]
    elif mode in AUDIO_PRESETS:
        preset = AUDIO_PRESETS[mode]
        pp = {
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": str(preset["codec"]),
                "preferredquality": str(preset["bitrate"]),
            }]
        }
        attempts = [("bestaudio/ba/best", pp), ("18/best", pp)]
    else:
        raise ValueError("Unknown download mode")

    last: Exception | None = None
    seen: set[tuple[str, str]] = set()
    for candidate in extractor_candidates(url):
        for fmt, extra in attempts:
            sig = (candidate, fmt)
            if sig in seen:
                continue
            seen.add(sig)
            try:
                opts = dict(base)
                opts["format"] = fmt
                opts.update(extra)
                with YoutubeDL(opts) as ydl:
                    ydl.download([candidate])
                files = files_in(Path(tmpdir) / "ytdl")
                if files:
                    return files
            except Exception as exc:
                last = exc
                log.warning("yt-dlp %s on %s failed: %s", fmt, platform_label(candidate), str(exc)[:220])
    if last:
        raise last
    return []


def download_gallery(url: str, tmpdir: str) -> list[Path]:
    if platform_of(url) not in {"instagram", "pinterest"}:
        return []
    dest = Path(tmpdir) / "gallery"
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["python", "-m", "gallery_dl"]
    if PROXY:
        cmd.extend(["--proxy", PROXY])
    cmd.extend(["-d", str(dest), "--no-mtime", "--range", f"1-{MAX_GALLERY_ITEMS}", url])
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
        if proc.returncode != 0:
            log.warning("gallery-dl: %s", (proc.stderr or "")[-250:])
    except Exception as exc:
        log.warning("gallery-dl error: %s", exc)
    return files_in(dest)


def og_values(page: str, names: set[str]) -> list[str]:
    values: list[str] = []
    for tag in re.findall(r"<meta\b[^>]*>", page, flags=re.I):
        attrs = dict(re.findall(r"([\w:-]+)\s*=\s*[\"']([^\"']*)[\"']", tag, flags=re.I))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        value = attrs.get("content") or ""
        if key in names and value:
            values.append(html.unescape(value))
    return values


def download_open_graph(url: str, tmpdir: str) -> list[Path]:
    """Public-page fallback, useful for share pages with direct OG media."""
    if not platform_of(url):
        return []
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Mobile Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    proxy = PROXY or None
    with httpx.Client(headers=headers, follow_redirects=True, timeout=25, proxy=proxy) as client:
        response = client.get(url)
        response.raise_for_status()
        if "text/html" not in response.headers.get("content-type", ""):
            return []
        video_names = {"og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"}
        image_names = {"og:image", "og:image:url", "og:image:secure_url", "twitter:image"}
        candidates = og_values(response.text, video_names) + og_values(response.text, image_names)
        dest = Path(tmpdir) / "og"
        dest.mkdir(parents=True, exist_ok=True)
        results: list[Path] = []
        for index, raw in enumerate(dict.fromkeys(candidates), 1):
            media_url = urljoin(str(response.url), raw)
            if not safe_remote_url(media_url):
                continue
            try:
                with client.stream("GET", media_url, headers={"Referer": str(response.url)}) as media:
                    media.raise_for_status()
                    length = int(media.headers.get("content-length") or 0)
                    if length and length > MAX_SOURCE_BYTES:
                        continue
                    content_type = media.headers.get("content-type", "").split(";", 1)[0]
                    ext = mimetypes.guess_extension(content_type) or Path(urlparse(media_url).path).suffix or ".bin"
                    if ext == ".jpe":
                        ext = ".jpg"
                    out = dest / f"media_{index:02d}{ext}"
                    total = 0
                    with out.open("wb") as fh:
                        for chunk in media.iter_bytes(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_SOURCE_BYTES:
                                raise RuntimeError("media too large")
                            fh.write(chunk)
                    if out.stat().st_size > 0:
                        results.append(out)
            except Exception as exc:
                log.info("OG media failed: %s", str(exc)[:160])
        return results[:MAX_GALLERY_ITEMS]


def friendly_error(platform: str, exc: Exception) -> str:
    """User-safe errors only. Full extractor details stay in logs."""
    text = str(exc)
    low = text.lower()
    label = PLATFORMS.get(platform, {}).get("label", "Media")
    if any(x in low for x in ("login", "sign in", "cookies", "private", "empty media response")):
        return f"{label}: this post currently requires access the bot does not have."
    if "404" in low or "not found" in low:
        return f"{label}: this link is unavailable, expired, or its public page changed."
    if "403" in low or "forbidden" in low:
        return f"{label}: the platform blocked this download request."
    if "429" in low or "too many" in low or "rate-limit" in low:
        return f"{label}: temporarily rate-limited. Try again later."
    if "unsupported url" in low:
        return f"{label}: this link type is not supported yet."
    if "telegram" in low and ("limit" in low or "fit" in low or "over" in low):
        return "Telegram upload limit prevented this file from being sent."
    return f"{label}: couldn't download this media right now."


def grab(url: str, mode: str, tmpdir: str) -> list[Path]:
    platform = platform_of(url)
    if not platform:
        raise RuntimeError("Only YouTube, Instagram, Snapchat and Pinterest links are supported.")
    Path(tmpdir).mkdir(parents=True, exist_ok=True)
    first_error: Exception | None = None
    files: list[Path] = []
    try:
        files = download_ytdlp(url, mode, tmpdir)
    except Exception as exc:
        first_error = exc
    if not files and platform in {"instagram", "pinterest"} and mode in {"original", AUTO_MODE, *VIDEO_PRESETS}:
        files = download_gallery(url, tmpdir)
    if not files:
        for page_url in public_page_candidates(url):
            try:
                files = download_open_graph(page_url, tmpdir)
                if files:
                    break
            except Exception as exc:
                if first_error is None:
                    first_error = exc
    if not files:
        if first_error:
            raise RuntimeError(friendly_error(platform, first_error)) from first_error
        raise RuntimeError("No public downloadable media was found in this link.")
    usable = [p for p in files if p.stat().st_size <= MAX_SOURCE_BYTES]
    if not usable:
        raise RuntimeError(f"Source file is over the {MAX_SOURCE_BYTES // 1048576} MB service cap.")
    return usable[:MAX_GALLERY_ITEMS]


def classify(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
        return "image"
    if ext in {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac"}:
        return "audio"
    return "video"


def ffmpeg(cmd: list[str], timeout: int = 900) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "")[-400:] or "ffmpeg failed")


def media_duration(path: Path) -> float:
    proc = subprocess.run(["ffmpeg", "-i", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=30)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
    if not match:
        return 0.0
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def fit_video(path: Path, dest: Path, max_height: int) -> Path:
    if path.stat().st_size <= MAX_BYTES:
        return path
    dest.mkdir(parents=True, exist_ok=True)
    duration = max(media_duration(path), 1.0)
    total_bps = max(int((MAX_BYTES * 8 * 0.92) / duration), 220_000)
    audio_bps = 96_000
    video_bps = max(total_bps - audio_bps, 120_000)
    height = max_height if max_height <= 2160 else 2160
    if video_bps < 2_500_000:
        height = min(height, 1080)
    if video_bps < 1_200_000:
        height = min(height, 720)
    if video_bps < 700_000:
        height = min(height, 480)
    if video_bps < 350_000:
        height = min(height, 360)
    ladder = [(height, video_bps), (min(height, 1080), int(video_bps * .82)), (min(height, 720), int(video_bps * .68)), (min(height, 480), int(video_bps * .52)), (360, max(int(video_bps * .42), 140_000))]
    out = dest / f"{path.stem}.telegram.mp4"
    seen: set[tuple[int, int]] = set()
    for h, bitrate in ladder:
        if (h, bitrate) in seen:
            continue
        seen.add((h, bitrate))
        ffmpeg(["ffmpeg", "-y", "-i", str(path), "-vf", f"scale=-2:'min({h},ih)'", "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2), "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-pix_fmt", "yuv420p", str(out)])
        if out.exists() and out.stat().st_size <= MAX_BYTES:
            return out
    return out if out.exists() else path


def fit_audio(path: Path, dest: Path, codec: str, kbps: int) -> Path:
    good_ext = ".m4a" if codec == "m4a" else ".mp3"
    if path.stat().st_size <= MAX_BYTES and path.suffix.lower() == good_ext:
        return path
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{path.stem}{good_ext}"
    for rate in dict.fromkeys([kbps, 320, 192, 160, 128, 96, 64]):
        if codec == "m4a":
            cmd = ["ffmpeg", "-y", "-i", str(path), "-vn", "-c:a", "aac", "-b:a", f"{min(rate, 256)}k", str(out)]
        else:
            cmd = ["ffmpeg", "-y", "-i", str(path), "-vn", "-c:a", "libmp3lame", "-b:a", f"{rate}k", str(out)]
        ffmpeg(cmd)
        if out.exists() and out.stat().st_size <= MAX_BYTES:
            return out
    return out if out.exists() else path


def split_video(path: Path, dest: Path) -> list[Path]:
    if path.stat().st_size <= MAX_BYTES:
        return [path]
    duration = media_duration(path)
    if duration <= 0:
        return [path]
    dest.mkdir(parents=True, exist_ok=True)
    count = max(2, math.ceil(path.stat().st_size / (MAX_BYTES * 0.88)))
    segment = max(10, int(duration / count))
    pattern = str(dest / f"{path.stem}.part%02d.mp4")
    ffmpeg(["ffmpeg", "-y", "-i", str(path), "-c", "copy", "-map", "0", "-f", "segment", "-segment_time", str(segment), "-reset_timestamps", "1", pattern])
    return sorted(p for p in dest.glob(f"{path.stem}.part*.mp4") if p.stat().st_size > 0)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Video", callback_data="menu|video"), InlineKeyboardButton("🎵 Audio", callback_data="menu|audio")], [InlineKeyboardButton("📦 Original media", callback_data="dl|original")]])


def video_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Best", callback_data="dl|best"), InlineKeyboardButton("4K", callback_data="dl|2160"), InlineKeyboardButton("1440p", callback_data="dl|1440")], [InlineKeyboardButton("1080p", callback_data="dl|1080"), InlineKeyboardButton("720p", callback_data="dl|720")], [InlineKeyboardButton("480p", callback_data="dl|480"), InlineKeyboardButton("360p", callback_data="dl|360")], [InlineKeyboardButton("‹ Back", callback_data="menu|main")]])


def audio_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("MP3 320", callback_data="dl|mp3_320"), InlineKeyboardButton("MP3 192", callback_data="dl|mp3_192")], [InlineKeyboardButton("MP3 128", callback_data="dl|mp3_128"), InlineKeyboardButton("M4A", callback_data="dl|m4a")], [InlineKeyboardButton("‹ Back", callback_data="menu|main")]])


def settings_kb(current: str) -> InlineKeyboardMarkup:
    def label(key: str) -> str:
        return ("• " if key == current else "") + VIDEO_PRESETS[key]["label"]
    return InlineKeyboardMarkup([[InlineKeyboardButton(label("1080"), callback_data="set|1080"), InlineKeyboardButton(label("720"), callback_data="set|720")], [InlineKeyboardButton(label("480"), callback_data="set|480"), InlineKeyboardButton(label("360"), callback_data="set|360")]])


def link_caption(url: str, meta: dict[str, Any] | None) -> str:
    label = platform_label(url)
    if not meta:
        return f"✅ {label} link\nChoose download type."
    title = (meta.get("title") or "Media").strip()
    duration = format_duration(meta.get("duration"))
    extra = f"\n⏱ {duration}" if duration != "—" else ""
    return f"✅ {label}\n{title}{extra}\n\nChoose download type."


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "⚡ Veltrix Downloader\n\n"
        "YouTube • Instagram • Snapchat • Pinterest\n"
        "Send a media link — I download it automatically."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📥 Send a YouTube, Instagram, Snapchat or Pinterest media link.\n"
        "🎬 Automatic quality: prefers 720p, then 1080p, then lower fallbacks.\n"
        "🎵 After a video is sent, tap MP3 to get audio."
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("⚙️ Quality is automatic. No setup needed.")


async def make_downloading_status(msg, url: str, platform: str, meta: dict[str, Any]):
    label = PLATFORMS[platform]["label"]
    title = str(meta.get("title") or "").strip()
    caption = f"⬇️ Downloading from {label}…"
    if title and title != "Media":
        caption += f"\n{title[:160]}"
    thumb = str(meta.get("thumbnail") or "").strip()
    if thumb and safe_remote_url(thumb):
        try:
            return await msg.reply_photo(photo=thumb, caption=caption)
        except Exception as exc:
            log.info("thumbnail preview failed: %s", str(exc)[:120])
    return await msg.reply_text(caption)


async def edit_status(status, text: str, reply_markup=None) -> None:
    try:
        if getattr(status, "photo", None):
            await status.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await status.edit_text(text, reply_markup=reply_markup)
    except TelegramError:
        pass


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    uid = update.effective_user.id
    url = extract_url(msg.text or msg.caption or "")
    if not url:
        return
    platform = platform_of(url)
    if not platform:
        return
    patch_user(uid, last_url=url)

    try:
        meta = await asyncio.wait_for(asyncio.to_thread(preview_media, url), timeout=18)
    except Exception:
        meta = {"title": "Media", "thumbnail": ""}

    status = await make_downloading_status(msg, url, platform, meta)
    await run_job(msg, context, uid, url, AUTO_MODE, status)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id
    # New UX exposes only one action after a successful video: MP3.
    if data != "dl|mp3_192":
        return
    mode = "mp3_192"
    url = user_row(uid).get("last_url") or ""
    if not url:
        await q.edit_message_text("Send the link again.")
        return
    await edit_status(q.message, "⬇️ Converting to MP3…")
    await run_job(q.message, context, uid, url, mode, q.message)


async def send_images(msg, paths: list[Path], caption: str) -> int:
    sent = 0
    batch: list[Path] = []
    for image in paths:
        if image.stat().st_size > MAX_PHOTO_BYTES:
            with image.open("rb") as fh:
                await msg.reply_document(document=fh, caption=caption if sent == 0 else None, filename=image.name)
            sent += 1
            continue
        batch.append(image)
        if len(batch) == 10:
            sent += await send_album(msg, batch, caption if sent == 0 else "")
            batch = []
    if len(batch) == 1:
        with batch[0].open("rb") as fh:
            await msg.reply_photo(photo=fh, caption=caption if sent == 0 else None)
        sent += 1
    elif batch:
        sent += await send_album(msg, batch, caption if sent == 0 else "")
    return sent


async def send_album(msg, paths: list[Path], caption: str) -> int:
    handles = []
    try:
        media = []
        for index, path in enumerate(paths):
            fh = path.open("rb")
            handles.append(fh)
            media.append(InputMediaPhoto(media=fh, caption=caption if index == 0 and caption else None))
        await msg.reply_media_group(media=media)
        return len(paths)
    finally:
        for fh in handles:
            try:
                fh.close()
            except Exception:
                pass


async def send_video(msg, path: Path, caption: str, max_height: int, tmp: Path, reply_markup=None) -> int:
    final = path
    if final.stat().st_size > MAX_BYTES:
        final = await asyncio.to_thread(fit_video, final, tmp / "fit", max_height)
    if final.stat().st_size <= MAX_BYTES:
        with final.open("rb") as fh:
            try:
                await msg.reply_video(video=fh, caption=caption, filename=final.name, supports_streaming=True, reply_markup=reply_markup)
            except TelegramError:
                fh.seek(0)
                await msg.reply_document(document=fh, caption=caption, filename=final.name, reply_markup=reply_markup)
        return 1
    parts = await asyncio.to_thread(split_video, final, tmp / "parts")
    if not parts or any(p.stat().st_size > MAX_BYTES for p in parts):
        raise RuntimeError("Could not fit this video under Telegram's hosted bot upload limit.")
    for index, part in enumerate(parts, 1):
        with part.open("rb") as fh:
            await msg.reply_video(video=fh, caption=f"{caption}\nPart {index}/{len(parts)}", filename=part.name, supports_streaming=True)
    return len(parts)


async def send_audio(msg, path: Path, caption: str, mode: str, tmp: Path) -> int:
    preset = AUDIO_PRESETS.get(mode) or AUDIO_PRESETS["mp3_192"]
    final = path
    wanted_ext = ".m4a" if preset["codec"] == "m4a" else ".mp3"
    if final.stat().st_size > MAX_BYTES or final.suffix.lower() != wanted_ext:
        final = await asyncio.to_thread(fit_audio, final, tmp / "fit_audio", str(preset["codec"]), int(preset["bitrate"]))
    if final.stat().st_size > MAX_BYTES:
        raise RuntimeError("Audio is still over Telegram's hosted bot upload limit.")
    with final.open("rb") as fh:
        await msg.reply_audio(audio=fh, caption=caption, filename=final.name)
    return 1


async def run_job(msg, context, uid: int, url: str, mode: str, status=None) -> None:
    lock = job_lock(uid)
    if lock.locked():
        await msg.reply_text("⏳ Your previous download is still running.")
        return
    if status is None:
        status = await msg.reply_text("⬇️ Downloading…")
    async def typing() -> None:
        try:
            while True:
                await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_DOCUMENT)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return
    typing_task = asyncio.create_task(typing())
    tmp = Path(tempfile.mkdtemp(prefix="vx_"))
    platform = platform_of(url) or "web"
    async with lock:
        async with global_sem():
            try:
                files = await asyncio.to_thread(grab, url, mode, str(tmp))
                images = [p for p in files if classify(p) == "image"]
                videos = [p for p in files if classify(p) == "video"]
                audios = [p for p in files if classify(p) == "audio"]
                sent = 0
                video_sent = False
                caption = f"⚡ Veltrix Downloader · {PLATFORMS.get(platform, {}).get('label', 'Media')}"
                mp3_button = InlineKeyboardMarkup([[InlineKeyboardButton("🎵 MP3", callback_data="dl|mp3_192")]])

                if mode in AUDIO_PRESETS:
                    sources = audios or videos
                    if not sources:
                        raise RuntimeError("No audio/video stream was found in this post.")
                    for source in sources[:3]:
                        sent += await send_audio(msg, source, caption, mode, tmp)
                elif mode == "original":
                    if images:
                        sent += await send_images(msg, images, caption)
                    for video in videos:
                        sent += await send_video(msg, video, caption, 2160, tmp, mp3_button)
                        video_sent = True
                    for audio in audios:
                        sent += await send_audio(msg, audio, caption, "mp3_192", tmp)
                else:
                    requested = 720 if mode == AUTO_MODE else int(VIDEO_PRESETS[mode]["height"])
                    if images and not videos:
                        sent += await send_images(msg, images, caption)
                    for video in videos:
                        sent += await send_video(msg, video, caption, requested, tmp, mp3_button)
                        video_sent = True
                    if not videos and audios:
                        raise RuntimeError("This media exposes audio only.")
                if not sent:
                    raise RuntimeError("Nothing downloadable was returned.")
                done_markup = mp3_button if video_sent else None
                await edit_status(status, f"✅ Sent · {sent} file{'s' if sent != 1 else ''}", done_markup)
            except Exception as exc:
                log.exception("download job failed")
                safe_message = friendly_error(platform, exc) if platform in PLATFORMS else "Download failed. Please try another link."
                try:
                    await edit_status(status, f"❌ {safe_message}")
                except Exception:
                    await msg.reply_text("❌ Download failed.")
            finally:
                typing_task.cancel()
                shutil.rmtree(tmp, ignore_errors=True)


def start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ok": True, "service": "veltrix-downloader", "version": VERSION, "platforms": list(PLATFORMS), "ffmpeg": bool(shutil.which("ffmpeg")), "deno": bool(DENO_BIN)}).encode()
            self.send_response(200 if self.path in {"/", "/health", "/healthz", "/readyz"} else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, fmt, *args):
            return
    Thread(target=lambda: ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever(), daemon=True).start()


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Veltrix Downloader %s started", VERSION)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
