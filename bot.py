#!/usr/bin/env python3
"""Veltrix Downloader backend.

Public-media downloader for YouTube, Instagram, Snapchat and Pinterest.
Preserves source quality with bounded platform workers and Telegram delivery.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sys
import signal
import subprocess
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from dotenv import load_dotenv
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, InputFile, InputMediaPhoto, InputMediaVideo,
                      InputMediaAudio, InputMediaDocument, MessageEntity, Update)
from telegram.constants import ChatAction
from telegram.error import BadRequest, Conflict, InvalidToken, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL
from runtime_jobs import ChatTarget, JobManager, mark_state
from cobalt_provider import ProviderError
from newisty_provider import ProviderRateLimit
from source_metadata import IncompletePost, media_quality
from transfer import TransferLimit

load_dotenv()
VERSION = "9.15.1"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_RELAY_BASE = os.getenv('TELEGRAM_RELAY_BASE', '').strip().rstrip('/')
if TELEGRAM_RELAY_BASE == '0':
    TELEGRAM_RELAY_BASE = ''
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
YT_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
YT_POST_RE = re.compile(r'^/(?:(?:@|%40)[^/]+/)?post/([A-Za-z0-9_-]+)/?$', re.I)
INSTAGRAM_POST_RE = re.compile(
    r'^/(?:(?!share/|direct/|stories/)[\w.]+/)?(reels?|p|tv)/([A-Za-z0-9_-]+)(?:/embed(?:/captioned)?)?/?$', re.I)
PINTEREST_PIN_RE = re.compile(r"^/pin/(?:[^/]+--)?(\d+)/?$", re.I)
SNAP_SPOTLIGHT_RE = re.compile(r'^/(?:(?:@|%40)[^/]+/)?spotlight/([A-Za-z0-9_-]+)(?:/embed)?/?$', re.I)
SNAP_PROFILE_RE = re.compile(r'^/(?:add/|@|%40)([A-Za-z0-9_.-]+)(?:/(.*))?$', re.I)
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


def cache_stored_at(path: Path) -> float:
    # Hard links retain the source modification time, which can be years old.
    stamp, separator, _ = path.stem.partition('_')
    if separator and stamp.isdecimal() and len(stamp) == 19:
        return int(stamp) / 1_000_000_000
    return path.stat().st_mtime  # Cache entries from older versions.


def cleanup_media_cache() -> None:
    now = time.time()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    try:
        for path in CACHE_DIR.iterdir():
            if not path.is_file():
                continue
            try:
                if now - cache_stored_at(path) > CACHE_TTL_SECONDS:
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
            for path in sorted(files, key=cache_stored_at):
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
    *,
    children: list[str] | None = None,
    refresh_audio: bool = False,
) -> str:
    """Bind an inline action to the exact request and optionally cache its source."""
    stamp = time.time_ns()
    now = stamp // 1_000_000_000
    raw = f"{uid}:{url}:{stamp}".encode()
    token = hashlib.sha256(raw).hexdigest()[:20]
    cleanup_media_cache()

    cache_path = ""
    if source_path is not None:
        try:
            if source_path.exists() and 0 < source_path.stat().st_size <= CACHE_MAX_BYTES:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                suffix = source_path.suffix.lower() or ".bin"
                dest = CACHE_DIR / f"{stamp}_{token}{suffix}"
                try:
                    os.link(source_path, dest)
                except OSError:
                    shutil.copyfile(source_path, dest)
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
        if children is not None:
            actions[token].update(post=True, children=children, refresh_audio=refresh_audio)
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
            if not path.is_file() or time.time() - cache_stored_at(path) > CACHE_TTL_SECONDS:
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
    match = URL_RE.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(").,]}>\"'")
    if url.lower().startswith("www."):
        url = "https://" + url
    if platform_of(url) == 'youtube':
        parsed = urlparse(url)
        parts = parsed.path.strip('/').split('/')
        if parsed.path.rstrip('/') == '/watch':
            video_id = parse_qs(parsed.query).get('v', [''])[0]
        elif (parsed.hostname or '').lower() in {'youtu.be', 'www.youtu.be'} and len(parts) == 1:
            video_id = parts[0]
        elif len(parts) == 2 and parts[0] in {'shorts', 'live', 'embed', 'v'}:
            video_id = parts[1]
        else:
            video_id = ''
        if YT_ID_RE.fullmatch(video_id):
            return f'https://www.youtube.com/watch?v={video_id}'
    return url


def youtube_playlist_url(url: str) -> bool:
    parsed = urlparse(url)
    return (platform_of(url) == 'youtube' and parsed.path.rstrip('/').lower() == '/playlist'
            and bool(parse_qs(parsed.query).get('list')))


def youtube_post_id(url: str) -> str:
    parsed = urlparse(url)
    if match := YT_POST_RE.fullmatch(parsed.path):
        return match[1]
    if parsed.path.rstrip('/').endswith('/community'):
        linked = parse_qs(parsed.query).get('lb', [''])[0]
        if re.fullmatch(r'[A-Za-z0-9_-]{5,128}', linked):
            return linked
    return ''


def platform_of(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower().rstrip('.').removeprefix('www.')
        if parsed.username is not None or parsed.password is not None:
            return None
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
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
        # Snapchat's embed HTML may advertise a broken compressed stream.
        # Requesting the uncompressed page avoids httpx.DecodingError.
        headers["Accept-Encoding"] = "identity"
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = "none"
        headers["Upgrade-Insecure-Requests"] = "1"
    elif platform == "pinterest":
        headers["Referer"] = "https://www.pinterest.com/"
    return headers


def resolve_public_redirect(url: str) -> str:
    parsed = urlparse(url)
    platform = platform_of(url)
    # A full Spotlight URL already contains the exact post ID. Probing it here
    # used to fetch the same page twice, including two identical HTTP 404s.
    if platform == 'snapchat' and (SNAP_SPOTLIGHT_RE.fullmatch(parsed.path) or SNAP_PROFILE_RE.fullmatch(parsed.path)):
        return url
    short_pin = (parsed.hostname or '').lower().removeprefix('www.') == 'pin.it'
    short_instagram = platform == 'instagram' and parsed.path.lower().startswith('/share/')
    if not (short_pin or short_instagram or platform == 'snapchat'):
        return url
    key = f'redirect:{url}'
    if cached := metadata_cache_get(key):
        return cached['url']
    try:
        response = fetch_public_page(url)
        final = str(response.url)
    except httpx.HTTPStatusError as exc:
        final = str(exc.request.url)
    except Exception as exc:
        log.info("Share resolution failed: %s", type(exc).__name__)
        return url
    if platform_of(final) == platform:
        metadata_cache_put(key, {'url': final})
        return final
    return url


def extractor_candidates(url: str) -> list[str]:
    """Return canonical public URL variants, keeping useful share context."""
    platform = platform_of(url)
    resolved = resolve_public_redirect(url)
    out = [resolved, url] if resolved != url else [url]
    try:
        parsed = urlparse(resolved)
        if platform == "instagram":
            match = INSTAGRAM_POST_RE.fullmatch(parsed.path)
            if match:
                kind = "reel" if match.group(1).lower() in {"reel", "reels"} else match.group(1).lower()
                out.insert(0, f"https://www.instagram.com/{kind}/{match.group(2)}/")
        elif platform == "snapchat":
            match = SNAP_SPOTLIGHT_RE.fullmatch(parsed.path)
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
            match = PINTEREST_PIN_RE.fullmatch(parsed.path)
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
            match = INSTAGRAM_POST_RE.fullmatch(parsed.path)
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


def _instagram_format_score(fmt: dict[str, Any], kind: str) -> tuple[float, float, float]:
    return media_quality(fmt)


def extract_instagram_public(url: str, *, graphql: bool = False) -> dict[str, Any]:
    """Dedicated public Instagram metadata path for Reels/posts/carousels."""
    cache_key = f'instagram:{graphql}:{url}'
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
    candidate = extractor_candidates(url)[0]
    match = INSTAGRAM_POST_RE.fullmatch(urlparse(candidate).path)
    if not match:
        return {}
    if graphql:
        info = downloader.media_extractor._extract_from_graphql(match[2])
    else:
        info = downloader.get_info(candidate)
    payload = info if isinstance(info, dict) else {}
    if payload.get('id') not in {None, '', 'unknown', match[2]}:
        raise RuntimeError('Instagram returned media for a different post')
    metadata_cache_put(cache_key, payload)
    return payload


def attach_post_music(video: Path, music: Path, folder: Path, *, loop_audio: bool = True) -> Path:
    """Add a post's one exposed music track to a video with no native audio."""
    from media_io import probe
    if has_audio(video) or not has_audio(music):
        return video
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f'{video.stem}_post_music.mkv'
    try:
        source_duration = float(probe(video).get('format', {}).get('duration') or 0)
        music_duration = float(probe(music).get('format', {}).get('duration') or 0)
        # A matching soundtrack needs no padding, looping or lossy AAC encode.
        copied = False
        if source_duration > 0 and abs(source_duration - music_duration) <= .1:
            try:
                ffmpeg(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(video), '-i', str(music),
                        '-map', '0:V:0', '-map', '1:a:0', '-c', 'copy', str(output)])
                copied = has_audio(output)
            except (RuntimeError, subprocess.SubprocessError):
                pass
        if not copied:
            ffmpeg(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(video),
                    *(['-stream_loop', '-1'] if loop_audio else []), '-i', str(music),
                    '-map', '0:V:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '256k',
                    *([] if loop_audio else ['-af', 'apad']), '-shortest', str(output)])
        if not has_audio(output):
            raise RuntimeError('Could not attach the source audio to its video')
        output_duration = float(probe(output).get('format', {}).get('duration') or 0)
        if source_duration > 0 and abs(output_duration - source_duration) > max(0.25, source_duration * 0.03):
            raise RuntimeError('Attaching audio changed the video duration')
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        output.unlink(missing_ok=True)
        raise
    return output


def attach_item_audio(path: Path, urls: list[str], folder: Path, referer: str) -> Path:
    """Keep audio from this item's metadata on the same video."""
    if not urls or classify(path) != 'video' or has_audio(path):
        return path
    audio = folder / 'track.m4a'
    result = path
    try:
        for url in urls:
            if _download_direct_file(url, audio, referer=referer) and has_audio(audio):
                try:
                    result = attach_post_music(path, audio, folder, loop_audio=False)
                    return result
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    log.warning('Item audio could not be merged: %s', type(exc).__name__)
            audio.unlink(missing_ok=True)
        return path
    finally:
        if result == path:
            audio.unlink(missing_ok=True)


def download_instagram_dedicated(
    url: str,
    tmpdir: str,
    info: dict[str, Any] | None = None,
    *,
    graphql: bool = False,
) -> tuple[list[Path], str]:
    """Download exact Instagram entries in original carousel order."""
    if not isinstance(info, dict) or not info.get("entries"):
        try:
            info = extract_instagram_public(url, graphql=True) if graphql else extract_instagram_public(url)
        except Exception as exc:
            if fatal_download_error(exc):
                raise
            log.info("Instagram dedicated metadata failed: %s", str(exc)[:180])
            return [], ""

    entries = info.get("entries") if isinstance(info.get("entries"), list) else []
    if not entries:
        return [], str(info.get("type") or "").lower()

    media = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('kind') not in {'video', 'image'}:
            raise RuntimeError('Incomplete Instagram carousel metadata')
        formats = [fmt for fmt in (entry.get('formats') or [])
                   if isinstance(fmt, dict) and fmt.get('url')]
        media.append({**entry, 'formats': sorted(formats, key=media_quality, reverse=True)})
    media.extend({'kind': 'audio', 'scope': 'post', 'formats': [{'url': url}]}
                 for url in dict.fromkeys(info.get('audio_urls') or []))
    files = download_source_entries(media, Path(tmpdir) / 'instagram', url)
    return files, str(info.get('type') or '').lower()


def _dig_dict(node: Any, *keys: str) -> dict[str, Any]:
    cur = node
    for key in keys:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(key)
    return cur if isinstance(cur, dict) else {}


def _snap_requested_id(url: str, doc: dict[str, Any]) -> str:
    match = SNAP_SPOTLIGHT_RE.fullmatch(urlparse(url).path)
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
                video_meta = meta.get('videoMetadata') if isinstance(meta, dict) else None
                if isinstance(video_meta, dict) and video_meta.get('contentUrl'):
                    return meta

    query_id = str(_dig_dict(doc, "query").get("snapID") or "")
    top = props.get("videoMetadata")
    # Spotlight pages can expose the selected clip at the top level while
    # spotlightStories contains only unrelated recommendations. Trust the top
    # level only when the page's own snapID matches the requested clip.
    if requested_id and query_id != requested_id:
        return {}
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


def _snap_info_from_exact_page(page: str, page_url: str) -> dict[str, Any]:
    """Use page-owned video tags only after checking the exact Spotlight ID."""
    requested = _snap_requested_id(page_url, {})
    if not requested or not _snap_page_owns_spotlight(page, page_url):
        return {}
    videos = list(dict.fromkeys(og_values(page, {
        'og:video', 'og:video:url', 'og:video:secure_url', 'twitter:player:stream',
    })))
    if len(videos) != 1 or not safe_remote_url(videos[0]):
        return {}
    return {'url': videos[0], 'entries': [{'url': videos[0], 'kind': 'video'}],
            'id': requested, 'page_url': page_url}


def _snap_page_owns_spotlight(page: str, page_url: str) -> bool:
    """Check page identity before accepting its one primary video preload."""
    requested = _snap_requested_id(page_url, {})
    if not requested:
        return False
    canonical = og_values(page, {'og:url'})
    for tag in re.findall(r'<link\b[^>]*>', page, flags=re.I):
        fields = dict(re.findall(r'([\w:-]+)\s*=\s*["\']([^"\']*)["\']', tag, flags=re.I))
        if 'canonical' in fields.get('rel', '').lower().split() and fields.get('href'):
            canonical.append(urljoin(page_url, html.unescape(fields['href'])))
    if canonical and any(platform_of(value) != 'snapchat' or _snap_requested_id(value, {}) != requested
                         for value in canonical):
        return False
    match = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', page, re.I | re.S)
    if match:
        try:
            doc = json.loads(match.group(1))
        except ValueError:
            return False
        query_id = str(_dig_dict(doc, 'query').get('snapID') or '')
        if query_id:
            return query_id == requested
        stories = _dig_dict(doc, 'props', 'pageProps', 'spotlightFeed').get('spotlightStories') or []
        return bool(canonical) or any(str(_dig_dict(item, 'story', 'storyId').get('value') or '') == requested
                                      for item in stories if isinstance(item, dict))
    # Some Snapchat Spotlight HTML has one primary video preload but no
    # canonical or Next.js tags. The page was fetched from the exact ID route
    # and the caller also requires precisely one Snapchat CDN video.
    return True


def snapchat_lookup(snap_id: str, *, client_factory=httpx.Client) -> dict[str, Any]:
    """Use Snapchat's exact-ID public lookup if its page has no usable data."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{30,160}', snap_id):
        return {}
    try:
        with client_factory(timeout=12, follow_redirects=False, proxy=PROXY or None, trust_env=False) as client:
            with client.stream('POST', 'https://www.snapchat.com/api/spotlight-up-next',
                               headers=request_headers('https://www.snapchat.com/'),
                               json={'source': 'lookup', 'videoIds': [snap_id]}) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes(65536):
                    content.extend(chunk)
                    if len(content) > 2 * 1024 * 1024:
                        return {}
                payload = json.loads(content)
        for item in payload.get('items') or []:
            video = item.get('video') if isinstance(item, dict) else None
            if not isinstance(video, dict) or video.get('rawSnapId') != snap_id:
                continue
            url = video.get('videoUrl')
            if not isinstance(url, str) or not safe_remote_url(url):
                continue
            return {'id': snap_id, 'url': url, 'title': str(video.get('caption') or ''),
                    'entries': [{'kind': 'video', 'formats': [{'url': url}]}]}
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    return {}


def extract_snapchat_public(url: str) -> dict[str, Any]:
    from source_metadata import snapchat_modern_video

    cached = metadata_cache_get(f"snapchat:{url}")
    if cached:
        return cached
    candidates = extractor_candidates(url)[:3]
    snap_id = _snap_requested_id(candidates[0], {})
    if snap_id:
        candidates.append(f"https://www.snapchat.com/spotlight/{snap_id}/embed")
    for candidate in dict.fromkeys(candidates):
        try:
            response = fetch_public_page(candidate)
        except (httpx.HTTPError, RuntimeError):
            continue
        page_url = str(response.url)
        if platform_of(page_url) != 'snapchat':
            continue
        candidate_id = _snap_requested_id(candidate, {})
        if candidate_id and _snap_requested_id(page_url, {}) != candidate_id:
            continue
        match = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', response.text, re.I | re.S)
        try:
            doc = json.loads(match.group(1)) if match else {}
        except ValueError:
            doc = {}
        if candidate_id:
            info = snapchat_modern_video(response.text, candidate_id)
            if info:
                info['page_url'] = page_url
                metadata_cache_put(f"snapchat:{url}", info)
                return info
            info = _snap_info_from_doc(doc, page_url)
            if info:
                info["entries"] = [{"url": info["url"], "kind": "video"}]
            elif _snap_page_owns_spotlight(response.text, page_url):
                info = _snap_info_from_exact_page(response.text, page_url) or snapchat_preload(response.text)
        else:
            profile = SNAP_PROFILE_RE.fullmatch(urlparse(candidate).path)
            redirected_profile = SNAP_PROFILE_RE.fullmatch(urlparse(page_url).path)
            if not profile or not redirected_profile or profile[1].casefold() != redirected_profile[1].casefold():
                continue
            entries = snapchat_story_entries(doc, candidate)
            info = {"entries": entries, "title": "Snapchat story"} if entries else {}
        if info:
            metadata_cache_put(f"snapchat:{url}", info)
            return info
    if snap_id:
        info = snapchat_lookup(snap_id)
        if info:
            metadata_cache_put(f"snapchat:{url}", info)
            return info
    return {}


def download_snapchat_dedicated(url: str, tmpdir: str, info: dict[str, Any] | None = None) -> list[Path]:
    info = info or extract_snapchat_public(url)
    entries = info.get("entries") or ([{"url": info["url"], "kind": "video"}] if info.get("url") else [])
    media = [{**entry, 'formats': entry.get('formats') or [{'url': entry['url']}]} for entry in entries]
    return download_source_entries(media, Path(tmpdir) / 'snapchat', 'https://www.snapchat.com/')


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


def requested_audio_track(entry: dict[str, Any]) -> bool:
    """Check the formats yt-dlp selected, rather than guessing from a file name."""
    tracks = list(entry.get('requested_formats') or [entry])
    for item in entry.get('requested_downloads') or []:
        if isinstance(item, dict):
            tracks.extend(item.get('requested_formats') or [item])
    return any(str(track.get('acodec') or 'none').lower() != 'none'
               for track in tracks if isinstance(track, dict))


def final_downloads(info: dict, folder: Path) -> list[Path]:
    """Pair each extractor entry with its final file, in playlist order."""
    if not isinstance(info, dict) or not info:
        return []
    collection = info.get('_type') in {'playlist', 'multi_video'}
    entries = list(info.get('entries') or []) if collection else [info]
    if len(entries) > MAX_GALLERY_ITEMS:
        raise RuntimeError('Collection exceeds configured item limit')
    files = []
    for entry in entries:
        if not isinstance(entry, dict):
            return []
        downloads = entry.get('requested_downloads') or [entry]
        names = [item.get('filepath') for item in downloads if isinstance(item, dict)]
        if len(names) == 1 and names[0]:
            path = Path(names[0])
            if not path.is_file() or re.search(r'\.(?:part|ytdl)$', path.name, re.I):
                return []
            files.append(path)
        elif not collection and len(names) == 1 and not names[0]:
            possible = files_in(folder)
            if len(possible) != 1:
                return []
            files.extend(possible)
        else:
            return []
    return files


def download_ytdlp(url: str, mode: str, tmpdir: str, *, manifest: dict | None = None) -> list[Path]:
    base = base_ydl_opts(tmpdir)
    base['noplaylist'] = not youtube_playlist_url(url)
    platform = platform_of(url)
    if platform == 'youtube' and mode not in AUDIO_PRESETS:
        base.update(format_sort=['res', 'fps', 'hdr:12'], format_sort_force=True)
    cookie_file = os.getenv(f"{(platform or '').upper()}_COOKIE_FILE", "")
    if cookie_file and Path(cookie_file).is_file():
        base["cookiefile"] = cookie_file
    attempts: list[tuple[str, dict[str, Any]]] = []
    if mode in {AUTO_MODE, "original", "playable"}:
        # yt-dlp ranks all exposed formats; no hidden resolution ceiling.
        # A music-only link may expose no video format at all.
        attempts = [("bv*+ba/b/ba", {})]
        if platform == 'youtube' and base['noplaylist']:
            attempts.append(("b", {}))
    elif mode in VIDEO_PRESETS:
        attempts = [(fmt, {}) for fmt in video_chain(int(VIDEO_PRESETS[mode]["height"]))]
    elif mode in AUDIO_PRESETS:
        # Keep source audio here. The delivery stage converts to MP3 once.
        attempts = [("bestaudio/ba/best", {})]
    else:
        raise ValueError("Unknown download mode")

    last: Exception | None = None
    audio_failure: RuntimeError | None = None
    seen: set[tuple[str, str]] = set()
    attempt_no = 0
    for candidate in ([url] if manifest is not None else extractor_candidates(url)):
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
                    if manifest is None:
                        info = ydl.extract_info(candidate, download=True)
                    else:
                        import copy
                        info = ydl.process_ie_result(copy.deepcopy(manifest), download=True)
                if not info:
                    raise RuntimeError("Extractor returned no media metadata")
                entries = list(info.get("entries") or []) if info.get("_type") in {"playlist", "multi_video"} else [info]
                complete = final_downloads(info, attempt_dir)
                if platform == 'youtube' or is_video_post_url(url):
                    complete = [p for p in complete if classify(p) in {"video", "audio"}]
                if len(complete) != len(entries):
                    raise RuntimeError("Incomplete collection: some media files are missing")
                if any(requested_audio_track(entry) and classify(path) == 'video' and not has_audio(path)
                       for entry, path in zip(entries, complete) if isinstance(entry, dict)):
                    audio_failure = RuntimeError('Incomplete source video audio track')
                    shutil.rmtree(attempt_dir, ignore_errors=True)
                    continue
                if complete:
                    return validate_media_files(complete)
            except Exception as exc:
                if fatal_download_error(exc):
                    raise
                if re.search(r"confirm you[’']re not a bot|login required|sign in to (?:confirm|view|watch)|private video", str(exc), re.I):
                    raise
                last = exc
                log.warning("yt-dlp %s on %s failed: %s", fmt, platform_label(candidate), type(exc).__name__)
            shutil.rmtree(attempt_dir, ignore_errors=True)
    if audio_failure:
        raise audio_failure
    if last:
        raise last
    return []


def download_youtube_public(url: str, mode: str, tmpdir: str) -> list[Path]:
    """Try a fresh anonymous visitor context after the normal extractor fails."""
    from youtube_public import request_manifest

    if youtube_playlist_url(url):
        return []
    canonical = extract_url(url) or url
    video_id = (parse_qs(urlparse(canonical).query).get('v') or [''])[0]
    if not YT_ID_RE.fullmatch(video_id):
        return []
    cooldown = DATA_DIR / 'youtube_public_cooldown'
    try:
        if 0 < float(cooldown.read_text()) - time.time() <= 300:
            return []
    except (OSError, ValueError):
        pass
    try:
        info = request_manifest(video_id, safe_url=safe_remote_url, proxy=PROXY or None)
    except httpx.HTTPError as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403:
            try:
                cooldown.parent.mkdir(parents=True, exist_ok=True)
                cooldown.write_text(str(time.time() + 300))
            except OSError:
                pass
        # An unavailable fallback must not replace the original platform error
        # or make a definitive bot check offer a futile immediate Retry button.
        raise ProviderError('Public YouTube player connection failed') from exc
    return download_ytdlp(canonical, mode, tmpdir, manifest=info)


def download_youtube_post(url: str, mode: str, tmpdir: str, post_id: str) -> list[Path]:
    from source_metadata import youtube_photo_entries, youtube_post_attachment

    page = fetch_public_page(f'https://www.youtube.com/post/{post_id}')
    attachment = youtube_post_attachment(page.text, post_id)
    for name in ('videoRenderer', 'compactVideoRenderer'):
        video = attachment.get(name)
        video_id = video.get('videoId') if isinstance(video, dict) else None
        if isinstance(video_id, str) and YT_ID_RE.fullmatch(video_id):
            return download_ytdlp(f'https://www.youtube.com/watch?v={video_id}', mode, tmpdir)
    entries = youtube_photo_entries(attachment)
    if not entries:
        raise RuntimeError('This YouTube Community post has no accessible media attachment')
    return download_source_entries(entries, Path(tmpdir) / 'youtube_post', url)


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
    platform = platform_of(url)
    if platform not in {"instagram", "pinterest"}:
        return []
    dest = Path(tmpdir) / "gallery"
    dest.mkdir(parents=True, exist_ok=True)
    # Dump an ordered manifest first. A partial subprocess download is not success.
    cmd = [sys.executable, "-m", "gallery_dl", "--config-ignore", "-c",
           str(Path(__file__).with_name("gallery-dl.conf")), "--resolve-json", url]
    if platform == "instagram":
        # gallery-dl uses synthetic ytdl: URLs for embedded DASH. Its private
        # metadata contains the actual CDN fallback and the item's full MPD.
        cmd[3:3] = ["-o", "output.private=true", "-o", "extractor.instagram.audio=merged"]
    if PROXY:
        cmd[3:3] = ["--proxy", PROXY]
    cookie_file = os.getenv(f"{platform.upper()}_COOKIE_FILE", "")
    if cookie_file and Path(cookie_file).is_file():
        cmd[3:3] = ["--cookies", cookie_file]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode:
        raise RuntimeError("Gallery metadata unavailable")
    manifest = json.loads(proc.stdout)
    if not isinstance(manifest, list):
        raise RuntimeError('Gallery returned invalid metadata')
    entries = [row for row in manifest if isinstance(row, list) and len(row) >= 3 and row[0] == 3
               and isinstance(row[2], dict) and not str(row[1]).startswith("text:")]
    if any(isinstance(row, list) and row and row[0] in {-1, 6} for row in manifest):
        if entries:
            raise RuntimeError("Incomplete gallery metadata or unresolved media item")
        raise RuntimeError("Gallery metadata unavailable; no items extracted")
    if platform == "instagram":
        from source_metadata import instagram_dash_audio_urls, instagram_dash_formats
        media = []
        requested = INSTAGRAM_POST_RE.fullmatch(urlparse(url).path)
        for _, media_url, meta in entries:
            if requested and meta.get('post_shortcode') not in {None, '', requested[2]}:
                raise RuntimeError('Instagram returned media for a different post')
            if meta.get('audio_url'):
                media.append({'kind': 'audio', 'formats': [{'url': meta['audio_url']}],
                              'scope': 'post' if meta.get('type') in {'post', 'reel'} else None})
                continue
            video = meta.get('video_url')
            if video or str(media_url).startswith('ytdl:'):
                candidates = [video, *(meta.get('_fallback') or [])]
                if not str(media_url).startswith('ytdl:'):
                    candidates.insert(0, media_url)
                item = {'video_dash_manifest': meta.get('_ytdl_manifest_data')}
                formats = instagram_dash_formats(item, 'video')
                formats.extend({'url': candidate, 'width': meta.get('width'), 'height': meta.get('height')}
                               for candidate in dict.fromkeys(filter(None, candidates)))
                media.append({'kind': 'video', 'formats': sorted(formats, key=media_quality, reverse=True),
                              'audio_urls': instagram_dash_audio_urls(item)})
            else:
                media.append({'kind': 'image', 'formats': [{'url': media_url}]})
        return download_source_entries(media, dest, url)
    if platform == "pinterest":
        from source_metadata import pinterest_entries
        pins = [row[-1] for row in manifest if isinstance(row, list) and row and row[0] == 2
                and isinstance(row[-1], dict) and any(k in row[-1] for k in ('images', 'videos', 'carousel_data', 'story_pin_data', 'audio'))]
        requested = PINTEREST_PIN_RE.fullmatch(urlparse(url).path)
        if requested and pins:
            pins = [pin for pin in pins if str(pin.get('id')) == requested[1]]
            if not pins:
                raise RuntimeError('Pinterest returned metadata for a different Pin')
            pins = [max(pins, key=lambda pin: (bool(pin.get('story_pin_data') or pin.get('carousel_data')), len(pin)))]
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
    from media_io import InvalidMedia

    if len(entries) > MAX_GALLERY_ITEMS:
        raise RuntimeError("Post exceeds configured item limit")
    outputs = []
    soundtrack = []
    post_music = []
    includes_visual = any(entry['kind'] != 'audio' for entry in entries)
    expected = tuple(entry['kind'] for entry in entries if not includes_visual or entry['kind'] != 'audio')
    for index, entry in enumerate(entries):
        # Keep independent soundtracks separate from the ordered visual slots.
        # Exact delivery can include them as documents, and MP3 can use them.
        folder = (dest.parent / "soundtrack" if entry["kind"] == "audio" else dest) / f"source_{index:05d}"
        folder.mkdir(parents=True, exist_ok=True)
        completed = None
        silent_fallback = None
        source_limit = None
        tried_item_audio = False
        used_urls = set()
        for variant, fmt in enumerate(entry["formats"]):
            url = fmt.get("url") or ""
            if not isinstance(url, str) or url in used_urls or not safe_remote_url(url):
                continue
            used_urls.add(url)
            variant_dir = folder / f'variant_{variant}'
            try:
                if urlparse(url).path.lower().endswith(('.m3u8', '.mpd')):
                    opts = base_ydl_opts(str(variant_dir))
                    selector = 'ba/b' if entry['kind'] == 'audio' else 'bv*+ba/b'
                    opts.update(format=selector, outtmpl=str(variant_dir / 'media.%(ext)s'),
                                http_headers={**request_headers(referer), 'Referer': referer})
                    with YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    found = final_downloads(info, variant_dir)
                    if len(found) != 1:
                        continue
                    path = found[0]
                else:
                    ext = Path(urlparse(url).path).suffix
                    if not re.fullmatch(r'\.[a-zA-Z0-9]{1,8}', ext):
                        ext = {'image': '.jpg', 'video': '.mp4', 'animation': '.gif', 'audio': '.m4a'}[entry['kind']]
                    variant_dir.mkdir()
                    path = variant_dir / f'media{ext}'
                    if not _download_direct_file(url, path, referer=referer):
                        continue
                validate_media_files([path])
                actual = classify(path)
                if actual != entry["kind"] and not (entry["kind"] == "image" and actual == "animation"):
                    continue
                if entry['kind'] == 'video' and not has_audio(path):
                    if not tried_item_audio and entry.get('audio_urls'):
                        tried_item_audio = True
                        path = attach_item_audio(path, entry['audio_urls'], variant_dir, referer)
                    if not has_audio(path):
                        silent_fallback = silent_fallback or path
                        continue
                if entry['kind'] == 'video' and silent_fallback:
                    from media_io import restore_variant_audio
                    path = restore_variant_audio(silent_fallback, path, variant_dir / 'restored')
                completed = path
                break
            except TransferLimit as exc:
                source_limit = exc
            except InvalidMedia:
                # A corrupt rendition does not make the next rendition incomplete.
                continue
            except Exception as exc:
                if fatal_download_error(exc):
                    raise
                log.warning("Source variant failed: %s", type(exc).__name__)
            finally:
                if not any(p and p.is_relative_to(variant_dir) for p in (completed, silent_fallback)):
                    shutil.rmtree(variant_dir, ignore_errors=True)
        completed = completed or silent_fallback
        if completed is None:
            if entry['kind'] == 'audio' and includes_visual:
                shutil.rmtree(folder, ignore_errors=True)
                continue
            if source_limit:
                raise source_limit
            raise IncompletePost(f'Incomplete carousel or post: item {index + 1}/{len(entries)} unavailable', expected)
        if entry['kind'] == 'audio':
            for unused in files_in(folder):
                if unused != completed:
                    unused.unlink()
            soundtrack.append(completed)
            if entry.get('scope') == 'post':
                post_music.append(completed)
        else:
            if silent_fallback and completed != silent_fallback:
                shutil.rmtree(silent_fallback.parent, ignore_errors=True)
            outputs.append(completed)
    if len(post_music) == 1 and outputs:
        augmented = []
        for path in outputs:
            if classify(path) == 'video' and not has_audio(path):
                try:
                    path = attach_post_music(path, post_music[0], path.parent / 'post_music_video')
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    log.warning('Post soundtrack could not be merged: %s', type(exc).__name__)
            augmented.append(path)
        outputs = augmented
    # An audio-only link still delivers its audio as the primary media.
    return outputs or soundtrack


def download_pinterest_page(url: str, tmpdir: str) -> list[Path]:
    from source_metadata import find_pinterest_pin, pinterest_entries
    resolved = resolve_public_redirect(url)
    match = PINTEREST_PIN_RE.fullmatch(urlparse(resolved).path)
    if not match:
        return []
    page = fetch_public_page(resolved)
    if platform_of(str(page.url)) != 'pinterest' or PINTEREST_PIN_RE.fullmatch(urlparse(str(page.url)).path) is None:
        return []
    decoder = json.JSONDecoder()
    matches = []
    for raw in re.findall(r"<script\b[^>]*>(.*?)</script>", page.text, re.I | re.S):
        raw = raw.lstrip()
        if raw.startswith(('window.__PWS_DATA__', 'window.__PWS_INITIAL_PROPS__')):
            raw = raw.partition('=')[2].lstrip()
        try:
            document, _ = decoder.raw_decode(raw)
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

    def __init__(self, message: str, category: str = "ExtractionFailed"):
        super().__init__(message)
        self.category = category if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", category) else "ExtractionFailed"


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
    if isinstance(exc, BadRequest):
        return "Telegram rejected this media. The rejected album was not delivered; try again."
    if connection_failed_before_send(exc):
        return "Could not connect to Telegram. The failed upload was not sent; try again when the connection returns."
    if isinstance(exc, (TimedOut, NetworkError)):
        return "Telegram did not confirm delivery. Check the chat before retrying to avoid duplicates."
    text = str(exc)
    low = text.lower()
    label = PLATFORMS.get(platform, {}).get("label", "Media")
    if 'no accessible media attachment' in low:
        return f'{label}: this post has no accessible media attachment.'
    if 'original file exceeds the telegram upload limit' in low:
        return 'This original file exceeds Telegram’s upload limit. Send the link on its own to try playable parts.'
    if "429" in low or "too many requests" in low or "rate-limit" in low or "rate limit" in low:
        return f"{label}: temporarily rate-limited. Please wait before trying again."
    if "storage" in low or "no space left" in low:
        return "The download device is low on storage. Free some space and try again."
    if "deadline" in low or "timed out" in low or "timeout" in low:
        return f"{label}: the download took too long. Please try again later."
    if "incomplete" in low or "truncated" in low:
        return f"{label}: some media could not be retrieved. The incomplete post was not sent."
    if "confirm you’re not a bot" in low or "confirm you're not a bot" in low:
        return f"{label}: this server was stopped by the platform's automated-traffic check. The media was not downloaded."
    if re.search(r"\bage(?:\b|_)", low) and any(s in low for s in ("restricted", "limit", "confirm", "verify")):
        return "This age-restricted media is not supported."
    if "limit" in low:
        return "This media exceeds the current file size or item limit."
    if any(x in low for x in ("login", "sign in", "cookies", "private", "empty media response")):
        return f"{label}: this post currently requires access the bot does not have."
    if "404" in low or "not found" in low:
        if platform == "snapchat":
            return "Snapchat: this clip's public web page returned 404 to the bot. The link may still open inside the Snapchat app."
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


def download_cobalt(url: str, mode: str, tmpdir: str) -> list[Path]:
    from cobalt_provider import request_manifest
    quality = str(VIDEO_PRESETS[mode]['height']) if mode in VIDEO_PRESETS and mode != 'best' else 'max'
    entries = request_manifest(url, audio_only=mode in AUDIO_PRESETS, quality=quality,
                               safe_url=safe_remote_url, max_items=MAX_GALLERY_ITEMS, proxy=PROXY or None)
    if not entries:
        return []
    return download_source_entries(entries, Path(tmpdir) / 'cobalt', url)


def newisty_enabled() -> bool:
    return (os.getenv('NEWISTY_YOUTUBE', '').strip() == '1'
            and not os.getenv('YOUTUBE_COOKIE_FILE') and not os.getenv('YOUTUBE_COOKIES_B64'))


def download_newisty(url: str, mode: str, tmpdir: str, errors: list) -> list[Path]:
    from newisty_provider import fetch

    if not errors or not re.search(r"confirm you[’']re not a bot", str(errors[0]), re.I):
        return []
    canonical = extract_url(url)
    if not canonical or not canonical.startswith('https://www.youtube.com/watch?v='):
        return []
    video_id = canonical.removeprefix('https://www.youtube.com/watch?v=')
    qualities = {'auto': 'best-mp4', 'original': 'best-mp4', 'playable': 'best-mp4', 'best': 'best-mp4',
                 '1080': '1080p', '720': '720p', '480': '480p', 'm4a': 'audio-m4a'}
    quality = 'audio-mp3' if mode.startswith('mp3_') else qualities.get(mode)
    if not quality:
        return []
    return fetch(video_id, quality, Path(tmpdir) / 'newisty', safe_url=safe_remote_url,
                 budget=check_download_budget, max_bytes=MAX_SOURCE_BYTES, proxy=PROXY or None)


def grab(url: str, mode: str, tmpdir: str, prefetched: dict[str, Any] | None = None) -> list[Path]:
    from media_io import same_video_stream

    platform = platform_of(url)
    if not platform or not safe_remote_url(url):
        raise RuntimeError("Unsupported or unsafe media link")
    Path(tmpdir).mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(tmpdir).free < 128 * 1024 * 1024:
        raise RuntimeError("Not enough free storage for a download")
    # Source-aware parsers only: page-wide URL scans can leak recommendations/posters.
    errors = []
    youtube_post = youtube_post_id(url) if platform == 'youtube' else ''
    if youtube_post:
        return download_youtube_post(url, mode, tmpdir, youtube_post)
    if platform == 'instagram':
        url = resolve_public_redirect(url)
    if platform == "youtube":
        engines = [lambda folder, extract_mode: download_ytdlp(url, extract_mode, folder)]
        if not youtube_playlist_url(url):
            if newisty_enabled():
                engines.append(lambda folder, extract_mode: download_newisty(url, extract_mode, folder, errors))
            engines.append(lambda folder, extract_mode: download_youtube_public(url, extract_mode, folder))
    elif platform == "instagram":
        dedicated = lambda folder, _: download_instagram_dedicated(url, folder, (prefetched or {}).get("_instagram_info"))[0]
        gallery = lambda folder, _: download_gallery(url, folder)
        cookie = os.getenv('INSTAGRAM_COOKIE_FILE', '')
        # The dedicated public parser cannot receive a login session. An
        # owner-authorized cookie file gives gallery-dl the first attempt.
        engines = ([gallery, dedicated] if cookie and Path(cookie).is_file()
                   else [dedicated, gallery])
        if INSTAGRAM_POST_RE.fullmatch(urlparse(url).path):
            engines.append(lambda folder, _: download_instagram_dedicated(url, folder, graphql=True)[0])
        engines.append(lambda folder, extract_mode: download_ytdlp(url, extract_mode, folder))
    elif platform == "pinterest":
        # gallery-dl retains page boundaries, audio blocks and carousel order.
        engines = [lambda folder, _: download_gallery(resolve_public_redirect(url), folder),
                   lambda folder, _: download_pinterest_page(url, folder)]
    else:
        engines = [lambda folder, _: download_snapchat_dedicated(url, folder, (prefetched or {}).get("_snap_info")),
                   lambda folder, extract_mode: download_ytdlp(url, extract_mode, folder)]
    # The owner supplies an authorized endpoint. No random public instance,
    # account cookie forwarding, or one-video substitute for a playlist.
    if os.getenv('COBALT_API_URL', '').strip() and not youtube_playlist_url(url):
        engines.append(lambda folder, extract_mode: download_cobalt(url, extract_mode, folder))
    candidate: list[Path] = []
    candidate_kinds: list[str] = []
    candidate_sound: set[int] = set()
    candidate_tracks: list[Path] = []
    required_layouts: set[tuple[str, ...]] = set()
    provider_diagnostics: list[str] = []
    audio_mode = mode in AUDIO_PRESETS
    for engine in engines:
        attempt = Path(tempfile.mkdtemp(prefix='extractor_', dir=tmpdir))
        try:
            has_collection = len(candidate) > 1 or any(len(layout) > 1 for layout in required_layouts)
            extract_mode = AUTO_MODE if audio_mode and has_collection else mode
            files = engine(str(attempt), extract_mode)
            if not files:
                shutil.rmtree(attempt, ignore_errors=True)
                continue
            check_download_budget({})
            if len(files) > MAX_GALLERY_ITEMS:
                raise RuntimeError("Configured item limit exceeded")
            if any(p.stat().st_size > MAX_SOURCE_BYTES for p in files):
                raise RuntimeError("Source file exceeds configured storage limit")
            files = validate_media_files(files)
            kinds = [classify(p) for p in files]
            if not audio_mode and is_video_post_url(url) and any(kind != 'video' for kind in kinds):
                raise RuntimeError("Video post returned a poster instead of video")
            layout = tuple('image' if kind == 'animation' else kind for kind in kinds)
            if required_layouts and required_layouts != {layout}:
                shutil.rmtree(attempt, ignore_errors=True)
                continue
            tracks = [p for p in files_in(attempt / 'soundtrack') if p not in files and has_audio(p)]
            video_indices = {i for i, kind in enumerate(kinds) if kind == 'video'}
            sound = {i for i in video_indices if has_audio(files[i])}
            if audio_mode and kinds == ['audio'] and len(candidate) <= 1:
                return files
            if candidate and kinds != candidate_kinds:
                shutil.rmtree(attempt, ignore_errors=True)
                continue
            if not candidate or sound > candidate_sound:
                candidate, candidate_kinds, candidate_sound = files, kinds, sound
                candidate_tracks = tracks or candidate_tracks
            elif sound - candidate_sound:
                candidate = list(candidate)
                for index in sorted(sound - candidate_sound):
                    # Matching kinds alone cannot identify a child; verify its
                    # encoded video before borrowing the audible rendition.
                    if same_video_stream(candidate[index], files[index]):
                        candidate[index] = files[index]
                        candidate_sound.add(index)
            candidate_tracks = candidate_tracks or tracks
            if video_indices == candidate_sound and (
                    not audio_mode or candidate_sound or candidate_tracks or 'audio' in candidate_kinds):
                break
        except Exception as exc:
            # Never hide a partial collection by falling back to a one-item result.
            # Resource and age restrictions remain terminal even when a prior
            # complete silent candidate exists.
            incomplete = any(word in str(exc).lower() for word in ('incomplete', 'truncated'))
            lost_audio = str(exc) == 'Incomplete source video audio track'
            if isinstance(exc, IncompletePost):
                required_layouts.add(tuple('image' if kind == 'animation' else kind for kind in exc.expected_kinds))
            elif fatal_download_error(exc) and not lost_audio and (not candidate or not incomplete):
                raise
            if platform == 'youtube' and isinstance(exc, ProviderError):
                provider_diagnostics.append(str(exc)[:170])
            # A provider configuration/outage error must not hide the actual
            # platform failure (e.g. YouTube's bot check) from the user.
            if not isinstance(exc, ProviderError) or isinstance(exc, ProviderRateLimit) or not errors:
                errors.append(exc)
            log.warning("%s extractor failed: %s", platform, type(exc).__name__)
        if not any(p.is_relative_to(attempt) for p in candidate + candidate_tracks):
            shutil.rmtree(attempt, ignore_errors=True)
    final_layout = tuple('image' if kind == 'animation' else kind for kind in candidate_kinds)
    if candidate and (not required_layouts or required_layouts == {final_layout}):
        if candidate_tracks:
            soundtrack = Path(tmpdir) / 'soundtrack'
            soundtrack.mkdir(exist_ok=True)
            for index, path in enumerate(candidate_tracks):
                path.rename(soundtrack / f'{index:05d}{path.suffix}')
        return candidate
    if errors:
        error = next((exc for exc in errors if isinstance(exc, (IncompletePost, ProviderRateLimit))), errors[-1])
        for diagnostic in provider_diagnostics:
            error.add_note(diagnostic)
        raise error
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
        + ("\nFor public YouTube videos, Newisty can process the video link if needed." if newisty_enabled() else "")
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📥 Send a YouTube, Instagram, Snapchat or Pinterest media link.\n"
        "📎 Source files are delivered at their highest available quality. Large files arrive as numbered parts.\n"
        "🎵 Tap MP3 after delivery to get audio.\n"
        "▶️ Use /playable followed by the link for a version that plays directly in Telegram."
        + ("\nIf YouTube blocks the server, the bot can send the video ID to Newisty’s public download API."
           if newisty_enabled() else "")
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("⚙️ Quality is automatic. No setup needed.")


async def cmd_original(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await media_command(update, context, 'original')


async def cmd_playable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await media_command(update, context, 'playable')


async def media_command(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    msg = update.effective_message
    url = extract_url(' '.join(context.args or []))
    if not url or not platform_of(url):
        await msg.reply_text(f'Use /{mode} followed by a YouTube, Instagram, Snapchat or Pinterest link.')
        return
    await dispatch_job(msg, context, update.effective_user.id, url, mode)


async def edit_status(status, text: str, reply_markup=None) -> bool:
    try:
        captionable = any(
            getattr(status, field, None)
            for field in ("photo", "video", "audio", "document", "animation")
        )
        if captionable:
            await status.edit_caption(caption=text, reply_markup=reply_markup, connect_timeout=8, read_timeout=8)
        else:
            await status.edit_text(text, reply_markup=reply_markup, connect_timeout=8, read_timeout=8)
        return True
    except TelegramError as exc:
        log.info("Status update failed: %s", type(exc).__name__)
        return False


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    uid = update.effective_user.id
    content = msg.text or msg.caption or ''
    parse_entities = getattr(msg, 'parse_entities' if msg.text else 'parse_caption_entities', None)
    links = ([entity.url for entity in parse_entities(types=[MessageEntity.TEXT_LINK])]
             if parse_entities else [])
    links.extend(match.group() for match in URL_RE.finditer(content))
    urls = list(filter(None, map(extract_url, links)))
    url = next((value for value in urls if platform_of(value)), urls[0] if urls else None)
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
        self._lock = asyncio.Lock()

    async def edit_text(self, text, **kwargs):
        async with self._lock:
            if self.message is None:
                self.message = await self.msg.reply_text(text, **kwargs)
            else:
                await self.message.edit_text(text, **kwargs)

    async def delete(self):
        async with self._lock:
            if self.message is not None:
                await self.message.delete()


async def dispatch_job(msg, context, uid: int, url: str, mode: str, action=None) -> None:
    url = extract_url(url) or url
    if platform_of(url) == "instagram":
        match = INSTAGRAM_POST_RE.fullmatch(urlparse(url).path)
        if match:
            kind = 'reel' if match[1].lower() in {'reel', 'reels'} else match[1].lower()
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
                      cached_sources=sources or None, known_meta={"title": "Media"},
                      refresh_audio=bool(action and action.get('refresh_audio')))
    manager.launch(row, execute)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = job_manager().recent(update.effective_user.id, update.effective_message.chat_id)
    labels = {"queued": "⏳ Queued", "downloading": "⬇️ Downloading / preparing",
              "sending": "📤 Sending", "done": "✅ Delivered", "failed": "❌ Failed — send the link to retry",
              "interrupted": "⚠️ Delivery interrupted — check received media before retrying",
              "expired": "⌛ Expired — send the link again"}
    text = "\n".join(f"{platform_label(r['url'])} · {labels.get(r['state'], r['state'])}" for r in rows)
    await update.effective_message.reply_text(text or "No recent downloads. Send a media link.")


async def answer_callback(query, *args, **kwargs) -> None:
    try:
        await query.answer(*args, **kwargs, connect_timeout=5, read_timeout=5, write_timeout=5, pool_timeout=5)
    except TelegramError as exc:
        log.info("Callback acknowledgement unavailable: %s", type(exc).__name__)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id
    if not callable(getattr(q.message, "reply_text", None)):
        await answer_callback(q, "This message is unavailable. Send the link again.")
        return

    if data.startswith("retry|"):
        action = resolve_action_data(uid, data.split("|", 1)[1])
        if not action.get("url"):
            await answer_callback(q, "This action expired. Send the link again.")
            return
        await dispatch_job(q.message, context, uid, action["url"], AUTO_MODE)
        await answer_callback(q)
        return
    if not data.startswith("mp3|"):
        await answer_callback(q)
        return

    parts = data.split("|")
    token = parts[2] if len(parts) == 4 else parts[1] if len(parts) == 2 else ""
    action = resolve_action_data(uid, token) if token else {}
    if not action.get("url"):
        action = recover_mp3_action(q, uid, data)
    url = str(action.get("url") or "")
    if not url:
        await answer_callback(q, "This MP3 action expired. Send the media link again.", show_alert=False)
        return

    await dispatch_job(q.message, context, uid, url, "mp3_320", action=action)
    await answer_callback(q)


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


def audio_sources(files: list[Path], soundtrack_dir: Path) -> list[Path]:
    """List each original audio-bearing item once in post order."""
    music = files_in(soundtrack_dir)
    # A post-level track muxed into a silent video is the same track, not a
    # second independent audio item for the MP3 action.
    originals = []
    for path in files:
        if music and path.parent.name == 'post_music_video':
            continue
        item_track = path.with_name('track.m4a')
        originals.append(item_track if path.name.endswith('_post_music.mkv') and item_track.is_file() else path)
    unique = dict.fromkeys(p.resolve() for p in originals + music)
    return [p for p in unique if has_audio(p)]


@dataclass(frozen=True)
class PreparedMedia:
    kind: str
    path: Path
    original: Path | None = None
    byte_range: tuple[int, int] | None = None
    filename: str | None = None


def media_format_rejected(exc: BadRequest) -> bool:
    description = str(exc).lower().removeprefix('bad request: ').strip()
    return description.startswith(('failed to process photo', 'failed to process video',
                                   'failed to process audio', 'failed to process media',
                                   'photo_invalid_dimensions', 'photo_ext_invalid',
                                   'image_process_failed', 'video_content_type_invalid',
                                   'audio_content_type_invalid', 'media_invalid'))


async def send_album(msg, files: list[Path], caption: str, tmp: Path, progress: dict | None = None,
                     *, originals: bool = False) -> int:
    """Preserve order; group compatible native types in batches of at most ten."""
    from media_io import FileSlice, lossless_parts, native_audio, native_video
    prepared: list[PreparedMedia] = []
    for index, path in enumerate(files):
        if progress is not None:
            progress["phase"] = f"Preparing media {index + 1}/{len(files)}"
        folder = tmp / "delivery" / str(index)
        if originals:
            size = path.stat().st_size
            if size <= MAX_BYTES:
                prepared.append(PreparedMedia('document', path))
            else:
                for part, start in enumerate(range(0, size, MAX_BYTES), 1):
                    prepared.append(PreparedMedia('document', path, byte_range=(start, min(MAX_BYTES, size - start)),
                                                  filename=f'{index + 1:03d}_{path.name}.part{part:03d}'))
            continue
        kind = await asyncio.to_thread(classify, path)
        original = path if path.stat().st_size <= MAX_BYTES else None
        if kind == "image":
            try:
                final = await asyncio.to_thread(fit_image, path, folder)
                prepared.append(PreparedMedia('image', final, original))
            except (RuntimeError, OSError, subprocess.SubprocessError):
                if original is None:
                    raise
                prepared.append(PreparedMedia('document', original))
        elif kind in {"video", "animation", "audio"}:
            final = None
            try:
                if kind == "audio":
                    final = await asyncio.to_thread(native_audio, path, folder)
                else:
                    final = await asyncio.to_thread(native_video, path, folder, upload_limit=MAX_BYTES)
                parts = await asyncio.to_thread(lossless_parts, final, folder / "parts", MAX_BYTES)
            except (RuntimeError, OSError, ImportError, subprocess.SubprocessError):
                if final == path and original is None:
                    # Splitting this exact source already failed above.
                    raise
                parts = await asyncio.to_thread(lossless_parts, path, folder / "source_parts", MAX_BYTES)
                prepared.extend(PreparedMedia('document', part) for part in parts)
                continue
            delivery_kind = "video" if kind == "animation" and (len(files) > 1 or len(parts) > 1) else kind
            prepared.extend(PreparedMedia(delivery_kind, part, original if len(parts) == 1 else None)
                            for part in parts)
        else:
            raise RuntimeError("Unsupported media type; no photo substitution performed")
    sent = 0
    if any(item.byte_range for item in prepared):
        caption = (caption + "\nJoin matching .part files in numbered order to recover the original.").strip()
    families = {'image': 'visual', 'video': 'visual', 'animation': 'visual', 'audio': 'audio', 'document': 'document'}
    while sent < len(prepared):
        family = families[prepared[sent].kind]
        batch = []
        for item in prepared[sent:sent + 10]:
            if families[item.kind] != family:
                break
            batch.append(item)
        label = caption if not sent else ""
        if progress is not None:
            progress["phase"] = f"Sending {sent + 1}-{sent + len(batch)}/{len(prepared)}"
        async def send_once():
            with ExitStack() as stack:
                media = []
                for offset, item in enumerate(batch):
                    kind, path = item.kind, item.path
                    file = stack.enter_context(path.open("rb"))
                    if item.byte_range:
                        file = FileSlice(file, *item.byte_range)
                    upload = InputFile(file, filename=item.filename or path.name,
                                       attach=len(batch) > 1, read_file_handle=False)
                    item_caption = label if offset == 0 else None
                    if len(batch) == 1:
                        if kind == "image":
                            return await msg.reply_photo(photo=upload, caption=item_caption)
                        if kind == "video":
                            return await msg.reply_video(video=upload, caption=item_caption, supports_streaming=True)
                        if kind == "animation":
                            return await msg.reply_animation(animation=upload, caption=item_caption)
                        if kind == 'document':
                            return await msg.reply_document(document=upload, caption=item_caption, disable_content_type_detection=True)
                        return await msg.reply_audio(audio=upload, caption=item_caption)
                    cls = {'image': InputMediaPhoto, 'video': InputMediaVideo, 'audio': InputMediaAudio,
                           'document': InputMediaDocument}[kind]
                    kwargs = {"supports_streaming": True} if kind == "video" else {}
                    media.append(cls(media=upload, caption=item_caption, **kwargs))
                return await msg.reply_media_group(media=media)
        if progress is not None:
            progress["attempted"] = True
        mark_state("sending")
        try:
            result = await _retry_telegram(send_once)
        except BadRequest as exc:
            if family == 'document' or not media_format_rejected(exc):
                raise
            # Telegram rejected this request for its media format before
            # accepting the batch. Use the source bytes wherever they fit.
            log.info("Telegram rejected native media format; sending %s documents", len(batch))
            batch = [PreparedMedia('document', item.original or item.path) for item in batch]
            result = await _retry_telegram(send_once)
        if progress is not None:
            progress["last_message"] = result[-1] if isinstance(result, (list, tuple)) and result else result
            progress["last_message_is_album"] = len(batch) > 1
        sent += len(batch)
        if progress is not None:
            progress["sent"] = sent
    return sent


def post_mp3_button(uid: int, url: str, sources: list[Path], title: str, *, refresh_audio: bool = False) -> InlineKeyboardMarkup:
    try:
        children = [create_action(uid, url, path, title, index) for index, path in enumerate(sources)]
        token = create_action(uid, url, title=title, children=children, refresh_audio=refresh_audio)
    except OSError as exc:
        log.warning('MP3 cache unavailable: %s', type(exc).__name__)
        token = secrets.token_hex(10)
    # Render's free filesystem disappears on a cold restart. Keep the URL in
    # Telegram's own button and authenticate its owner so MP3 can re-extract
    # the post even after the temporary audio cache has vanished.
    signature = hmac.new(BOT_TOKEN.encode(), f"{uid}:{token}:{url}".encode(), hashlib.sha256).hexdigest()[:16]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("MP3", callback_data=f"mp3|{uid}|{token}|{signature}"),
        InlineKeyboardButton("Original", url=url),
    ]])


def recover_mp3_action(query, uid: int, data: str) -> dict[str, Any]:
    parts = data.split("|")
    if len(parts) != 4 or parts[0] != "mp3" or parts[1] != str(uid):
        return {}
    token, signature = parts[2:]
    if not re.fullmatch(r"[a-f0-9]{20}", token) or not re.fullmatch(r"[a-f0-9]{16}", signature):
        return {}
    markup = getattr(query.message, "reply_markup", None)
    buttons = [b for row in getattr(markup, "inline_keyboard", ()) for b in row]
    if not any(b.callback_data == data for b in buttons):
        return {}
    url = next((b.url for b in buttons if b.text == "Original" and b.url), "")
    if not platform_of(url) or not safe_remote_url(url):
        return {}
    expected = hmac.new(BOT_TOKEN.encode(), f"{uid}:{token}:{url}".encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(signature, expected):
        return {}
    return {"uid": uid, "url": url, "post": True, "children": []}


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


async def report_job_event(job: str, platform: str, stage: str, *, items: int = 0,
                           error: str = 'none') -> None:
    """Expose safe progress on Render without copying a chat or media URL."""
    if os.getenv('TERMUX_PRIMARY') == '0':
        log.info('Job %s platform=%s stage=%s items=%s error=%s', job, platform, stage,
                 min(items, 100), error)
        return
    if not TELEGRAM_RELAY_BASE or not BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=4) as client:
            await client.post(TELEGRAM_RELAY_BASE + '/relay/event',
                              headers={'X-Veltrix-Token': BOT_TOKEN},
                              json={'job': job, 'platform': platform or 'unknown',
                                    'stage': stage, 'items': min(items, 100), 'error': error})
    except (httpx.HTTPError, OSError):
        pass


async def run_job(
    msg, context, uid: int, url: str, mode: str, status=None,
    selected_audio_index: int | None = None,
    known_meta: dict[str, Any] | None = None,
    cached_sources: list[Path] | None = None,
    refresh_audio: bool = False,
) -> None:
    lock = job_lock(uid)
    progress = {"sent": 0, "phase": "Queued"}
    event_id = secrets.token_hex(6)
    source = platform_of(url) or 'unknown'
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
            await report_job_event(event_id, source, 'downloading')
            tmp = Path(tempfile.mkdtemp(prefix="vx_"))
            progress["phase"] = f"Downloading from {platform_label(url)}"
            meta = dict(known_meta or {})
            files = []
            if cached_sources and all(p.is_file() for p in cached_sources):
                for i, path in enumerate(cached_sources):
                    local = tmp / f"cached_{i}{path.suffix}"
                    try:
                        await asyncio.to_thread(shutil.copy2, path, local)
                    except OSError:
                        files = []
                        break
                    files.append(local)
            if not files or refresh_audio:
                try:
                    fresh = await download_in_worker(url, mode, tmp, meta)
                    if not files or len(await asyncio.to_thread(audio_sources, fresh, tmp / 'soundtrack')) > len(files):
                        files = fresh
                    else:
                        shutil.rmtree(tmp / 'soundtrack', ignore_errors=True)
                except (RuntimeError, OSError) as exc:
                    if not files:
                        raise
                    log.info('Keeping cached MP3 sources after refresh failed: %s', type(exc).__name__)
                    shutil.rmtree(tmp / 'soundtrack', ignore_errors=True)
            kinds = await asyncio.to_thread(lambda: [classify(p) for p in files])
            with_sound = await asyncio.to_thread(lambda: sum(
                kind == 'video' and has_audio(path) for kind, path in zip(kinds, files)))
            log.info('Job %s source media: videos=%s with_audio=%s images=%s animations=%s audio=%s',
                     event_id, kinds.count('video'), with_sound, kinds.count('image'),
                     kinds.count('animation'), kinds.count('audio'))
            await report_job_event(event_id, source, 'prepared', items=len(files))
            await report_job_event(event_id, source, 'uploading', items=len(files))
            if mode in AUDIO_PRESETS:
                progress["phase"] = "Checking available audio tracks"
                if selected_audio_index is not None:
                    if selected_audio_index < 0 or selected_audio_index >= len(files):
                        raise RuntimeError("Requested media item is no longer available")
                    files = [files[selected_audio_index]]
                sources = await asyncio.to_thread(audio_sources, files, tmp / "soundtrack")
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
                silent_videos = kinds.count('video') - with_sound
                if silent_videos:
                    caption += f"\n🔇 Audio unavailable in {silent_videos} source video(s)."
                if mode in {AUTO_MODE, 'original'}:
                    soundtracks = await asyncio.to_thread(files_in, tmp / 'soundtrack')
                    deliveries = list(dict.fromkeys([*files, *soundtracks]))
                    sent = await send_album(msg, deliveries, caption, tmp, progress, originals=True)
                else:
                    deliveries = files
                    sent = await send_album(msg, files, caption, tmp, progress)
                if not sent:
                    raise RuntimeError("Nothing downloadable was returned")
                delivered = True
                # Persist delivery before optional cache/status operations.
                mark_state("done")
                sources = await asyncio.to_thread(audio_sources, files, tmp / "soundtrack")
                # Offer one MP3 action for every delivered post. A silent
                # rendition can be rechecked through the other extractors
                # when clicked; if none exposes sound, report that clearly.
                markup = None
                try:
                    markup = await asyncio.to_thread(post_mp3_button, uid, url, sources,
                                                     str(meta.get('title') or ''), refresh_audio=bool(silent_videos))
                except OSError:
                    log.warning("Media delivered; MP3 action could not be saved")
                ticker.cancel()
                await asyncio.gather(ticker, return_exceptions=True)
                attached = False
                last_message = progress.get("last_message")
                if (markup is not None and last_message is not None
                        and not progress.get("last_message_is_album")
                        and hasattr(last_message, 'edit_reply_markup')):
                    try:
                        await last_message.edit_reply_markup(reply_markup=markup,
                                                             connect_timeout=8, read_timeout=8)
                        attached = True
                    except TelegramError as exc:
                        log.info("Media keyboard attachment failed: %s", type(exc).__name__)
                if markup is not None and not attached:
                    # Place the action after the album so it stays visible.
                    # Telegram rejects inline keyboards on grouped media.
                    try:
                        await _retry_telegram(lambda: msg.reply_text("🎵 MP3", reply_markup=markup))
                        attached = True
                    except TelegramError as exc:
                        log.info("MP3 message unavailable: %s", type(exc).__name__)
                await edit_status(status, f"✅ {len(deliveries)} media delivered",
                                  reply_markup=None if attached else markup)
            metric_add("jobs_succeeded", 1)
            await report_job_event(event_id, source, 'delivered', items=sent)
    except Exception as exc:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        count = progress["sent"]
        if delivered:
            metric_add("jobs_succeeded", 1)
            log.warning("Job %s media delivered; follow-up failed: %s", event_id, type(exc).__name__)
            await report_job_event(event_id, source, 'delivered', items=progress['sent'], error=type(exc).__name__)
            await edit_status(status, "✅ Media delivered. Send the link again if the MP3 action is unavailable.")
            return
        metric_add("jobs_failed", 1)
        uncertain = bool(progress.get("attempted")) and not isinstance(exc, BadRequest) and not connection_failed_before_send(exc)
        mark_state("interrupted" if count or uncertain else "failed")
        log.error("job %s failed during %s: %s", event_id, progress['phase'], type(exc).__name__)
        await report_job_event(event_id, source, 'interrupted' if uncertain or count else 'failed',
                               items=count, error=exc.category if isinstance(exc, WorkerFailure) else type(exc).__name__)
        markup = None
        definitive = isinstance(exc, WorkerFailure) and exc.category in {
            'SourceUnavailable', 'PlatformBotCheck', 'AccessRequired',
        }
        if mode == AUTO_MODE and not count and not uncertain and not definitive:
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
                    "environment_proxy_available": bool(not (TELEGRAM_API_BASE or TELEGRAM_RELAY_BASE) and environment_proxy()),
                    "relay_enabled": bool(TELEGRAM_RELAY_BASE),
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
            if self.path == "/readyz" and not connectivity['polling_recently']:
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
        raise TransferLimit("Insufficient storage; download stopped")
    used = sum(p.stat().st_size for p in Path(root).rglob("*") if p.is_file())
    if used > MAX_JOB_BYTES:
        raise TransferLimit("Job storage limit exceeded; download stopped")


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
    match = SNAP_PROFILE_RE.fullmatch(urlparse(url).path)
    if not match:
        return []
    requested = (match[2] or '').strip('/').rsplit('/', 1)[-1]
    if requested in {'story', 'stories', 'highlights'}:
        requested = ''
    story = _dig_dict(props, 'story')
    highlights = [row for row in (props.get('curatedHighlights') or []) if isinstance(row, dict)]
    snaps = story.get('snapList') or []
    if not requested:
        if not snaps and highlights:
            snaps = highlights[0].get('snapList') or []
    else:
        snaps = [snap for group in [story, *highlights] for snap in (group.get('snapList') or [])
                 if isinstance(snap, dict) and _dig_dict(snap, 'snapId').get('value') == requested]
        if snaps:
            snaps = snaps[:1]
        else:
            selected = next((group for group in highlights
                             if _dig_dict(group, 'highlightId').get('value') == requested), {})
            snaps = selected.get('snapList') or []
    output = []
    for snap in snaps:
        if not isinstance(snap, dict):
            raise RuntimeError('Incomplete story metadata')
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
        raise WorkerFailure(result["error"], str(result.get("category") or "ExtractionFailed"))
    files = [Path(p).resolve() for p in result.get("files", [])]
    if not files or any(not p.is_relative_to(tmp) or not p.is_file() for p in files):
        raise RuntimeError("Invalid download worker result")
    return files


def telegram_request(pool_size: int = 16, token: str | None = None):
    from telegram_network import ConnectFallback, ObservedRequest, RequestIDTransport, environment_proxy
    from urllib.parse import urlsplit
    token = token or BOT_TOKEN
    if TELEGRAM_RELAY_BASE:
        address = urlsplit(TELEGRAM_RELAY_BASE)
        if (address.scheme != 'https' or not address.hostname or address.username
                or address.password or address.query or address.fragment or address.path not in {'', '/'}):
            raise ValueError('TELEGRAM_RELAY_BASE must be a clean HTTPS host')
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
        backup_proxy = environment_proxy() if not (TELEGRAM_API_BASE or TELEGRAM_RELAY_BASE) else None
        if backup_proxy:
            transport = ConnectFallback(
                transport, httpx.AsyncHTTPTransport(limits=limits, retries=0, proxy=backup_proxy),
            )
    if TELEGRAM_RELAY_BASE:
        transport = RequestIDTransport(transport)
    headers = {'X-Veltrix-Token': token} if TELEGRAM_RELAY_BASE else None
    return ObservedRequest(relay_base=TELEGRAM_RELAY_BASE, relay_token=token,
                           connection_pool_size=pool_size, read_timeout=120, write_timeout=120,
                           connect_timeout=10, pool_timeout=10, media_write_timeout=1800,
                           httpx_kwargs={"transport": transport, "trust_env": False, **({'headers': headers} if headers else {})})


def application_builder(token: str):
    builder = (Application.builder().token(token).request(telegram_request(token=token))
               .get_updates_request(telegram_request(1, token=token)).concurrent_updates(8)
               .post_init(resume_jobs).post_stop(stop_jobs).post_shutdown(stop_jobs))
    if TELEGRAM_RELAY_BASE:
        builder = builder.base_url(TELEGRAM_RELAY_BASE + '/bot').base_file_url(TELEGRAM_RELAY_BASE + '/file/bot')
    elif TELEGRAM_API_BASE:
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
    app.add_handler(CommandHandler('original', cmd_original))
    app.add_handler(CommandHandler('playable', cmd_playable))
    app.add_handler(CallbackQueryHandler(on_callback))
    caption_links = filters.CaptionRegex(URL_RE) | filters.CaptionEntity(MessageEntity.TEXT_LINK)
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | caption_links, on_text))
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
        # Short polls finish within the relay's HTTP path and expose dead links
        # promptly; they still keep the free relay awake while the phone runs.
        app.run_polling(drop_pending_updates=False, bootstrap_retries=3,
                        timeout=5 if TELEGRAM_RELAY_BASE else 25)
    except InvalidToken:
        log.error("BOT_TOKEN is invalid. Correct .env before restarting.")
        raise SystemExit(78)
    except NetworkError as exc:
        log.warning("Telegram startup offline: %s; supervisor will retry", type(exc).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
