#!/usr/bin/env python3
"""Veltrix Downloader backend.

Public-media downloader for YouTube, Instagram, Snapchat and Pinterest.
Preserves source quality with bounded platform workers and Telegram delivery.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import logging
import math
import mimetypes
import os
import re
import shutil
import socket
import sys
import signal
import subprocess
import tempfile
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, InputMediaPhoto, InputMediaVideo, InputMediaAudio, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, Conflict, InvalidToken, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL
from runtime_jobs import ChatTarget, JobManager, mark_state

load_dotenv()
VERSION = "9.3.3"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PROXY = (os.getenv("PROXY") or os.getenv("HTTPS_PROXY") or "").strip()
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "").rstrip("/")
TELEGRAM_LOCAL = bool(TELEGRAM_API_BASE)
API_UPLOAD_LIMIT = 2_000_000_000 if TELEGRAM_LOCAL else 49_000_000
MAX_BYTES = min(int(os.getenv("TELEGRAM_MAX_BYTES", str(API_UPLOAD_LIMIT))), API_UPLOAD_LIMIT)
JOB_TIMEOUT = int(os.getenv("JOB_TIMEOUT_SECONDS", "1800"))
MAX_SOURCE_BYTES = int(os.getenv("MAX_SOURCE_BYTES", str(4 * 1024 * 1024 * 1024)))
MAX_JOB_BYTES = int(os.getenv("MAX_JOB_BYTES", str(6 * 1024 * 1024 * 1024)))
MAX_PHOTO_BYTES = int(os.getenv("MAX_PHOTO_BYTES", str(9 * 1024 * 1024)))
MAX_GALLERY_ITEMS = max(1, min(int(os.getenv("MAX_GALLERY_ITEMS", "100")), 1000))
MAX_CONCURRENT_JOBS = max(1, min(int(os.getenv("MAX_CONCURRENT_JOBS", "1")), 3))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
USERS_FILE = DATA_DIR / "users.json"
ACTIONS_FILE = DATA_DIR / "actions.json"
CACHE_DIR = DATA_DIR / "cache"
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", str(3600)))
CACHE_MAX_BYTES = int(os.getenv("CACHE_MAX_BYTES", str(384 * 1024 * 1024)))
CACHE_TOTAL_MAX_BYTES = int(os.getenv("CACHE_TOTAL_MAX_BYTES", str(768 * 1024 * 1024)))

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("veltrix")
_user_lock = Lock()
_action_lock = Lock()
_job_locks: dict[int, asyncio.Lock] = {}
_global_sem: asyncio.Semaphore | None = None
_jobs: JobManager | None = None
_instance_lock = None
_metrics_lock = Lock()
_metrics = {
    "jobs_started": 0,
    "jobs_succeeded": 0,
    "jobs_failed": 0,
    "active_jobs": 0,
    "queued_jobs": 0,
}
_metadata_lock = Lock()
_metadata_cache: dict[str, tuple[float, dict[str, Any]]] = {}
METADATA_CACHE_TTL = 60.0

URL_RE = re.compile(r"(https?://[^\s<>\"']+)|(www\.[^\s<>\"']+)", re.I)
YT_ID_RE = re.compile(r"(?:v=|/shorts/|/live/|youtu\.be/)([A-Za-z0-9_-]{11})")
PLATFORMS = {
    "youtube": {"label": "YouTube", "hosts": ("youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com")},
    "instagram": {"label": "Instagram", "hosts": ("instagram.com", "instagr.am")},
    "snapchat": {"label": "Snapchat", "hosts": ("snapchat.com", "snap.com")},
    "pinterest": {"label": "Pinterest", "hosts": ("pinterest.com", "pinterest.co", "pinterest.co.uk", "pinterest.de", "pinterest.fr", "pinterest.ca", "pinterest.jp", "pinterest.com.au", "pin.it")},
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
DEFAULT_QUALITY = "best"
AUTO_MODE = "auto"
DESKTOP_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"


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
    # Termux installs the real Deno binary through pkg; prefer it. Render can
    # use the Python deno package as a fallback.
    system_deno = shutil.which("deno")
    if system_deno:
        return system_deno
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


def metric_add(key: str, amount: int) -> None:
    with _metrics_lock:
        _metrics[key] = max(0, int(_metrics.get(key, 0)) + amount)


def metric_snapshot() -> dict[str, int]:
    with _metrics_lock:
        return {k: int(v) for k, v in _metrics.items()}


def metadata_cache_get(key: str) -> dict[str, Any]:
    now = time.monotonic()
    with _metadata_lock:
        row = _metadata_cache.get(key)
        if not row:
            return {}
        created, payload = row
        if now - created > METADATA_CACHE_TTL:
            _metadata_cache.pop(key, None)
            return {}
        return payload


def metadata_cache_put(key: str, payload: dict[str, Any]) -> None:
    if not payload:
        return
    now = time.monotonic()
    with _metadata_lock:
        stale = [k for k, (created, _) in _metadata_cache.items() if now - created > METADATA_CACHE_TTL]
        for k in stale:
            _metadata_cache.pop(k, None)
        if len(_metadata_cache) >= 128:
            oldest = min(_metadata_cache.items(), key=lambda item: item[1][0])[0]
            _metadata_cache.pop(oldest, None)
        _metadata_cache[key] = (now, payload)


def cleanup_stale_temp(max_age_seconds: int = 6 * 3600) -> None:
    """Remove abandoned temp directories left by killed Android/Termux processes."""
    roots = [Path(tempfile.gettempdir())]
    now = time.time()
    for root in roots:
        try:
            for path in root.iterdir():
                if not path.is_dir() or not path.name.startswith(("vx_", "vx_probe_")):
                    continue
                try:
                    if now - path.stat().st_mtime >= max_age_seconds:
                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    continue
        except OSError:
            continue


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


def _load_actions() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(ACTIONS_FILE.read_text(encoding="utf-8")) if ACTIONS_FILE.exists() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_actions(data: dict[str, dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACTIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(ACTIONS_FILE)


def cleanup_media_cache() -> None:
    now = time.time()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    try:
        for path in CACHE_DIR.iterdir():
            if not path.is_file():
                continue
            try:
                if now - path.stat().st_mtime > CACHE_TTL_SECONDS:
                    path.unlink(missing_ok=True)
                elif path.exists():
                    files.append(path)
            except OSError:
                continue
    except OSError:
        return

    # Bound total cache size so repeated large videos cannot fill phone storage.
    try:
        files = [p for p in files if p.exists()]
        total = sum(p.stat().st_size for p in files)
        if total > CACHE_TOTAL_MAX_BYTES:
            for path in sorted(files, key=lambda p: p.stat().st_mtime):
                if total <= CACHE_TOTAL_MAX_BYTES:
                    break
                try:
                    size = path.stat().st_size
                    path.unlink(missing_ok=True)
                    total -= size
                except OSError:
                    continue
    except OSError:
        pass


def create_action(
    uid: int,
    url: str,
    source_path: Path | None = None,
    title: str = "",
    item_index: int = 0,
) -> str:
    """Bind an inline action to the exact request and optionally cache its source."""
    now = int(time.time())
    raw = f"{uid}:{url}:{time.time_ns()}".encode()
    token = hashlib.sha256(raw).hexdigest()[:20]
    cleanup_media_cache()

    cache_path = ""
    if source_path is not None:
        try:
            if source_path.exists() and 0 < source_path.stat().st_size <= CACHE_MAX_BYTES:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                suffix = source_path.suffix.lower() or ".bin"
                dest = CACHE_DIR / f"{token}{suffix}"
                try:
                    os.link(source_path, dest)
                except OSError:
                    shutil.copy2(source_path, dest)
                cache_path = str(dest)
                cleanup_media_cache()
                if not dest.exists():
                    cache_path = ""
        except OSError as exc:
            log.info("media cache skipped: %s", str(exc)[:140])

    with _action_lock:
        actions = _load_actions()
        cutoff = now - 7 * 24 * 3600
        actions = {
            k: v for k, v in actions.items()
            if isinstance(v, dict) and int(v.get("created_at") or 0) >= cutoff
        }
        actions[token] = {
            "uid": uid,
            "url": url,
            "created_at": now,
            "cache_path": cache_path,
            "title": safe_media_title(title) if title else "",
            "item_index": max(0, int(item_index)),
        }
        if len(actions) > 1500:
            newest = sorted(
                actions.items(),
                key=lambda kv: int(kv[1].get("created_at") or 0),
                reverse=True,
            )[:1200]
            actions = dict(newest)
        _save_actions(actions)
    return token


def resolve_action_data(uid: int, token: str) -> dict[str, Any]:
    with _action_lock:
        row = dict(_load_actions().get(token) or {})
    if int(row.get("uid") or -1) != int(uid):
        return {}
    if int(row.get("created_at") or 0) < int(time.time()) - 7 * 24 * 3600:
        return {}

    cache_path = str(row.get("cache_path") or "")
    if cache_path:
        path = Path(cache_path)
        try:
            if not path.exists() or time.time() - path.stat().st_mtime > CACHE_TTL_SECONDS:
                row["cache_path"] = ""
        except OSError:
            row["cache_path"] = ""
    return row


def resolve_action(uid: int, token: str) -> str:
    return str(resolve_action_data(uid, token).get("url") or "")


def mp3_button(
    uid: int,
    url: str,
    source_path: Path | None = None,
    title: str = "",
    item_index: int = 0,
) -> InlineKeyboardMarkup:
    token = create_action(
        uid,
        url,
        source_path=source_path,
        title=title,
        item_index=item_index,
    )
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎵 MP3", callback_data=f"mp3|{token}")]])


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
    if yt and platform_of(url) == "youtube":
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


def is_video_post_url(url: str) -> bool:
    platform = platform_of(url)
    path = urlparse(url).path.lower()
    return (
        (platform == "instagram" and re.search(r"/(?:reels?|tv)/", path) is not None)
        or (platform == "snapchat" and "/spotlight/" in path)
    )


def request_headers(url: str | None = None) -> dict[str, str]:
    platform = platform_of(url or "")
    headers = {
        "User-Agent": DESKTOP_UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if platform == "instagram":
        headers["Referer"] = "https://www.instagram.com/"
    elif platform == "snapchat":
        headers["Referer"] = "https://www.snapchat.com/"
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = "none"
        headers["Upgrade-Insecure-Requests"] = "1"
    elif platform == "pinterest":
        headers["Referer"] = "https://www.pinterest.com/"
    return headers


def resolve_public_redirect(url: str) -> str:
    if (urlparse(url).hostname or "").lower() != "pin.it" and platform_of(url) != "snapchat":
        return url
    try:
        response = fetch_public_page(url)
        final = str(response.url)
        if platform_of(final) == platform_of(url):
            return final
    except Exception as exc:
        log.info("Share resolution failed: %s", type(exc).__name__)
    return url


def extractor_candidates(url: str) -> list[str]:
    """Return canonical public URL variants, keeping useful share context."""
    platform = platform_of(url)
    resolved = resolve_public_redirect(url)
    out = [resolved, url] if resolved != url else [url]
    try:
        parsed = urlparse(resolved)
        if platform == "instagram":
            match = re.search(r"/(reel|reels|p|tv)/([A-Za-z0-9_-]+)", parsed.path, re.I)
            if match:
                kind = "reel" if match.group(1).lower() in {"reel", "reels"} else match.group(1).lower()
                out.insert(0, f"https://www.instagram.com/{kind}/{match.group(2)}/")
        elif platform == "snapchat":
            match = re.search(r"/(?:@[^/]+/)?spotlight/([A-Za-z0-9_-]+)", parsed.path, re.I)
            if match:
                snap_id = match.group(1)
                # Keep the resolved @creator route first when available. yt-dlp
                # currently handles some modern Spotlight pages through generic
                # HTML5 extraction even when its dedicated extractor misses them.
                if "/@" in parsed.path and "/spotlight/" in parsed.path:
                    out.insert(0, resolved)
                out.extend([
                    f"https://www.snapchat.com/spotlight/{snap_id}?locale=en_US",
                    f"https://www.snapchat.com/spotlight/{snap_id}",
                    f"https://snapchat.com/spotlight/{snap_id}",
                ])
        elif platform == "pinterest":
            match = re.search(r"/pin/(?:[\w-]+--)?(\d+)", parsed.path, re.I)
            if match:
                out.insert(0, f"https://www.pinterest.com/pin/{match.group(1)}/")
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
    """Reject local/private destinations, including DNS-resolved private hosts."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            pass

        # Prevent SSRF through a hostname that resolves to loopback/private/link-local.
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            addresses = {info[4][0] for info in infos if info and info[4]}
            if not addresses:
                return False
            return all(ipaddress.ip_address(addr).is_global for addr in addresses)
        except (socket.gaierror, ValueError, OSError):
            # Media CDNs can be temporarily unresolvable. Do not fetch an unknown host.
            return False
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
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 25,
        "concurrent_fragment_downloads": 4,
        "outtmpl": str(out / "%(playlist_index)05d_%(extractor)s_%(id)s_%(title).80B.%(ext)s"),
        "restrictfilenames": True,
        "overwrites": True,
        "cachedir": False,
        "merge_output_format": "mkv",
        "age_limit": 17,
        "skip_unavailable_fragments": False,
        "playlistend": MAX_GALLERY_ITEMS + 1,
        "max_filesize": MAX_SOURCE_BYTES,
        "http_headers": request_headers(),
        "progress_hooks": [check_download_budget],
    }
    if PROXY:
        opts["proxy"] = PROXY
    if DENO_BIN:
        opts["js_runtimes"] = {"deno": {"path": DENO_BIN}}
    return opts


def _instagram_format_score(fmt: dict[str, Any], kind: str) -> tuple[int, int, int]:
    width, height = int(fmt.get("width") or 0), int(fmt.get("height") or 0)
    return (width * height or height, int(fmt.get("has_audio", True)), int(fmt.get("bitrate") or 0))


def extract_instagram_public(url: str) -> dict[str, Any]:
    """Dedicated public Instagram metadata path for Reels/posts/carousels."""
    cache_key = f"instagram:{url}"
    cached = metadata_cache_get(cache_key)
    if cached:
        return cached

    from parth_dl import InstagramDownloader

    downloader = InstagramDownloader(
        verbose=False,
        rate_limit=True,
        quiet=True,
        overwrite=True,
    )
    from source_metadata import instagram_extractor
    downloader = instagram_extractor(downloader)
    info = downloader.get_info(extractor_candidates(url)[0])
    payload = info if isinstance(info, dict) else {}
    metadata_cache_put(cache_key, payload)
    return payload


def download_instagram_dedicated(
    url: str,
    tmpdir: str,
    info: dict[str, Any] | None = None,
) -> tuple[list[Path], str]:
    """Download exact Instagram entries in original carousel order."""
    if not isinstance(info, dict) or not info.get("entries"):
        try:
            info = extract_instagram_public(url)
        except Exception as exc:
            if fatal_download_error(exc):
                raise
            log.info("Instagram dedicated metadata failed: %s", str(exc)[:180])
            return [], ""

    entries = info.get("entries") if isinstance(info.get("entries"), list) else []
    if not entries:
        return [], str(info.get("type") or "").lower()

    dest = Path(tmpdir) / "instagram"
    dest.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []

    if len(entries) > MAX_GALLERY_ITEMS:
        raise RuntimeError("Post exceeds the configured item limit; nothing was truncated.")
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").lower()
        if kind not in {"video", "image"}:
            continue
        formats = [
            fmt for fmt in (entry.get("formats") or [])
            if isinstance(fmt, dict) and fmt.get("url")
        ]
        formats.sort(key=lambda fmt: (kind != "video" or fmt.get("has_audio", True),
                                      _instagram_format_score(fmt, kind)), reverse=True)

        for fmt in formats:
            media_url = str(fmt.get("url") or "")
            if not media_url or not safe_remote_url(media_url):
                continue
            path_ext = Path(urlparse(media_url).path).suffix.lower()
            if kind == "video":
                ext = path_ext if path_ext in {".mp4", ".m4v", ".mov", ".webm"} else ".mp4"
            else:
                ext = path_ext if path_ext in {".jpg", ".jpeg", ".png", ".webp", ".avif"} else ".jpg"
            out = dest / f"{index:02d}_{kind}{ext}"
            if _download_direct_file(media_url, out, referer="https://www.instagram.com/"):
                if classify(out) != kind:
                    out.unlink(missing_ok=True)
                    continue
                outputs.append(out)
                break

    if len(outputs) != len(entries):
        raise RuntimeError(f"Incomplete carousel: downloaded {len(outputs)}/{len(entries)} items.")
    for index, audio_url in enumerate(info.get("audio_urls") or []):
        audio = Path(tmpdir) / "soundtrack" / f"{index:05d}.m4a"
        if _download_direct_file(audio_url, audio, referer=url):
            if not has_audio(audio):
                audio.unlink(missing_ok=True)
    return validate_media_files(outputs), str(info.get("type") or "").lower()


def _dig_dict(node: Any, *keys: str) -> dict[str, Any]:
    cur = node
    for key in keys:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(key)
    return cur if isinstance(cur, dict) else {}


def _snap_requested_id(url: str, doc: dict[str, Any]) -> str:
    match = re.search(r"/(?:@[^/]+/)?spotlight/([A-Za-z0-9_-]+)", urlparse(url).path, re.I)
    if match:
        return match.group(1)
    query = doc.get("query") if isinstance(doc.get("query"), dict) else {}
    return str(query.get("snapID") or "")


def _snap_target_metadata(doc: dict[str, Any], requested_id: str) -> dict[str, Any]:
    props = _dig_dict(doc, "props", "pageProps")
    feed = props.get("spotlightFeed") if isinstance(props.get("spotlightFeed"), dict) else {}
    stories = feed.get("spotlightStories") if isinstance(feed.get("spotlightStories"), list) else []

    if requested_id:
        for item in stories:
            if not isinstance(item, dict):
                continue
            story = item.get("story") if isinstance(item.get("story"), dict) else {}
            story_id = story.get("storyId") if isinstance(story.get("storyId"), dict) else {}
            if str(story_id.get("value") or "") == requested_id:
                meta = item.get("metadata")
                if isinstance(meta, dict):
                    return meta

    if requested_id and stories:
        return {}  # Never substitute a recommendation for the requested item.
    query_id = str(_dig_dict(doc, "query").get("snapID") or "")
    if requested_id and query_id and requested_id != query_id:
        return {}
    top = props.get("videoMetadata")
    if isinstance(top, dict) and str(top.get("contentUrl") or ""):
        return {"videoMetadata": top}

    return {}


def _snap_info_from_doc(doc: dict[str, Any], page_url: str) -> dict[str, str]:
    requested_id = _snap_requested_id(page_url, doc)
    meta = _snap_target_metadata(doc, requested_id)
    vm = meta.get("videoMetadata") if isinstance(meta.get("videoMetadata"), dict) else {}
    video = str(vm.get("contentUrl") or "")
    if not video:
        return {}
    creator = vm.get("creator") if isinstance(vm.get("creator"), dict) else {}
    person = creator.get("personCreator") if isinstance(creator.get("personCreator"), dict) else {}
    title = str(vm.get("name") or meta.get("llmTitle") or "Spotlight")
    return {
        "id": requested_id,
        "url": video,
        "thumbnail": str(vm.get("thumbnailUrl") or ""),
        "title": title,
        "uploader": str(person.get("username") or person.get("name") or ""),
        "page_url": page_url,
    }


def extract_snapchat_public(url: str) -> dict[str, Any]:
    cached = metadata_cache_get(f"snapchat:{url}")
    if cached:
        return cached
    for candidate in extractor_candidates(url)[:3]:
        try:
            response = fetch_public_page(candidate)
        except (httpx.HTTPError, RuntimeError):
            continue
        match = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', response.text, re.I | re.S)
        if not match:
            if "/spotlight/" in urlparse(candidate).path:
                info = snapchat_preload(response.text)
                if info:
                    return info
            continue
        doc = json.loads(match.group(1))
        if "/spotlight/" in urlparse(candidate).path:
            info = _snap_info_from_doc(doc, candidate)
            if info:
                info["entries"] = [{"url": info["url"], "kind": "video"}]
        else:
            entries = snapchat_story_entries(doc, candidate)
            info = {"entries": entries, "title": "Snapchat story"} if entries else {}
        if info:
            metadata_cache_put(f"snapchat:{url}", info)
            return info
    return {}


def download_snapchat_dedicated(url: str, tmpdir: str, info: dict[str, Any] | None = None) -> list[Path]:
    info = info or extract_snapchat_public(url)
    entries = info.get("entries") or ([{"url": info["url"], "kind": "video"}] if info.get("url") else [])
    if len(entries) > MAX_GALLERY_ITEMS:
        raise RuntimeError("Story exceeds the configured item limit; nothing was truncated.")
    outputs = []
    for index, entry in enumerate(entries, 1):
        kind = entry["kind"]
        out = Path(tmpdir) / "snapchat" / f"{index:05d}{'.jpg' if kind == 'image' else '.mp4'}"
        if not _download_direct_file(entry["url"], out, referer="https://www.snapchat.com/"):
            raise RuntimeError(f"Incomplete story: downloaded {len(outputs)}/{len(entries)} items")
        outputs.append(out)
    return validate_media_files(outputs)


def files_in(root: Path) -> list[Path]:
    if not root.exists():
        return []
    skip = {".json", ".vtt", ".srt", ".part", ".ytdl", ".nfo", ".txt"}
    result = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() not in skip and p.stat().st_size > 0]
    # Downloaders create carousel items sequentially. Keep that order instead of
    # sorting by size, which used to scramble item 1/2/3.
    result.sort(key=lambda p: (p.stat().st_mtime_ns, str(p)))
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
    cookie_file = os.getenv(f"{(platform_of(url) or '').upper()}_COOKIE_FILE", "")
    if cookie_file and Path(cookie_file).is_file():
        base["cookiefile"] = cookie_file
    attempts: list[tuple[str, dict[str, Any]]] = []
    if mode in {AUTO_MODE, "original"}:
        # yt-dlp ranks all exposed formats; no hidden resolution ceiling.
        attempts = [("bv*+ba/b", {})]
    elif mode in VIDEO_PRESETS:
        attempts = [(fmt, {}) for fmt in video_chain(int(VIDEO_PRESETS[mode]["height"]))]
    elif mode in AUDIO_PRESETS:
        # Keep source audio here. The delivery stage converts to MP3 once.
        attempts = [("bestaudio/ba/best", {})]
    else:
        raise ValueError("Unknown download mode")

    last: Exception | None = None
    seen: set[tuple[str, str]] = set()
    attempt_no = 0
    for candidate in extractor_candidates(url):
        for fmt, extra in attempts:
            sig = (candidate, fmt)
            if sig in seen:
                continue
            seen.add(sig)
            attempt_no += 1
            attempt_dir = Path(tmpdir) / "ytdl_attempts" / f"{attempt_no:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            try:
                opts = dict(base)
                opts["format"] = fmt
                opts["http_headers"] = request_headers(candidate)
                opts["outtmpl"] = str(attempt_dir / "%(playlist_index)05d_%(extractor)s_%(id)s_%(title).80B.%(ext)s")
                opts.update(extra)
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(candidate, download=True)
                files = files_in(attempt_dir)
                if not info:
                    raise RuntimeError("Extractor returned no media metadata")
                entries = list(info.get("entries") or []) if info.get("_type") in {"playlist", "multi_video"} else [info]
                if any(e is None for e in entries) or len(entries) > MAX_GALLERY_ITEMS:
                    raise RuntimeError("Incomplete collection or configured item limit exceeded")
                # Only postprocessed final paths, never leftover video-only/audio-only fragments.
                expected = []
                for entry in entries:
                    for item in entry.get("requested_downloads") or [entry]:
                        name = item.get("filepath") or entry.get("filepath")
                        if name and Path(name).is_file(): expected.append(Path(name))
                if expected:
                    files = expected
                complete = [p for p in files if p.suffix.lower() not in {".part", ".ytdl"}]
                if mode in AUDIO_PRESETS:
                    complete = [p for p in complete if classify(p) in {"audio", "video", "animation"}]
                elif platform_of(url) == "youtube" or is_video_post_url(url):
                    complete = [p for p in complete if classify(p) in {"video", "audio"}]
                if len(complete) != len(entries):
                    raise RuntimeError("Incomplete collection: some media files are missing")
                if complete:
                    return validate_media_files(complete)
                shutil.rmtree(attempt_dir, ignore_errors=True)
            except Exception as exc:
                if fatal_download_error(exc):
                    raise
                last = exc
                log.warning("yt-dlp %s on %s failed: %s", fmt, platform_label(candidate), str(exc)[:220])
    if last:
        raise last
    return []


def _pinterest_quality_score(fmt: dict[str, Any]) -> tuple[int, int, int]:
    h, w = int(fmt.get("height") or 0), int(fmt.get("width") or 0)
    return (h * w or h, int(fmt.get("bitrate") or 0), int(".mp4" in str(fmt.get("url", ""))))


def _pinterest_target_height(formats: list[dict[str, Any]]) -> int:
    return max((int(f.get("height") or 0) for f in formats), default=0)


def _download_direct_file(media_url: str, out: Path, *, referer: str, timeout: int = 45) -> bool:
    from transfer import download
    try:
        return download(media_url, out,
                        headers={"User-Agent": DESKTOP_UA, "Referer": referer, "Accept": "*/*"},
                        safe_url=safe_remote_url, budget=check_download_budget,
                        max_bytes=MAX_SOURCE_BYTES, proxy=PROXY or None, timeout=timeout)
    except RuntimeError as exc:
        if fatal_download_error(exc):
            raise
        log.info("Direct source variant unavailable: %s", type(exc).__name__)
        return False


def download_gallery(url: str, tmpdir: str) -> list[Path]:
    if platform_of(url) not in {"instagram", "pinterest"}:
        return []
    dest = Path(tmpdir) / "gallery"
    dest.mkdir(parents=True, exist_ok=True)
    # Dump an ordered manifest first. A partial subprocess download is not success.
    cmd = [sys.executable, "-m", "gallery_dl", "--config-ignore", "-c",
           str(Path(__file__).with_name("gallery-dl.conf")), "--resolve-json", url]
    if PROXY:
        cmd[3:3] = ["--proxy", PROXY]
    cookie_file = os.getenv(f"{platform_of(url).upper()}_COOKIE_FILE", "")
    if cookie_file and Path(cookie_file).is_file():
        cmd[3:3] = ["--cookies", cookie_file]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode:
        raise RuntimeError("Gallery metadata unavailable")
    manifest = json.loads(proc.stdout)
    entries = [row for row in manifest if isinstance(row, list) and len(row) >= 3 and row[0] == 3
               and not str(row[1]).startswith("text:")]
    if any(row and row[0] in {-1, 6} for row in manifest):
        if entries:
            raise RuntimeError("Incomplete gallery metadata or unresolved media item")
        raise RuntimeError("Gallery metadata unavailable; no items extracted")
    if platform_of(url) == "pinterest":
        from source_metadata import pinterest_entries
        pins = [row[-1] for row in manifest if isinstance(row, list) and row and row[0] == 2
                and isinstance(row[-1], dict) and any(k in row[-1] for k in ("images", "videos", "carousel_data", "story_pin_data"))]
        if pins:
            # Reconstruct from full pin metadata: gallery-dl's carousel path
            # chooses an image even for a video slot.
            return download_source_entries([entry for pin in pins for entry in pinterest_entries(pin)], dest, url)
    if not entries:
        return []
    if len(entries) > MAX_GALLERY_ITEMS:
        raise RuntimeError("Post exceeds the configured item limit; nothing was truncated")
    outputs = []
    for index, (_, media_url, meta) in enumerate(entries, 1):
        media_url = str(media_url).removeprefix("ytdl:")
        if not safe_remote_url(media_url):
            raise RuntimeError("Unsafe media destination")
        folder = dest / f"{index:05d}"
        folder.mkdir(exist_ok=True)
        if ".m3u8" in media_url or meta.get("_ytdl_manifest"):
            opts = base_ydl_opts(str(folder))
            opts.update(format="bv*+ba/b", outtmpl=str(folder / "media.%(ext)s"))
            with YoutubeDL(opts) as ydl:
                ydl.extract_info(media_url, download=True)
            found = validate_media_files(files_in(folder))
            if len(found) != 1:
                raise RuntimeError("Incomplete gallery video")
            outputs.extend(found)
        else:
            ext = str(meta.get("extension") or Path(urlparse(media_url).path).suffix.lstrip(".") or "bin")
            ext = ext if re.fullmatch(r"[a-zA-Z0-9]{1,8}", ext) else "bin"
            out = folder / f"media.{ext}"
            candidates = [media_url, *(meta.get("_fallback") or [])]
            if not any(_download_direct_file(candidate, out, referer=url) for candidate in candidates):
                raise RuntimeError(f"Incomplete carousel: downloaded {len(outputs)}/{len(entries)} items")
            outputs.append(out)
    return validate_media_files(outputs)


def download_source_entries(entries: list[dict], dest: Path, referer: str) -> list[Path]:
    if len(entries) > MAX_GALLERY_ITEMS:
        raise RuntimeError("Post exceeds configured item limit")
    outputs = []
    for index, entry in enumerate(entries):
        folder = dest / f"source_{index:05d}"
        folder.mkdir(parents=True, exist_ok=True)
        completed = None
        for variant, fmt in enumerate(entry["formats"]):
            url = fmt.get("url") or ""
            if not safe_remote_url(url):
                continue
            try:
                if ".m3u8" in url or ".mpd" in url:
                    opts = base_ydl_opts(str(folder))
                    opts.update(format="bv*+ba/b", outtmpl=str(folder / f"{variant}.%(ext)s"))
                    with YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    found = [Path(x["filepath"]) for x in info.get("requested_downloads", []) if x.get("filepath")]
                    found = found or files_in(folder)
                    if len(found) != 1:
                        continue
                    path = found[0]
                else:
                    ext = Path(urlparse(url).path).suffix
                    ext = ext if re.fullmatch(r"\.[a-zA-Z0-9]{1,8}", ext) else ".bin"
                    path = folder / f"{variant}{ext}"
                    if not _download_direct_file(url, path, referer=referer):
                        continue
                actual = classify(path)
                if actual != entry["kind"] and not (entry["kind"] == "image" and actual == "animation"):
                    continue
                completed = path
                break
            except Exception as exc:
                if fatal_download_error(exc):
                    raise
                log.warning("Source variant failed: %s", type(exc).__name__)
        if completed is None:
            raise RuntimeError(f"Incomplete post: item {index + 1}/{len(entries)} unavailable; poster substitution refused")
        outputs.append(completed)
    return validate_media_files(outputs)


def download_pinterest_page(url: str, tmpdir: str) -> list[Path]:
    from source_metadata import find_pinterest_pin, pinterest_entries
    resolved = resolve_public_redirect(url)
    match = re.search(r"/pin/(?:[\w-]+--)?(\d+)", urlparse(resolved).path)
    if not match:
        return []
    page = fetch_public_page(resolved)
    matches = []
    for raw in re.findall(r"<script\b[^>]*>(.*?)</script>", page.text, re.I | re.S):
        try:
            document = json.loads(raw)
        except (ValueError, TypeError):
            continue
        pin = find_pinterest_pin(document, match.group(1))
        if pin:
            matches.append(pin)
    if not matches:
        return []
    pin = find_pinterest_pin(matches, match.group(1))
    return download_source_entries(pinterest_entries(pin), Path(tmpdir) / "pinterest_page", resolved)


def og_values(page: str, names: set[str]) -> list[str]:
    values: list[str] = []
    for tag in re.findall(r"<meta\b[^>]*>", page, flags=re.I):
        attrs = dict(re.findall(r"([\w:-]+)\s*=\s*[\"']([^\"']*)[\"']", tag, flags=re.I))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        value = attrs.get("content") or ""
        if key in names and value:
            values.append(html.unescape(value))
    return values


def _decode_web_url(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\\/","/").replace("\\u0026", "&").replace("\\u003d", "=")
    value = value.replace("\\u002F", "/").replace("\\u002f", "/")
    return value.strip()


def _walk_media_urls(value: Any, key_hint: str = "") -> list[str]:
    """Extract public media URLs from JSON/Next.js/JSON-LD payloads."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.extend(_walk_media_urls(child, str(key).lower()))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_media_urls(child, key_hint))
    elif isinstance(value, str):
        raw = _decode_web_url(value)
        key_media = any(x in key_hint for x in (
            "contenturl", "content_url", "video_url", "videourl",
            "playbackurl", "playback_url", "mediaurl", "media_url",
            "thumbnailurl", "thumbnail_url", "imageurl", "image_url",
        ))
        path = urlparse(raw).path.lower() if raw.startswith(("http://", "https://")) else ""
        ext_media = any(path.endswith(ext) for ext in (
            ".mp4", ".m4v", ".mov", ".webm", ".m3u8",
            ".jpg", ".jpeg", ".png", ".webp",
        ))
        if raw.startswith(("http://", "https://")) and (key_media or ext_media):
            found.append(raw)
    return found


def page_media_candidates(page: str, include_images: bool = True) -> list[str]:
    """Parse public HTML while excluding unrelated page artwork for video posts."""
    video_candidates: list[str] = []
    image_candidates: list[str] = []

    video_candidates += og_values(page, {
        "og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream",
    })
    if include_images:
        image_candidates += og_values(page, {
            "og:image", "og:image:url", "og:image:secure_url", "twitter:image",
        })

    for match in re.finditer(
        r"<script[^>]*(?:type=[\"']application/(?:ld\+)?json[\"']|id=[\"']__NEXT_DATA__[\"'])[^>]*>(.*?)</script>",
        page,
        flags=re.I | re.S,
    ):
        raw = html.unescape(match.group(1)).strip()
        try:
            for candidate in _walk_media_urls(json.loads(raw)):
                path = urlparse(candidate).path.lower()
                if any(ext in path for ext in (".mp4", ".m4v", ".mov", ".webm", ".m3u8")):
                    video_candidates.append(candidate)
                elif include_images and any(ext in path for ext in (".jpg", ".jpeg", ".png", ".webp")):
                    image_candidates.append(candidate)
        except Exception:
            pass

    for match in re.finditer(
        r"[\"'](?:video_url|videoUrl|contentUrl|content_url|playbackUrl|playback_url)[\"']\s*:\s*[\"']([^\"']+)",
        page,
        flags=re.I,
    ):
        video_candidates.append(_decode_web_url(match.group(1)))

    for match in re.finditer(
        r"https?:\\?/\\?/[^\"'<>\s]+?(?:\.mp4|\.m3u8|\.webm)(?:\?[^\"'<>\s]*)?",
        page,
        flags=re.I,
    ):
        video_candidates.append(_decode_web_url(match.group(0)))

    if include_images:
        for match in re.finditer(
            r"[\"'](?:thumbnailUrl|thumbnail_url|imageUrl|image_url)[\"']\s*:\s*[\"']([^\"']+)",
            page,
            flags=re.I,
        ):
            image_candidates.append(_decode_web_url(match.group(1)))

    return list(dict.fromkeys(x for x in video_candidates + image_candidates if x))


class WorkerFailure(RuntimeError):
    """A worker's already classified, safe user-facing failure."""


def fatal_download_error(exc: Exception) -> bool:
    low = str(exc).lower()
    if re.search(r"\bage(?:\b|_)", low) and any(word in low for word in ("restricted", "limit", "confirm", "verify")):
        return True
    return any(word in low for word in ("incomplete", "truncated", "storage", "deadline",
                                        "configured item limit", "configured storage limit", "job storage limit"))


def friendly_error(platform: str, exc: Exception) -> str:
    """User-safe errors only. Full extractor details stay in logs."""
    if isinstance(exc, WorkerFailure):
        return str(exc)
    if connection_failed_before_send(exc):
        return "Could not connect to Telegram. The failed upload was not sent; try again when the connection returns."
    if isinstance(exc, (TimedOut, NetworkError)):
        return "Telegram did not confirm delivery. Check the chat before retrying to avoid duplicates."
    text = str(exc)
    low = text.lower()
    label = PLATFORMS.get(platform, {}).get("label", "Media")
    if "429" in low or "too many requests" in low or "rate-limit" in low or "rate limit" in low:
        return f"{label}: temporarily rate-limited. Please wait before trying again."
    if "storage" in low or "no space left" in low:
        return "The download device is low on storage. Free some space and try again."
    if "deadline" in low or "timed out" in low or "timeout" in low:
        return f"{label}: the download took too long. Please try again later."
    if "incomplete" in low or "truncated" in low:
        return f"{label}: some media could not be retrieved. The incomplete post was not sent."
    if re.search(r"\bage(?:\b|_)", low) and any(s in low for s in ("restricted", "limit", "confirm", "verify")):
        return "This age-restricted media is not supported."
    if "limit" in low:
        return "This media exceeds the current file size or item limit."
    if any(x in low for x in ("login", "sign in", "cookies", "private", "empty media response")):
        return f"{label}: this post currently requires access the bot does not have."
    if "404" in low or "not found" in low:
        return f"{label}: this link is unavailable, expired, or its public page changed."
    if "403" in low or "forbidden" in low:
        return f"{label}: the platform blocked this download request."
    if "no accessible audio" in low or "no audio/video" in low or "no audio track" in low:
        return "This post has no accessible audio track. A photo alone does not contain its background music."
    if "unsupported url" in low:
        return f"{label}: this link type is not supported yet."
    if "telegram" in low and ("limit" in low or "fit" in low or "over" in low):
        return "Telegram upload limit prevented this file from being sent."
    return f"{label}: couldn't download this media right now."


def _looks_like_html(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(512).lstrip().lower()
        return (
            head.startswith(b"<!doctype html")
            or head.startswith(b"<html")
            or b"<head" in head[:256]
            or b"<body" in head[:256]
        )
    except OSError:
        return True


def _quick_fingerprint(path: Path) -> tuple[int, str]:
    """Cheap duplicate detection that preserves carousel order."""
    size = path.stat().st_size
    h = hashlib.blake2b(digest_size=16)
    with path.open("rb") as fh:
        h.update(fh.read(256 * 1024))
        if size > 512 * 1024:
            fh.seek(max(0, size - 256 * 1024))
            h.update(fh.read(256 * 1024))
    h.update(str(size).encode())
    return size, h.hexdigest()


def sanitize_media_files(paths: list[Path]) -> list[Path]:
    """Drop empty/HTML/duplicate artifacts without reordering valid media."""
    clean: list[Path] = []
    seen: set[tuple[int, str]] = set()
    for path in paths:
        try:
            if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
                continue
            if _looks_like_html(path):
                log.warning("discarding HTML masquerading as media: %s", path.name)
                continue
            key = _quick_fingerprint(path)
            if key in seen:
                log.info("discarding duplicate media artifact: %s", path.name)
                continue
            seen.add(key)
            clean.append(path)
        except OSError:
            continue
    return clean


def grab(url: str, mode: str, tmpdir: str, prefetched: dict[str, Any] | None = None) -> list[Path]:
    platform = platform_of(url)
    if not platform or not safe_remote_url(url):
        raise RuntimeError("Unsupported or unsafe media link")
    Path(tmpdir).mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(tmpdir).free < 128 * 1024 * 1024:
        raise RuntimeError("Not enough free storage for a download")
    # Source-aware parsers only: page-wide URL scans can leak recommendations/posters.
    errors = []
    if platform == "youtube":
        engines = [lambda: download_ytdlp(url, mode, tmpdir)]
    elif platform == "instagram":
        engines = [lambda: download_instagram_dedicated(url, tmpdir, (prefetched or {}).get("_instagram_info"))[0],
                   lambda: download_gallery(url, tmpdir),
                   lambda: download_ytdlp(url, mode, tmpdir)]
    elif platform == "pinterest":
        # gallery-dl retains page boundaries, audio blocks and carousel order.
        engines = [lambda: download_gallery(resolve_public_redirect(url), tmpdir),
                   lambda: download_pinterest_page(url, tmpdir)]
    else:
        engines = [lambda: download_snapchat_dedicated(url, tmpdir, (prefetched or {}).get("_snap_info")),
                   lambda: download_ytdlp(url, mode, tmpdir)]
    for engine in engines:
        try:
            files = engine()
            if not files:
                continue
            if len(files) > MAX_GALLERY_ITEMS:
                raise RuntimeError("Configured item limit exceeded")
            if any(p.stat().st_size > MAX_SOURCE_BYTES for p in files):
                raise RuntimeError("Source file exceeds configured storage limit")
            files = validate_media_files(files)
            if mode not in AUDIO_PRESETS and is_video_post_url(url) and any(classify(p) != "video" for p in files):
                raise RuntimeError("Video post returned a poster instead of video")
            return files
        except Exception as exc:
            # Never hide a partial collection by falling back to a one-item result.
            if fatal_download_error(exc):
                raise
            errors.append(exc)
            log.warning("%s extractor failed: %s", platform, type(exc).__name__)
    if errors:
        raise errors[-1]
    raise RuntimeError("No public downloadable media found")


def dedupe_media(paths: list[Path]) -> list[Path]:
    """Backward-compatible alias for the canonical media sanitizer."""
    return sanitize_media_files(paths)


def classify(path: Path) -> str:
    """Classify the actual media so Telegram receives it as the right type."""
    try:
        from media_io import media_kind
        return media_kind(path)
    except Exception:
        pass
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic", ".heif"}:
        return "image"
    if ext in {".gif"}:
        return "animation"
    if ext in {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac"}:
        return "audio"
    if ext in {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".ts"}:
        return "video"

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=15,
            )
            payload = json.loads(proc.stdout or "{}")
            types = {str(x.get("codec_type") or "") for x in payload.get("streams") or [] if isinstance(x, dict)}
            if "video" in types:
                return "video"
            if "audio" in types:
                return "audio"
        except Exception:
            pass
    return "document"


def ffmpeg(cmd: list[str], timeout: int = 900) -> None:
    from media_process import run
    proc = run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "")[-400:] or "ffmpeg failed")


def media_duration(path: Path) -> float:
    proc = subprocess.run(["ffmpeg", "-i", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=30)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
    if not match:
        return 0.0
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def image_dimensions(path: Path) -> tuple[int, int]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return (0, 0)
    try:
        proc = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
        streams = (json.loads(proc.stdout or "{}").get("streams") or [])
        if streams and isinstance(streams[0], dict):
            return (int(streams[0].get("width") or 0), int(streams[0].get("height") or 0))
    except Exception:
        pass
    return (0, 0)


def telegram_photo_ready(path: Path) -> bool:
    """Conservative sendPhoto preflight: valid container, size, dimensions and aspect."""
    try:
        if path.stat().st_size <= 0 or path.stat().st_size > MAX_PHOTO_BYTES:
            return False
        with path.open("rb") as fh:
            head = fh.read(16)
        jpeg = head.startswith(b"\xff\xd8\xff")
        png = head.startswith(b"\x89PNG\r\n\x1a\n")
        if not (jpeg or png):
            return False
        width, height = image_dimensions(path)
        if width <= 0 or height <= 0:
            return False
        if width + height > 10000:
            return False
        ratio = max(width / height, height / width)
        return ratio <= 20
    except (OSError, ZeroDivisionError):
        return False


def fit_image(path: Path, dest: Path) -> Path:
    """Keep visual media native only when it is guaranteed to fit Telegram photo rules."""
    if telegram_photo_ready(path):
        return path
    if classify(path) != "image":
        raise RuntimeError("Refusing to convert video or animation to a photo")
    dest.mkdir(parents=True, exist_ok=True)
    for width, quality in ((4096, 4), (3072, 6), (2048, 8), (1600, 10)):
        out = dest / f"{path.stem}.{width}.jpg"
        ffmpeg([
            "ffmpeg", "-y", "-i", str(path), "-frames:v", "1",
            "-vf", f"scale='min({width},iw)':'min({width},ih)':force_original_aspect_ratio=decrease,pad='max(iw,ih/19)':'max(ih,iw/19)':(ow-iw)/2:(oh-ih)/2",
            "-q:v", str(quality), str(out),
        ], timeout=180)
        if telegram_photo_ready(out):
            return out
    raise RuntimeError("Image could not fit Telegram photo limits")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "⚡ Veltrix Downloader\n\n"
        "YouTube • Instagram • Snapchat • Pinterest\n"
        "Send a media link — I download it automatically."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📥 Send a YouTube, Instagram, Snapchat or Pinterest media link.\n"
        "🎬 Highest available quality; large videos may arrive in parts.\n"
        "🎵 After a video is sent, tap MP3 to get audio."
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("⚙️ Quality is automatic. No setup needed.")


async def edit_status(status, text: str, reply_markup=None) -> None:
    try:
        captionable = any(
            getattr(status, field, None)
            for field in ("photo", "video", "audio", "document", "animation")
        )
        if captionable:
            await status.edit_caption(caption=text, reply_markup=reply_markup, connect_timeout=8, read_timeout=8)
        else:
            await status.edit_text(text, reply_markup=reply_markup, connect_timeout=8, read_timeout=8)
    except TelegramError:
        pass


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    uid = update.effective_user.id
    url = extract_url(msg.text or msg.caption or "")
    if not url:
        await msg.reply_text("Send a YouTube, Instagram, Snapchat or Pinterest link to download its media.")
        return
    platform = platform_of(url)
    if not platform:
        await msg.reply_text("This site is not supported. Send a YouTube, Instagram, Snapchat or Pinterest link.")
        return
    patch_user(uid, last_url=url)

    await dispatch_job(msg, context, uid, url, AUTO_MODE)


def job_manager() -> JobManager:
    global _jobs
    if _jobs is None:
        _jobs = JobManager(DATA_DIR / "jobs.sqlite3",
                           max_pending=max(3, min(int(os.getenv("MAX_PENDING_JOBS", "20")), 100)))
    return _jobs


class DeferredStatus:
    """A lost acknowledgement must not lose an admitted download."""
    def __init__(self, msg):
        self.msg, self.message = msg, None

    async def edit_text(self, text, **kwargs):
        if self.message is None:
            self.message = await self.msg.reply_text(text, **kwargs)
        else:
            await self.message.edit_text(text, **kwargs)

    async def delete(self):
        if self.message is not None:
            await self.message.delete()


async def dispatch_job(msg, context, uid: int, url: str, mode: str, action=None) -> None:
    url = extract_url(url) or url
    if platform_of(url) == "instagram":
        match = re.search(r"/(reels?|p|tv)/([A-Za-z0-9_-]+)", urlparse(url).path)
        if match:
            kind = "reel" if match[1] in {"reel", "reels"} else match[1]
            url = f"https://www.instagram.com/{kind}/{match[2]}/"
    manager = job_manager()
    result, row = manager.admit(uid, msg.chat_id, url, mode, getattr(msg, "message_thread_id", None))
    if result != "accepted":
        messages = {
            "duplicate": "This link is already being processed here. Use /status to see its state.",
            "user_full": "You already have 3 requests in progress. Please wait for one to finish.",
            "full": "The download queue is full. Please try again shortly.",
        }
        await msg.reply_text(messages[result])
        return

    async def execute(_):
        status = DeferredStatus(msg)
        await edit_status(status, f"⏳ {platform_label(url)} · queued")
        sources = []
        if action:
            rows = [resolve_action_data(uid, child) for child in action.get("children", [])] if action.get("post") else [action]
            sources = [Path(r["cache_path"]) for r in rows if r.get("cache_path")]
            if not sources or len(sources) != len(rows) or not all(p.is_file() for p in sources):
                sources = []
        await run_job(msg, context, uid, url, mode, status,
                      cached_sources=sources or None, known_meta={"title": "Media"})
    manager.launch(row, execute)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = job_manager().recent(update.effective_user.id, update.effective_message.chat_id)
    labels = {"queued": "⏳ Queued", "downloading": "⬇️ Downloading / preparing",
              "sending": "📤 Sending", "done": "✅ Delivered", "failed": "❌ Failed — send the link to retry",
              "interrupted": "⚠️ Delivery interrupted — check received media before retrying",
              "expired": "⌛ Expired — send the link again"}
    text = "\n".join(f"{platform_label(r['url'])} · {labels.get(r['state'], r['state'])}" for r in rows)
    await update.effective_message.reply_text(text or "No recent downloads. Send a media link.")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id

    if data.startswith("retry|"):
        action = resolve_action_data(uid, data.split("|", 1)[1])
        if not action.get("url"):
            await q.answer("This action expired. Send the link again.")
            return
        await q.answer()
        await dispatch_job(q.message, context, uid, action["url"], AUTO_MODE)
        return
    if not data.startswith("mp3|"):
        await q.answer()
        return

    token = data.split("|", 1)[1]
    action = resolve_action_data(uid, token)
    url = str(action.get("url") or "")
    if not url:
        await q.answer("This MP3 action expired. Send the media link again.", show_alert=False)
        return

    await q.answer()
    await dispatch_job(q.message, context, uid, url, "mp3_320", action=action)


def telegram_retry_delay(exc: Exception, attempt: int) -> float:
    if isinstance(exc, RetryAfter):
        value = getattr(exc, "retry_after", 1)
        try:
            seconds = value.total_seconds() if hasattr(value, "total_seconds") else float(value)
            return max(1.0, seconds + 0.5)
        except Exception:
            return 2.0
    return float(min(2 ** attempt, 8))


def connection_failed_before_send(exc: Exception) -> bool:
    """Only connection establishment / pool failures prove no upload began."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return True
        exc = exc.__cause__
    return False


async def _retry_telegram(call, attempts: int = 4):
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await call()
        except RetryAfter as exc:
            last = exc
            if attempt + 1 >= attempts:
                break
            await asyncio.sleep(telegram_retry_delay(exc, attempt))
        except (TimedOut, NetworkError) as exc:
            if not connection_failed_before_send(exc):
                raise
            last = exc
            if attempt + 1 >= attempts:
                break
            await asyncio.sleep(telegram_retry_delay(exc, attempt))
    if last:
        raise last
    raise RuntimeError("Telegram operation failed")


async def send_image(msg, path: Path, caption: str, tmp: Path) -> int:
    final = await asyncio.to_thread(fit_image, path, tmp / "fit_images")
    async def send_once():
        with final.open("rb") as fh:
            return await msg.reply_photo(photo=InputFile(fh, read_file_handle=False), caption=caption or None)
    await _retry_telegram(send_once)
    return 1


def has_audio(path: Path) -> bool:
    from media_io import probe
    try:
        return any(s.get("codec_type") == "audio" for s in probe(path)["streams"])
    except (RuntimeError, OSError):
        return False


async def send_album(msg, files: list[Path], caption: str, tmp: Path, progress: dict | None = None) -> int:
    """Preserve order; group compatible native types in batches of at most ten."""
    from media_io import lossless_parts, native_audio, native_video
    prepared = []
    for index, path in enumerate(files):
        if progress is not None:
            progress["phase"] = f"Preparing media {index + 1}/{len(files)}"
        folder = tmp / "delivery" / str(index)
        kind = await asyncio.to_thread(classify, path)
        if kind == "image":
            prepared.append(("image", await asyncio.to_thread(fit_image, path, folder)))
        elif kind in {"video", "animation"}:
            final = await asyncio.to_thread(native_video, path, folder)
            parts = await asyncio.to_thread(lossless_parts, final, folder / "parts", MAX_BYTES)
            delivery_kind = "animation" if kind == "animation" and len(files) == len(parts) == 1 else "video"
            prepared.extend((delivery_kind, part) for part in parts)
        elif kind == "audio":
            path = await asyncio.to_thread(native_audio, path, folder)
            parts = await asyncio.to_thread(lossless_parts, path, folder / "parts", MAX_BYTES)
            prepared.extend(("audio", part) for part in parts)
        else:
            raise RuntimeError("Unsupported media type; no photo substitution performed")
    sent = 0
    while sent < len(prepared):
        family = "audio" if prepared[sent][0] == "audio" else "visual"
        batch = []
        for kind, path in prepared[sent:sent + 10]:
            if ("audio" if kind == "audio" else "visual") != family:
                break
            batch.append((kind, path))
        label = caption if not sent else ""
        if progress is not None:
            progress["phase"] = f"Sending {sent + 1}-{sent + len(batch)}/{len(prepared)}"
        async def send_once():
            with ExitStack() as stack:
                media = []
                for offset, (kind, path) in enumerate(batch):
                    upload = InputFile(stack.enter_context(path.open("rb")), filename=path.name, read_file_handle=False)
                    item_caption = label if offset == 0 else None
                    if len(batch) == 1:
                        if kind == "image":
                            return await msg.reply_photo(photo=upload, caption=item_caption)
                        if kind == "video":
                            return await msg.reply_video(video=upload, caption=item_caption, supports_streaming=True)
                        if kind == "animation":
                            return await msg.reply_animation(animation=upload, caption=item_caption)
                        return await msg.reply_audio(audio=upload, caption=item_caption)
                    cls = {"image": InputMediaPhoto, "video": InputMediaVideo, "audio": InputMediaAudio}[kind]
                    kwargs = {"supports_streaming": True} if kind == "video" else {}
                    media.append(cls(media=upload, caption=item_caption, **kwargs))
                return await msg.reply_media_group(media=media)
        if progress is not None:
            progress["attempted"] = True
        mark_state("sending")
        await _retry_telegram(send_once)
        sent += len(batch)
        if progress is not None:
            progress["sent"] = sent
    return sent


def post_mp3_button(uid: int, url: str, sources: list[Path], title: str) -> InlineKeyboardMarkup:
    children = [create_action(uid, url, path, title, index) for index, path in enumerate(sources)]
    token = create_action(uid, url, title=title)
    with _action_lock:
        actions = _load_actions()
        actions[token].update(post=True, children=children)
        _save_actions(actions)
    return InlineKeyboardMarkup([[InlineKeyboardButton("MP3", callback_data=f"mp3|{token}")]])


async def send_animation(msg, path: Path, caption: str, tmp: Path) -> int:
    if path.stat().st_size > MAX_BYTES or path.suffix.lower() not in {".gif", ".mp4"}:
        return await send_document(msg, path, caption)
    async def send_once():
        with path.open("rb") as fh:
            return await msg.reply_animation(animation=InputFile(fh, read_file_handle=False), caption=caption or None, filename=path.name)
    try:
        await _retry_telegram(send_once)
    except BadRequest:
        return await send_document(msg, path, caption)
    return 1


async def _send_video_file(msg, path: Path, caption: str, reply_markup=None) -> None:
    async def send_once():
        with path.open("rb") as fh:
            return await msg.reply_video(
                video=InputFile(fh, read_file_handle=False),
                caption=caption or None,
                filename=path.name,
                supports_streaming=True,
                reply_markup=reply_markup,
            )
    await _retry_telegram(send_once)


async def send_video(msg, path: Path, caption: str, max_height: int, tmp: Path, reply_markup=None) -> int:
    from media_io import lossless_parts, streamable, prepare_video
    path = await asyncio.to_thread(prepare_video, path, tmp / "remux")
    parts = await asyncio.to_thread(lossless_parts, path, tmp / (path.stem + "_parts"), MAX_BYTES)
    for index, part in enumerate(parts, 1):
        label = f"{caption}\nPart {index}/{len(parts)}".strip() if len(parts) > 1 else caption
        markup = reply_markup if index == len(parts) else None
        if await asyncio.to_thread(streamable, part):
            try:
                await _send_video_file(msg, part, label, markup)
                continue
            except BadRequest as exc:
                if not any(t in str(exc).lower() for t in ("video", "media", "file", "content")):
                    raise
        await send_document(msg, part, label, markup)
    return len(parts)


def safe_media_title(value: str) -> str:
    value = re.sub(r"[\r\n\t]+", " ", value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value[:120] or "Veltrix Audio"


async def send_audio(msg, path: Path, caption: str, mode: str, tmp: Path, title: str = "") -> int:
    from media_io import lossless_parts
    final = path
    if mode in AUDIO_PRESETS:
        preset = AUDIO_PRESETS[mode]
        folder = tmp / "audio" / path.stem
        folder.mkdir(parents=True, exist_ok=True)
        ext = ".m4a" if preset["codec"] == "m4a" else ".mp3"
        final = folder / ("audio" + ext)
        codec = "aac" if ext == ".m4a" else "libmp3lame"
        await asyncio.to_thread(ffmpeg, ["ffmpeg", "-nostdin", "-y", "-i", str(path), "-vn", "-c:a", codec,
                                        "-b:a", str(preset["bitrate"]) + "k", str(final)])
    parts = await asyncio.to_thread(lossless_parts, final, tmp / (path.stem + "_audio_parts"), MAX_BYTES)
    for index, part in enumerate(parts, 1):
        label = f"{caption}\nPart {index}/{len(parts)}".strip() if len(parts) > 1 else caption
        if part.suffix.lower() not in {".mp3", ".m4a"}:
            await send_document(msg, part, label)
            continue
        async def send_once():
            with part.open("rb") as fh:
                return await msg.reply_audio(audio=InputFile(fh, read_file_handle=False), caption=label or None, filename=part.name,
                                             title=safe_media_title(title) if title else None)
        await _retry_telegram(send_once)
    return len(parts)


async def send_document(msg, path: Path, caption: str, reply_markup=None) -> int:
    if path.stat().st_size > MAX_BYTES:
        raise RuntimeError("Unsupported media is over Telegram's upload limit.")

    async def send_once():
        with path.open("rb") as fh:
            return await msg.reply_document(document=InputFile(fh, read_file_handle=False), caption=caption or None, filename=path.name, reply_markup=reply_markup)

    await _retry_telegram(send_once)
    return 1


async def send_mp3_album(msg, sources: list[Path], tmp: Path, progress: dict | None = None) -> int:
    outputs = []
    for index, source in enumerate(sources):
        if progress is not None:
            progress["phase"] = f"Extracting audio {index + 1}/{len(sources)}"
        if not await asyncio.to_thread(has_audio, source):
            continue
        folder = tmp / "mp3" / str(index)
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / f"audio_{index + 1:02d}.mp3"
        from media_io import probe
        info = await asyncio.to_thread(probe, source)
        track = next(s for s in info["streams"] if s.get("codec_type") == "audio")
        codec_options = ["-c:a", "copy"] if track.get("codec_name") == "mp3" else ["-c:a", "libmp3lame", "-b:a", "320k"]
        await asyncio.to_thread(ffmpeg, ["ffmpeg", "-nostdin", "-y", "-i", str(source),
                                        "-map", "0:a:0", "-vn", *codec_options, str(output)])
        outputs.append(output)
    if not outputs:
        raise RuntimeError("No accessible audio track was found in this post.")
    if progress is None:
        return await send_album(msg, outputs, "Veltrix Downloader · MP3", tmp)
    return await send_album(msg, outputs, "Veltrix Downloader · MP3", tmp, progress)


async def run_cached_audio_job(msg, context, uid: int, source: Path | list[Path], title: str, status) -> None:
    sources = source if isinstance(source, list) else [source]
    await run_job(msg, context, uid, "", "mp3_320", status,
                  cached_sources=sources, known_meta={"title": title})


async def run_job(
    msg, context, uid: int, url: str, mode: str, status=None,
    selected_audio_index: int | None = None,
    known_meta: dict[str, Any] | None = None,
    cached_sources: list[Path] | None = None,
) -> None:
    lock = job_lock(uid)
    progress = {"sent": 0, "phase": "Queued"}
    status = status if status is not None else DeferredStatus(msg)
    started_at = time.monotonic()
    tmp = None
    active = False
    delivered = False
    queued = lock.locked() or global_sem().locked()
    metric_add("jobs_started", 1)
    if queued:
        metric_add("queued_jobs", 1)

    async def typing():
        previous = ""
        while True:
            try:
                await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_DOCUMENT,
                                                   connect_timeout=8, read_timeout=8)
                text = f"⏳ {progress['phase']} · {int(time.monotonic() - started_at) // 15 * 15}s"
                if text != previous:
                    await edit_status(status, text)
                    previous = text
            except TelegramError:
                pass
            await asyncio.sleep(4)

    ticker = asyncio.create_task(typing())
    try:
        async with lock, global_sem():
            if queued:
                metric_add("queued_jobs", -1)
                queued = False
            active = True
            metric_add("active_jobs", 1)
            mark_state("downloading")
            tmp = Path(tempfile.mkdtemp(prefix="vx_"))
            progress["phase"] = f"Downloading from {platform_label(url)}"
            meta = dict(known_meta or {})
            if cached_sources and all(p.is_file() for p in cached_sources):
                files = []
                for i, path in enumerate(cached_sources):
                    local = tmp / f"cached_{i}{path.suffix}"
                    await asyncio.to_thread(shutil.copy2, path, local)
                    files.append(local)
            else:
                files = await download_in_worker(url, mode, tmp, meta)
            if mode in AUDIO_PRESETS:
                if selected_audio_index is not None:
                    if selected_audio_index < 0 or selected_audio_index >= len(files):
                        raise RuntimeError("Requested media item is no longer available")
                    files = [files[selected_audio_index]]
                sources = await asyncio.to_thread(lambda: [p for p in files + files_in(tmp / "soundtrack") if has_audio(p)])
                sent = await send_mp3_album(msg, sources, tmp, progress)
                if not sent:
                    raise RuntimeError("No accessible audio track")
                delivered = True
                mark_state("done")
                ticker.cancel()
                await asyncio.gather(ticker, return_exceptions=True)
                try:
                    await status.delete()
                except TelegramError:
                    await edit_status(status, "✅ MP3 delivered")
            else:
                caption = f"⚡ Veltrix Downloader · {platform_label(url)}"
                sent = await send_album(msg, files, caption, tmp, progress)
                if not sent:
                    raise RuntimeError("Nothing downloadable was returned")
                delivered = True
                # Persist delivery before optional cache/status operations.
                mark_state("done")
                sources = await asyncio.to_thread(lambda: [p for p in files + files_in(tmp / "soundtrack") if has_audio(p)])
                markup = None
                try:
                    markup = await asyncio.to_thread(post_mp3_button, uid, url, sources, str(meta.get("title") or ""))
                except OSError:
                    log.warning("Media delivered; MP3 cache could not be saved")
                ticker.cancel()
                await asyncio.gather(ticker, return_exceptions=True)
                await edit_status(status, f"✅ {len(files)} media delivered" + (" · MP3" if markup else ""), reply_markup=markup)
            metric_add("jobs_succeeded", 1)
    except Exception as exc:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        count = progress["sent"]
        if delivered:
            metric_add("jobs_succeeded", 1)
            log.warning("Delivery completed; follow-up failed: %s", type(exc).__name__)
            await edit_status(status, "✅ Media delivered. Send the link again if the MP3 action is unavailable.")
            return
        metric_add("jobs_failed", 1)
        uncertain = bool(progress.get("attempted")) and not connection_failed_before_send(exc)
        mark_state("interrupted" if count or uncertain else "failed")
        log.error("job failed: %s", type(exc).__name__)
        markup = None
        if mode == AUTO_MODE and not count and not uncertain:
            try:
                token = create_action(uid, url)
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("Try again", callback_data=f"retry|{token}")]])
            except OSError:
                pass
        detail = f" {count} media parts already delivered." if count else ""
        await edit_status(status, "❌ " + friendly_error(platform_of(url) or "", exc) + detail, reply_markup=markup)
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        if active:
            metric_add("active_jobs", -1)
        if queued:
            metric_add("queued_jobs", -1)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                disk = shutil.disk_usage(tempfile.gettempdir())
                free_mb = disk.free // (1024 * 1024)
            except Exception:
                free_mb = -1
            from telegram_network import snapshot, environment_proxy
            connectivity = snapshot()
            body = json.dumps({
                "ok": True,
                "telegram": connectivity,
                "telegram_route": {
                    "ipv4_first": os.getenv("TELEGRAM_IPV4", "0") == "1",
                    "proxy_configured": bool(os.getenv("TELEGRAM_PROXY")),
                    "environment_proxy_available": bool(not TELEGRAM_API_BASE and environment_proxy()),
                },
                "service": "veltrix-downloader",
                "version": VERSION,
                "platforms": list(PLATFORMS),
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "ffprobe": bool(shutil.which("ffprobe")),
                "deno": bool(DENO_BIN),
                "temp_free_mb": free_mb,
                "cache_mb": (
                    sum(p.stat().st_size for p in CACHE_DIR.glob("*") if p.is_file()) // (1024 * 1024)
                    if CACHE_DIR.exists() else 0
                ),
                "pid": os.getpid(),
                "metrics": metric_snapshot(),
            }).encode()
            code = 200 if self.path in {"/", "/health", "/healthz", "/readyz"} else 404
            if self.path == "/readyz" and not connectivity['connected_recently']:
                code = 503
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, fmt, *args):
            return
    def serve() -> None:
        try:
            ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
        except OSError as exc:
            log.warning("health server unavailable on port %s: %s", port, exc)

    Thread(target=serve, daemon=True).start()


def validate_media_files(paths: list[Path]) -> list[Path]:
    from media_io import probe
    for path in paths:
        probe(path)
    # Preserve intentional repeated carousel items. Source IDs define identity.
    return paths


_budget_checked_at = 0.0


def check_download_budget(progress: dict) -> None:
    global _budget_checked_at
    root = os.getenv("VELTRIX_JOB_ROOT")
    if not root or time.monotonic() - _budget_checked_at < 1:
        return
    _budget_checked_at = time.monotonic()
    if shutil.disk_usage(root).free < 128 * 1024 * 1024:
        raise RuntimeError("Insufficient storage; download stopped")
    used = sum(p.stat().st_size for p in Path(root).rglob("*") if p.is_file())
    if used > MAX_JOB_BYTES:
        raise RuntimeError("Job storage limit exceeded; download stopped")


def fetch_public_page(url: str) -> httpx.Response:
    for attempt in range(3):
        try:
            return _fetch_public_page_once(url)
        except httpx.TransportError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def _fetch_public_page_once(url: str) -> httpx.Response:
    current = url
    with httpx.Client(headers=request_headers(url), follow_redirects=False, timeout=25,
                      proxy=PROXY or None, trust_env=False) as client:
        for _ in range(6):
            if not safe_remote_url(current):
                raise RuntimeError("Unsafe page redirect")
            with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    current = urljoin(current, response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 12 * 1024 * 1024:
                        raise RuntimeError("Public page exceeds metadata size limit")
                    chunks.append(chunk)
                return httpx.Response(response.status_code, headers=response.headers,
                                      content=b"".join(chunks), request=response.request)
    raise RuntimeError("Too many page redirects")


def snapchat_story_entries(doc: dict, url: str) -> list[dict]:
    props = _dig_dict(doc, "props", "pageProps")
    match = re.search(r"/(?:add|@)/?([^/]+)(?:/([^/?]+))?", urlparse(url).path)
    requested = match.group(2) if match else None
    if requested in {"story", "stories"}:
        requested = None
    story = props.get("story") or {}
    snaps = list(story.get("snapList") or [])
    if not snaps and not requested:
        # Only an explicitly linked public profile can select its current highlight.
        highlights = props.get("curatedHighlights") or []
        if highlights:
            snaps = list(highlights[0].get("snapList") or [])
    if requested:
        snaps = [s for s in snaps if str((s.get("snapId") or {}).get("value")) == requested]
    output = []
    for snap in snaps:
        urls = snap.get("snapUrls") or {}
        media_url = urls.get("mediaUrl")
        if isinstance(media_url, dict):
            media_url = media_url.get("value")
        media_type = snap.get("snapMediaType")
        if not media_url or media_type not in {0, 1}:
            raise RuntimeError("Incomplete story metadata")
        output.append({"url": str(media_url), "kind": "image" if media_type == 0 else "video"})
    return output


def snapchat_preload(page: str) -> dict:
    class Links(HTMLParser):
        urls = None
        def handle_starttag(self, tag, attrs):
            fields = dict(attrs)
            if tag == "link" and fields.get("rel") == "preload" and fields.get("as") == "video":
                url = fields.get("href", "")
                host = urlparse(url).hostname or ""
                if host.endswith(".sc-cdn.net"):
                    self.urls.append(url)
    parser = Links()
    parser.urls = []
    parser.feed(page)
    urls = list(dict.fromkeys(parser.urls))
    if len(urls) != 1:
        return {}
    return {"url": urls[0], "entries": [{"url": urls[0], "kind": "video"}]}


async def download_in_worker(url: str, mode: str, tmp: Path, meta: dict) -> list[Path]:
    tmp = tmp.resolve()
    (tmp / "request.json").write_text(json.dumps({"url": url, "mode": mode, "meta": meta}))
    with (tmp / "worker.log").open("wb") as log_file:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name("download_worker.py")), str(tmp),
            stdout=log_file, stderr=log_file, start_new_session=(os.name == "posix"),
        )
        try:
            await asyncio.wait_for(proc.wait(), JOB_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError):
            if proc.returncode is None:
                try:
                    if os.name == "posix":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
            if asyncio.current_task().cancelling():
                raise
            raise RuntimeError("Download deadline exceeded; worker stopped. Try the link later.")
    result_path = tmp / "result.json"
    if proc.returncode or not result_path.is_file():
        raise RuntimeError("Download worker stopped unexpectedly")
    result = json.loads(result_path.read_text())
    if result.get("error"):
        log.warning("worker %s: %s", platform_of(url), result.get("diagnostic", "no diagnostic"))
        raise WorkerFailure(result["error"])
    files = [Path(p).resolve() for p in result.get("files", [])]
    if not files or any(not p.is_relative_to(tmp) or not p.is_file() for p in files):
        raise RuntimeError("Invalid download worker result")
    return files


def telegram_request(pool_size: int = 16):
    from telegram_network import ConnectFallback, ObservedRequest, environment_proxy
    limits = httpx.Limits(max_connections=pool_size, max_keepalive_connections=pool_size)
    # Prefer the selected route; retry through the other route only when no
    # request bytes were sent (connect error/timeout).
    ipv4 = os.getenv("TELEGRAM_IPV4", "0") == "1"
    # An explicit Telegram proxy takes precedence. A generic shell proxy is
    # attempted only after both direct connections failed before sending bytes.
    proxy = os.getenv("TELEGRAM_PROXY") or None
    transport = httpx.AsyncHTTPTransport(
        local_address="0.0.0.0" if ipv4 else None, limits=limits,
        retries=1 if proxy else 0, proxy=proxy,
    )
    if not proxy:
        alternate = httpx.AsyncHTTPTransport(
            local_address=None if ipv4 else "0.0.0.0", limits=limits, retries=0,
        )
        transport = ConnectFallback(transport, alternate)
        # A custom Bot API base may be local HTTP: never send that URL through
        # a generic system proxy chosen for api.telegram.org.
        backup_proxy = environment_proxy() if not TELEGRAM_API_BASE else None
        if backup_proxy:
            transport = ConnectFallback(
                transport, httpx.AsyncHTTPTransport(limits=limits, retries=0, proxy=backup_proxy),
            )
    return ObservedRequest(connection_pool_size=pool_size, read_timeout=120, write_timeout=120,
                           connect_timeout=10, pool_timeout=10, media_write_timeout=1800,
                           httpx_kwargs={"transport": transport, "trust_env": False})


def application_builder(token: str):
    builder = (Application.builder().token(token).request(telegram_request())
               .get_updates_request(telegram_request(1)).concurrent_updates(8)
               .post_init(resume_jobs).post_stop(stop_jobs).post_shutdown(stop_jobs))
    if TELEGRAM_API_BASE:
        builder = builder.base_url(TELEGRAM_API_BASE + "/bot").base_file_url(TELEGRAM_API_BASE + "/file/bot")
        # Upload bytes even when the local server is on a different machine.
        builder = builder.local_mode(False)
    return builder


async def resume_jobs(app) -> None:
    log.info("Telegram identity verified: @%s", app.bot.username)
    manager = job_manager()
    ready, uncertain = manager.recover()
    async def execute(row):
        target = ChatTarget(app.bot, row['chat'], row['thread'])
        await run_job(target, app, row['uid'], row['url'], row['mode'])
    for row in ready:
        manager.launch(row, execute)
    async def notify_interrupted(row):
        target = ChatTarget(app.bot, row['chat'], row['thread'])
        try:
            await target.reply_text("⚠️ A restart interrupted media delivery. Check the received items before resending the link. /status shows the job state.",
                                    connect_timeout=8, read_timeout=8)
        except TelegramError:
            pass
    for row in uncertain:
        manager.launch(row, notify_interrupted)
    log.info("Job recovery: %s queued, %s interrupted deliveries", len(ready), len(uncertain))


async def stop_jobs(app) -> None:
    from media_process import stop_all
    stop_all()
    if _jobs is not None:
        await _jobs.shutdown()


async def on_error(update, context) -> None:
    exc = context.error
    if isinstance(exc, Conflict):
        log.error("Telegram polling conflict: another instance is using this token. Stop the other worker/webhook.")
    elif isinstance(exc, NetworkError):
        log.warning("Telegram connection interrupted: %s; polling will reconnect", type(exc).__name__)
    else:
        log.error("Update failed: %s", type(exc).__name__)


def register_handlers(app) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)


def acquire_instance_lock() -> None:
    global _instance_lock
    if os.name != "posix":
        return
    import fcntl
    handle = (DATA_DIR / 'worker.lock').open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        log.error("Another Veltrix worker is already running in this data directory")
        raise SystemExit(73)
    _instance_lock = handle


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    acquire_instance_lock()
    cleanup_stale_temp()
    cleanup_media_cache()
    start_health_server()
    app = application_builder(BOT_TOKEN).build()
    register_handlers(app)
    log.info("Veltrix Downloader %s connecting to Telegram", VERSION)
    try:
        app.run_polling(drop_pending_updates=False, bootstrap_retries=3, timeout=25)
    except InvalidToken:
        log.error("BOT_TOKEN is invalid. Correct .env before restarting.")
        raise SystemExit(78)
    except NetworkError as exc:
        log.warning("Telegram startup offline: %s; supervisor will retry", type(exc).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
