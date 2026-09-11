#!/usr/bin/env python3
"""Veltrix Downloader backend.

Public-media downloader for YouTube, Instagram, Snapchat and Pinterest.
Optimized for Telegram hosted Bot API limits and small Render instances.
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
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL

load_dotenv()
VERSION = "6.0.0"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PROXY = (os.getenv("PROXY") or os.getenv("HTTPS_PROXY") or "").strip()
MAX_BYTES = int(os.getenv("TELEGRAM_MAX_BYTES", str(48 * 1024 * 1024)))
MAX_SOURCE_BYTES = int(os.getenv("MAX_SOURCE_BYTES", str(1024 * 1024 * 1024)))
MAX_PHOTO_BYTES = int(os.getenv("MAX_PHOTO_BYTES", str(9 * 1024 * 1024)))
MAX_GALLERY_ITEMS = max(1, min(int(os.getenv("MAX_GALLERY_ITEMS", "20")), 40))
MAX_CONCURRENT_JOBS = max(1, min(int(os.getenv("MAX_CONCURRENT_JOBS", "1")), 3))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
USERS_FILE = DATA_DIR / "users.json"
ACTIONS_FILE = DATA_DIR / "actions.json"
CACHE_DIR = DATA_DIR / "cache"
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", str(2 * 3600)))
CACHE_MAX_BYTES = int(os.getenv("CACHE_MAX_BYTES", str(256 * 1024 * 1024)))

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("veltrix")
_user_lock = Lock()
_action_lock = Lock()
_job_locks: dict[int, asyncio.Lock] = {}
_global_sem: asyncio.Semaphore | None = None
_metrics_lock = Lock()
_metrics = {
    "jobs_started": 0,
    "jobs_succeeded": 0,
    "jobs_failed": 0,
    "active_jobs": 0,
    "queued_jobs": 0,
}

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
    try:
        for path in CACHE_DIR.iterdir():
            if not path.is_file():
                continue
            try:
                if now - path.stat().st_mtime > CACHE_TTL_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        pass


def create_action(uid: int, url: str, source_path: Path | None = None, title: str = "") -> str:
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
                shutil.copy2(source_path, dest)
                cache_path = str(dest)
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


def mp3_button(uid: int, url: str, source_path: Path | None = None, title: str = "") -> InlineKeyboardMarkup:
    token = create_action(uid, url, source_path=source_path, title=title)
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
    """Resolve public short/share URLs and recover canonical social post routes."""
    platform = platform_of(url)
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if host != "pin.it" and platform != "snapchat":
        return url

    try:
        with httpx.Client(
            headers=request_headers(url),
            follow_redirects=True,
            timeout=15,
            proxy=PROXY or None,
        ) as client:
            response = client.get(url)

        final_url = str(response.url)
        if platform_of(final_url) == platform and final_url != url:
            return final_url

        # Snapchat can render/return a compatibility page whose HTML contains
        # the modern /@creator/spotlight/<id> route even when the old route
        # itself is not the canonical address.
        if platform == "snapchat":
            target = re.search(
                r'https?://(?:www\.)?snapchat\.com/@[^/"\']+/spotlight/[A-Za-z0-9_-]+',
                response.text or "",
                flags=re.I,
            )
            if target:
                return html.unescape(target.group(0))
            relative = re.search(
                r'/(?:@[^/"\']+/)?spotlight/[A-Za-z0-9_-]+',
                response.text or "",
                flags=re.I,
            )
            if relative:
                return "https://www.snapchat.com" + html.unescape(relative.group(0))

        if platform == "pinterest" and platform_of(final_url) == "pinterest":
            return final_url
    except Exception as exc:
        log.info("share redirect resolution failed: %s", str(exc)[:140])

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
        "http_headers": request_headers(),
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
    if platform_of(url) == "snapchat":
        try:
            snap = extract_snapchat_public(url)
            if snap.get("url"):
                return {
                    "title": str(snap.get("title") or "Spotlight")[:180],
                    "duration": 0,
                    "format_count": 1,
                    "thumbnail": str(snap.get("thumbnail") or ""),
                }
        except Exception as exc:
            log.info("Snapchat preview unavailable: %s", str(exc)[:160])

    try:
        return probe_media(url)
    except Exception as first:
        log.info("preview extractor unavailable: %s", str(first)[:160])
    headers = request_headers(url)
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


def _dig_dict(node: Any, *keys: str) -> dict[str, Any]:
    cur = node
    for key in keys:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(key)
    return cur if isinstance(cur, dict) else {}


def _snap_requested_id(url: str, doc: dict[str, Any]) -> str:
    query = doc.get("query") if isinstance(doc.get("query"), dict) else {}
    snap_id = str(query.get("snapID") or "")
    if snap_id:
        return snap_id
    match = re.search(r"/(?:@[^/]+/)?spotlight/([A-Za-z0-9_-]+)", urlparse(url).path, re.I)
    return match.group(1) if match else ""


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

    top = props.get("videoMetadata")
    if isinstance(top, dict) and str(top.get("contentUrl") or ""):
        return {"videoMetadata": top}

    for item in stories:
        if isinstance(item, dict) and isinstance(item.get("metadata"), dict):
            return item["metadata"]
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


def extract_snapchat_public(url: str) -> dict[str, str]:
    """Extract exactly the requested public Spotlight from Snapchat __NEXT_DATA__."""
    candidates = extractor_candidates(url)
    seen: set[str] = set()
    last_error: Exception | None = None

    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            with httpx.Client(
                headers=request_headers(candidate),
                follow_redirects=True,
                timeout=25,
                proxy=PROXY or None,
            ) as client:
                response = client.get(candidate)
            if response.status_code != 200:
                continue
            page = response.text
            match = re.search(
                r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                page,
                flags=re.I | re.S,
            )
            if not match:
                match = re.search(r'__NEXT_DATA__["\'][^>]*>(.*?)</script>', page, flags=re.I | re.S)
            if not match:
                continue
            doc = json.loads(html.unescape(match.group(1)).strip())
            info = _snap_info_from_doc(doc, str(response.url))
            if info.get("url"):
                return info
        except Exception as exc:
            last_error = exc
            log.info("Snapchat dedicated extractor failed: %s", str(exc)[:180])

    if last_error:
        raise last_error
    return {}


def download_snapchat_dedicated(url: str, tmpdir: str) -> list[Path]:
    try:
        info = extract_snapchat_public(url)
    except Exception:
        return []
    media_url = str(info.get("url") or "")
    if not media_url or not safe_remote_url(media_url):
        return []

    dest = Path(tmpdir) / "snapchat"
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "spotlight.mp4"
    headers = {
        "User-Agent": DESKTOP_UA,
        "Referer": "https://www.snapchat.com/",
        "Accept": "*/*",
    }
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=45, proxy=PROXY or None) as client:
            with client.stream("GET", media_url) as response:
                response.raise_for_status()
                total = 0
                with out.open("wb") as fh:
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_SOURCE_BYTES:
                            raise RuntimeError("Snapchat media too large")
                        fh.write(chunk)
        return [out] if out.exists() and out.stat().st_size else []
    except Exception as exc:
        log.info("Snapchat media download failed: %s", str(exc)[:180])
        return []


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
                opts["outtmpl"] = str(attempt_dir / "%(extractor)s_%(id)s_%(title).80B.%(ext)s")
                opts.update(extra)
                with YoutubeDL(opts) as ydl:
                    ydl.download([candidate])
                files = files_in(attempt_dir)
                complete = [p for p in files if p.suffix.lower() not in {".part", ".ytdl"}]
                if complete:
                    return complete
            except Exception as exc:
                last = exc
                log.warning("yt-dlp %s on %s failed: %s", fmt, platform_label(candidate), str(exc)[:220])
    if last:
        raise last
    return []


def _pinterest_quality_score(fmt: dict[str, Any]) -> tuple[int, int, int]:
    """Prefer progressive 720p, then 1080p, then the closest useful fallback."""
    url = str(fmt.get("url") or "")
    h = int(fmt.get("height") or 0)
    progressive = 1 if ".mp4" in url.lower() else 0
    if h == 720:
        tier = 100
    elif h == 1080:
        tier = 95
    elif 0 < h < 720:
        tier = 80 + h // 100
    elif h > 1080:
        tier = 70
    else:
        tier = 60
    # Quality preference is authoritative; progressive MP4 only breaks ties.
    return (tier, progressive, h)


def _download_direct_file(
    media_url: str,
    out: Path,
    *,
    referer: str,
    timeout: int = 45,
) -> bool:
    if not safe_remote_url(media_url):
        return False
    headers = {"User-Agent": DESKTOP_UA, "Referer": referer, "Accept": "*/*"}
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            if out.exists():
                out.unlink(missing_ok=True)
            with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout, proxy=PROXY or None) as client:
                with client.stream("GET", media_url) as response:
                    response.raise_for_status()
                    length = int(response.headers.get("content-length") or 0)
                    if length and length > MAX_SOURCE_BYTES:
                        return False
                    total = 0
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with out.open("wb") as fh:
                        for chunk in response.iter_bytes(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_SOURCE_BYTES:
                                raise RuntimeError("media too large")
                            fh.write(chunk)
            if out.exists() and out.stat().st_size > 0:
                return True
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.7 * (2 ** (attempt - 1)))
    if last_error:
        log.info("direct media download failed after retries: %s", str(last_error)[:160])
    return False


def download_pinterest_dedicated(url: str, tmpdir: str) -> tuple[list[Path], str]:
    """Return the exact Pinterest media and the platform-reported media type."""
    try:
        from pinterest_downloader import Pinterest
    except Exception as exc:
        log.info("Pinterest dedicated extractor unavailable: %s", exc)
        return [], ""

    resolved = resolve_public_redirect(url)
    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    try:
        client = Pinterest(timeout=25, proxies=proxies)
        result = client.get_pin(resolved)
    except Exception as exc:
        log.info("Pinterest dedicated metadata failed: %s", str(exc)[:180])
        return [], ""

    if not isinstance(result, dict) or not result.get("ok"):
        log.info(
            "Pinterest dedicated extractor returned no pin: %s",
            str(result.get("error") if isinstance(result, dict) else result)[:160],
        )
        return [], ""

    pin = result.get("pin") or {}
    media_type = str(pin.get("media_type") or "").lower()
    dest = Path(tmpdir) / "pinterest"
    dest.mkdir(parents=True, exist_ok=True)

    if media_type == "video":
        video = pin.get("video") or {}
        formats = [f for f in (video.get("formats") or []) if isinstance(f, dict) and f.get("url")]
        formats.sort(key=_pinterest_quality_score, reverse=True)
        headers = {**request_headers(resolved), "Referer": "https://www.pinterest.com/"}

        for index, fmt in enumerate(formats, 1):
            media_url = str(fmt.get("url") or "")
            if not safe_remote_url(media_url):
                continue
            if ".m3u8" in media_url.lower():
                try:
                    opts = base_ydl_opts(tmpdir)
                    opts["format"] = "best"
                    opts["http_headers"] = headers
                    with YoutubeDL(opts) as ydl:
                        ydl.download([media_url])
                    found = [p for p in files_in(Path(tmpdir) / "ytdl") if classify(p) == "video"]
                    if found:
                        return found[:1], "video"
                except Exception as exc:
                    log.info("Pinterest HLS fallback failed: %s", str(exc)[:140])
                continue

            ext = Path(urlparse(media_url).path).suffix or ".mp4"
            out = dest / f"pin_video_{index}{ext}"
            if _download_direct_file(media_url, out, referer="https://www.pinterest.com/"):
                return [out], "video"
        return [], "video"

    images = pin.get("images") if isinstance(pin.get("images"), dict) else {}
    image_url = ""
    for size_key in ("orig", "736x", "474x", "236x", "170x"):
        item = images.get(size_key)
        if isinstance(item, dict) and item.get("url"):
            image_url = str(item["url"])
            break
    if not image_url and images:
        for item in images.values():
            if isinstance(item, dict) and item.get("url"):
                image_url = str(item["url"])
                break

    if image_url:
        ext = Path(urlparse(image_url).path).suffix
        if not ext:
            ext = ".gif" if media_type == "gif" else ".jpg"
        out = dest / f"pin_media{ext}"
        if _download_direct_file(image_url, out, referer="https://www.pinterest.com/"):
            return [out], media_type or "image"

    return [], media_type




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
    items = files_in(dest)
    # gallery-dl writes carousel items sequentially; files_in preserves that
    # download order. Do not re-sort by hash-like filenames.
    if is_video_post_url(url):
        return [p for p in items if classify(p) == "video"]
    return items


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


def download_public_page_media(url: str, tmpdir: str) -> list[Path]:
    """Download media URLs exposed by a public social page without login bypass."""
    headers = request_headers(url)
    dest = Path(tmpdir) / "page_media"
    dest.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []

    with httpx.Client(headers=headers, follow_redirects=True, timeout=25, proxy=PROXY or None) as client:
        response = client.get(url)
        response.raise_for_status()
        if "text/html" not in response.headers.get("content-type", ""):
            return []
        force_video = is_video_post_url(str(response.url)) or is_video_post_url(url)
        candidates = page_media_candidates(response.text, include_images=not force_video)

        for index, raw in enumerate(candidates[:40], 1):
            media_url = urljoin(str(response.url), raw)
            if not safe_remote_url(media_url):
                continue
            path_lower = urlparse(media_url).path.lower()

            # HLS is delegated to yt-dlp/ffmpeg.
            if path_lower.endswith(".m3u8"):
                try:
                    opts = base_ydl_opts(tmpdir)
                    opts["format"] = "best"
                    opts["http_headers"] = {**opts.get("http_headers", {}), "Referer": str(response.url)}
                    with YoutubeDL(opts) as ydl:
                        ydl.download([media_url])
                    hls_files = files_in(Path(tmpdir) / "ytdl")
                    if hls_files:
                        results.extend(hls_files)
                        break
                except Exception as exc:
                    log.info("public HLS fallback failed: %s", str(exc)[:140])
                continue

            try:
                with client.stream("GET", media_url, headers={"Referer": str(response.url)}) as media:
                    media.raise_for_status()
                    content_type = media.headers.get("content-type", "").split(";", 1)[0].lower()
                    media_host = (urlparse(media_url).hostname or "").lower()
                    known_snap_video = media_host == "sc-cdn.net" or media_host.endswith(".sc-cdn.net")
                    if not (
                        content_type.startswith("video/")
                        or content_type.startswith("image/")
                        or content_type.startswith("audio/")
                        or known_snap_video
                    ):
                        continue
                    length = int(media.headers.get("content-length") or 0)
                    if length and length > MAX_SOURCE_BYTES:
                        continue
                    ext = (
                        ".mp4"
                        if known_snap_video
                        else (mimetypes.guess_extension(content_type) or Path(urlparse(media_url).path).suffix or ".bin")
                    )
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
                    if out.exists() and out.stat().st_size:
                        results.append(out)
            except Exception as exc:
                log.info("public direct media failed: %s", str(exc)[:140])

    if is_video_post_url(url):
        return [p for p in results if classify(p) == "video"][:MAX_GALLERY_ITEMS]
    videos = [p for p in results if classify(p) == "video"]
    if videos:
        return videos[:MAX_GALLERY_ITEMS]
    # Keep original media order for image/carousel posts.
    return results[:MAX_GALLERY_ITEMS]


def download_open_graph(url: str, tmpdir: str) -> list[Path]:
    """Public-page fallback, useful for share pages with direct OG media."""
    if not platform_of(url):
        return []
    headers = request_headers(url)
    proxy = PROXY or None
    with httpx.Client(headers=headers, follow_redirects=True, timeout=25, proxy=proxy) as client:
        response = client.get(url)
        response.raise_for_status()
        if "text/html" not in response.headers.get("content-type", ""):
            return []
        video_names = {"og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"}
        image_names = {"og:image", "og:image:url", "og:image:secure_url", "twitter:image"}
        candidates = og_values(response.text, video_names)
        if not is_video_post_url(url):
            candidates += og_values(response.text, image_names)
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
    expected_type = ""

    # Platform-specialized engines go first. Generic extractors are fallbacks.
    if platform == "snapchat":
        files = download_snapchat_dedicated(url, tmpdir)
        expected_type = "video"

    elif platform == "pinterest":
        files, expected_type = download_pinterest_dedicated(url, tmpdir)

    elif platform == "instagram" and not is_video_post_url(url):
        # gallery-dl handles Instagram multi-item posts/carousels better than
        # a single-video extractor and preserves all public media items.
        files = download_gallery(url, tmpdir)

    if not files:
        try:
            files = download_ytdlp(url, mode, tmpdir)
        except Exception as exc:
            first_error = exc

    if not files:
        for page_url in public_page_candidates(url):
            try:
                files = download_public_page_media(page_url, tmpdir)
                if files:
                    break
            except Exception as exc:
                log.info("public page parser failed: %s", str(exc)[:160])
                if first_error is None:
                    first_error = exc

    if not files and platform in {"instagram", "pinterest"} and mode in {"original", AUTO_MODE, *VIDEO_PRESETS}:
        files = download_gallery(resolve_public_redirect(url), tmpdir)

    if not files:
        for page_url in public_page_candidates(url):
            try:
                files = download_open_graph(page_url, tmpdir)
                if files:
                    break
            except Exception as exc:
                if first_error is None:
                    first_error = exc

    # Never degrade a known video post into its poster/thumbnail.
    force_video = is_video_post_url(url) or expected_type == "video"
    if force_video:
        files = [p for p in files if classify(p) == "video"]

    # If Pinterest itself says the pin is an image/GIF, keep only that type.
    if platform == "pinterest" and expected_type in {"image", "gif"}:
        wanted = "animation" if expected_type == "gif" else "image"
        typed = [p for p in files if classify(p) == wanted]
        if typed:
            files = typed

    if not files:
        if first_error:
            raise RuntimeError(friendly_error(platform, first_error)) from first_error
        raise RuntimeError("No downloadable media was found in this link.")

    usable = [p for p in files if p.stat().st_size <= MAX_SOURCE_BYTES]
    if not usable:
        raise RuntimeError(f"Source file is over the {MAX_SOURCE_BYTES // 1048576} MB service cap.")

    log.info(
        "grab success platform=%s count=%s types=%s",
        platform,
        len(usable),
        ",".join(classify(p) for p in usable),
    )
    return usable[:MAX_GALLERY_ITEMS]


def classify(path: Path) -> str:
    """Classify the actual media so Telegram receives it as the right type."""
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
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "")[-400:] or "ffmpeg failed")


def media_duration(path: Path) -> float:
    proc = subprocess.run(["ffmpeg", "-i", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=30)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
    if not match:
        return 0.0
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def fit_image(path: Path, dest: Path) -> Path:
    """Keep visual media as Telegram photo instead of falling back to document."""
    if path.stat().st_size <= MAX_PHOTO_BYTES and path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
        return path
    dest.mkdir(parents=True, exist_ok=True)
    for width, quality in ((4096, 4), (3072, 6), (2048, 8), (1600, 10)):
        out = dest / f"{path.stem}.{width}.jpg"
        ffmpeg([
            "ffmpeg", "-y", "-i", str(path), "-frames:v", "1",
            "-vf", f"scale='min({width},iw)':-2",
            "-q:v", str(quality), str(out),
        ], timeout=180)
        if out.exists() and out.stat().st_size <= MAX_PHOTO_BYTES:
            return out
    return out if out.exists() else path


def fit_animation(path: Path, dest: Path) -> Path:
    """Convert oversized/unsupported GIF-like media to Telegram animation MP4."""
    if path.stat().st_size <= MAX_BYTES and path.suffix.lower() in {".gif", ".mp4"}:
        return path
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{path.stem}.animation.mp4"
    ffmpeg([
        "ffmpeg", "-y", "-i", str(path), "-an",
        "-vf", "scale='min(1280,iw)':-2:flags=lanczos",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out),
    ])
    return out if out.exists() else path


def normalize_video(path: Path, dest: Path, max_height: int) -> Path:
    """Create a Telegram-streamable H.264/AAC MP4 when the source container/codec fails."""
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{path.stem}.normalized.mp4"
    ffmpeg([
        "ffmpeg", "-y", "-i", str(path),
        "-vf", f"scale=-2:'min({max_height},ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-pix_fmt", "yuv420p", str(out),
    ])
    return out if out.exists() else path


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
    ladder = [
        (height, video_bps),
        (min(height, 1080), int(video_bps * .82)),
        (min(height, 720), int(video_bps * .68)),
        (min(height, 480), int(video_bps * .52)),
        (360, max(int(video_bps * .42), 140_000)),
    ]
    out = dest / f"{path.stem}.telegram.mp4"
    seen: set[tuple[int, int]] = set()
    for h, bitrate in ladder:
        if (h, bitrate) in seen:
            continue
        seen.add((h, bitrate))
        ffmpeg([
            "ffmpeg", "-y", "-i", str(path),
            "-vf", f"scale=-2:'min({h},ih)'",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2),
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", "-pix_fmt", "yuv420p", str(out),
        ])
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
    ffmpeg([
        "ffmpeg", "-y", "-i", str(path), "-c", "copy", "-map", "0",
        "-f", "segment", "-segment_time", str(segment), "-reset_timestamps", "1", pattern,
    ])
    return sorted(p for p in dest.glob(f"{path.stem}.part*.mp4") if p.stat().st_size > 0)


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
        captionable = any(
            getattr(status, field, None)
            for field in ("photo", "video", "audio", "document", "animation")
        )
        if captionable:
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
    data = q.data or ""
    uid = q.from_user.id

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
    status = await q.message.reply_text("⬇️ Converting to MP3…")
    cache_path = str(action.get("cache_path") or "")
    if cache_path and Path(cache_path).exists():
        await run_cached_audio_job(
            q.message,
            context,
            uid,
            Path(cache_path),
            str(action.get("title") or ""),
            status,
        )
    else:
        await run_job(q.message, context, uid, url, "mp3_192", status)


def telegram_retry_delay(exc: Exception, attempt: int) -> float:
    if isinstance(exc, RetryAfter):
        value = getattr(exc, "retry_after", 1)
        try:
            seconds = value.total_seconds() if hasattr(value, "total_seconds") else float(value)
            return max(1.0, min(seconds + 0.5, 30.0))
        except Exception:
            return 2.0
    return float(min(2 ** attempt, 8))


async def _retry_telegram(call, attempts: int = 3):
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await call()
        except (RetryAfter, TimedOut, NetworkError) as exc:
            last = exc
            if attempt + 1 >= attempts:
                break
            await asyncio.sleep(telegram_retry_delay(exc, attempt))
    if last:
        raise last
    raise RuntimeError("Telegram operation failed")


async def send_image(msg, path: Path, caption: str, tmp: Path) -> int:
    final = await asyncio.to_thread(fit_image, path, tmp / "fit_images")
    if final.stat().st_size > MAX_PHOTO_BYTES:
        raise RuntimeError("Photo could not be fitted for Telegram.")

    async def send_once():
        with final.open("rb") as fh:
            return await msg.reply_photo(photo=fh, caption=caption or None)

    await _retry_telegram(send_once)
    return 1


async def send_animation(msg, path: Path, caption: str, tmp: Path) -> int:
    final = path
    if final.stat().st_size > MAX_BYTES or final.suffix.lower() not in {".gif", ".mp4"}:
        final = await asyncio.to_thread(fit_animation, final, tmp / "fit_animation")
    if final.stat().st_size > MAX_BYTES:
        raise RuntimeError("Animation could not be fitted for Telegram.")

    async def send_once():
        with final.open("rb") as fh:
            return await msg.reply_animation(animation=fh, caption=caption or None, filename=final.name)

    await _retry_telegram(send_once)
    return 1


async def _send_video_file(msg, path: Path, caption: str, reply_markup=None) -> None:
    async def send_once():
        with path.open("rb") as fh:
            return await msg.reply_video(
                video=fh,
                caption=caption or None,
                filename=path.name,
                supports_streaming=True,
                reply_markup=reply_markup,
            )
    await _retry_telegram(send_once)


async def send_video(msg, path: Path, caption: str, max_height: int, tmp: Path, reply_markup=None) -> int:
    # First try the original file unchanged when it already fits Telegram.
    if path.stat().st_size <= MAX_BYTES:
        try:
            await _send_video_file(msg, path, caption, reply_markup)
            return 1
        except (RetryAfter, TimedOut, NetworkError):
            raise
        except TelegramError as exc:
            log.info("Telegram rejected source video; normalizing: %s", str(exc)[:140])
            normalized = await asyncio.to_thread(normalize_video, path, tmp / "normalized", max_height)
            if normalized.stat().st_size > MAX_BYTES:
                normalized = await asyncio.to_thread(fit_video, normalized, tmp / "fit_normalized", max_height)
            if normalized.stat().st_size <= MAX_BYTES:
                await _send_video_file(msg, normalized, caption, reply_markup)
                return 1

    # Long files preserve quality by splitting before recompression.
    duration = await asyncio.to_thread(media_duration, path)
    if path.stat().st_size > MAX_BYTES and duration >= 8 * 60:
        try:
            parts = await asyncio.to_thread(split_video, path, tmp / "parts_original")
        except Exception:
            parts = []
        if parts and all(p.stat().st_size <= MAX_BYTES for p in parts):
            for index, part in enumerate(parts, 1):
                markup = reply_markup if index == len(parts) else None
                await _send_video_file(
                    msg,
                    part,
                    f"{caption}\nPart {index}/{len(parts)}" if caption else f"Part {index}/{len(parts)}",
                    markup,
                )
            return len(parts)

    final = await asyncio.to_thread(fit_video, path, tmp / "fit", max_height)
    if final.stat().st_size > MAX_BYTES:
        raise RuntimeError("Video could not be fitted for Telegram.")

    try:
        await _send_video_file(msg, final, caption, reply_markup)
        return 1
    except (RetryAfter, TimedOut, NetworkError):
        raise
    except TelegramError:
        normalized = await asyncio.to_thread(normalize_video, final, tmp / "normalized_final", max_height)
        if normalized.stat().st_size > MAX_BYTES:
            normalized = await asyncio.to_thread(fit_video, normalized, tmp / "fit_final", max_height)
        if normalized.stat().st_size > MAX_BYTES:
            raise RuntimeError("Video could not be fitted for Telegram.")
        await _send_video_file(msg, normalized, caption, reply_markup)
        return 1


def safe_media_title(value: str) -> str:
    value = re.sub(r"[\r\n\t]+", " ", value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value[:120] or "Veltrix Audio"


async def send_audio(msg, path: Path, caption: str, mode: str, tmp: Path, title: str = "") -> int:
    preset = AUDIO_PRESETS.get(mode) or AUDIO_PRESETS["mp3_192"]
    final = path
    wanted_ext = ".m4a" if preset["codec"] == "m4a" else ".mp3"
    if final.stat().st_size > MAX_BYTES or final.suffix.lower() != wanted_ext:
        final = await asyncio.to_thread(
            fit_audio,
            final,
            tmp / "fit_audio",
            str(preset["codec"]),
            int(preset["bitrate"]),
        )
    if final.stat().st_size > MAX_BYTES:
        raise RuntimeError("Audio could not be fitted for Telegram.")
    async def send_once():
        with final.open("rb") as fh:
            return await msg.reply_audio(
                audio=fh,
                caption=caption or None,
                filename=final.name,
                title=safe_media_title(title) if title else None,
                performer="Veltrix Downloader",
            )
    await _retry_telegram(send_once)
    return 1


async def send_document(msg, path: Path, caption: str) -> int:
    if path.stat().st_size > MAX_BYTES:
        raise RuntimeError("Unsupported media is over Telegram's upload limit.")

    async def send_once():
        with path.open("rb") as fh:
            return await msg.reply_document(document=fh, caption=caption or None, filename=path.name)

    await _retry_telegram(send_once)
    return 1


async def run_cached_audio_job(msg, context, uid: int, source: Path, title: str, status) -> None:
    """Fast MP3 path using the exact video already downloaded for this button."""
    lock = job_lock(uid)
    metric_add("jobs_started", 1)
    if lock.locked():
        metric_add("queued_jobs", 1)
        await edit_status(status, "⏳ Queued…")

    tmp = Path(tempfile.mkdtemp(prefix="vx_cache_"))
    async with lock:
        if metric_snapshot().get("queued_jobs", 0) > 0:
            metric_add("queued_jobs", -1)
        metric_add("active_jobs", 1)
        try:
            if not source.exists():
                raise RuntimeError("Cached media expired.")
            sent = await send_audio(
                msg,
                source,
                "⚡ Veltrix Downloader · MP3",
                "mp3_192",
                tmp,
                title,
            )
            if not sent:
                raise RuntimeError("MP3 conversion failed.")
            metric_add("jobs_succeeded", 1)
            try:
                await status.delete()
            except TelegramError:
                await edit_status(status, "✅ Done")
        except Exception as exc:
            metric_add("jobs_failed", 1)
            log.exception("cached MP3 job failed")
            await edit_status(status, "❌ MP3 conversion failed. Send the link again.")
        finally:
            metric_add("active_jobs", -1)
            shutil.rmtree(tmp, ignore_errors=True)


async def run_job(msg, context, uid: int, url: str, mode: str, status=None) -> None:
    lock = job_lock(uid)
    metric_add("jobs_started", 1)
    if status is None:
        status = await msg.reply_text("⬇️ Downloading…")
    elif lock.locked():
        metric_add("queued_jobs", 1)
        await edit_status(status, "⏳ Queued…")

    async def typing() -> None:
        try:
            while True:
                await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_DOCUMENT)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return

    tmp = Path(tempfile.mkdtemp(prefix="vx_"))
    platform = platform_of(url) or "web"
    typing_task: asyncio.Task | None = None

    async with lock:
        if metric_snapshot().get("queued_jobs", 0) > 0:
            metric_add("queued_jobs", -1)
        metric_add("active_jobs", 1)
        await edit_status(status, f"⬇️ Downloading from {PLATFORMS.get(platform, {}).get('label', 'Media')}…")
        typing_task = asyncio.create_task(typing())
        async with global_sem():
            try:
                meta = {}
                try:
                    meta = await asyncio.wait_for(asyncio.to_thread(preview_media, url), timeout=15)
                except Exception:
                    meta = {}
                files = await asyncio.to_thread(grab, url, mode, str(tmp))
                kinds = [classify(p) for p in files]
                sent = 0
                caption = f"⚡ Veltrix Downloader · {PLATFORMS.get(platform, {}).get('label', 'Media')}"

                if mode in AUDIO_PRESETS:
                    sources = [p for p, kind in zip(files, kinds) if kind in {"audio", "video", "animation"}]
                    if not sources:
                        raise RuntimeError("No audio/video stream was found in this post.")
                    for index, source in enumerate(sources):
                        sent += await send_audio(
                            msg,
                            source,
                            caption if index == 0 else "",
                            mode,
                            tmp,
                            str(meta.get("title") or ""),
                        )
                else:
                    requested = 2160 if mode == "original" else (720 if mode == AUTO_MODE else int(VIDEO_PRESETS[mode]["height"]))
                    for index, (path, kind) in enumerate(zip(files, kinds)):
                        item_caption = caption if sent == 0 else ""
                        if kind == "image":
                            sent += await send_image(msg, path, item_caption, tmp)
                        elif kind == "animation":
                            sent += await send_animation(msg, path, item_caption, tmp)
                        elif kind == "video":
                            markup = mp3_button(
                                uid,
                                url,
                                source_path=path,
                                title=str(meta.get("title") or ""),
                            )
                            sent += await send_video(
                                msg,
                                path,
                                item_caption,
                                requested,
                                tmp,
                                markup,
                            )
                        elif kind == "audio":
                            sent += await send_audio(msg, path, item_caption, "mp3_192", tmp)
                        else:
                            # Rare unknown files are kept rather than silently lost.
                            sent += await send_document(msg, path, item_caption)

                if not sent:
                    raise RuntimeError("Nothing downloadable was returned.")

                metric_add("jobs_succeeded", 1)
                try:
                    await status.delete()
                except TelegramError:
                    await edit_status(status, "✅ Done")

            except Exception as exc:
                metric_add("jobs_failed", 1)
                log.exception("download job failed")
                safe_message = (
                    friendly_error(platform, exc)
                    if platform in PLATFORMS
                    else "Download failed. Please try another link."
                )
                await edit_status(status, f"❌ {safe_message}")
            finally:
                metric_add("active_jobs", -1)
                if typing_task is not None:
                    typing_task.cancel()
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
            body = json.dumps({
                "ok": True,
                "service": "veltrix-downloader",
                "version": VERSION,
                "platforms": list(PLATFORMS),
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "deno": bool(DENO_BIN),
                "temp_free_mb": free_mb,
                "metrics": metric_snapshot(),
            }).encode()
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
    cleanup_stale_temp()
    cleanup_media_cache()
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Veltrix Downloader %s started", VERSION)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
