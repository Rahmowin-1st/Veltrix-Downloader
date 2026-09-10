#!/usr/bin/env python3
"""Veltrix Downloader — YouTube, Instagram, Pinterest, Snapchat."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_BYTES = 48 * 1024 * 1024
MAX_PHOTO = 9 * 1024 * 1024
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
USERS_FILE = DATA_DIR / "users.json"
COOKIES = Path("cookies.txt")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("veltrix")
_user_lock = Lock()
_job_locks: dict[int, asyncio.Lock] = {}

URL_RE = re.compile(r"(https?://[^\s<>\"']+)|(www\.[^\s<>\"']+)", re.I)

HOSTS = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com"),
    "instagram": ("instagram.com", "instagr.am"),
    "pinterest": ("pinterest.com", "pinterest.co", "pin.it"),
    "snapchat": ("snapchat.com", "snap.com"),
    "tiktok": ("tiktok.com", "vm.tiktok.com"),
    "x": ("twitter.com", "x.com"),
}

DEFAULT_KEY = "720"

PRESETS: dict[str, dict[str, Any]] = {
    "best": {"label": "Best", "kind": "video", "height": 2160},
    "1080": {"label": "1080p", "kind": "video", "height": 1080},
    "720": {"label": "720p", "kind": "video", "height": 720},
    "480": {"label": "480p", "kind": "video", "height": 480},
    "360": {"label": "360p", "kind": "video", "height": 360},
    "mp3": {"label": "MP3 320", "kind": "audio", "height": 0},
    "m4a": {"label": "M4A", "kind": "audio", "height": 0},
}


def job_lock(uid: int) -> asyncio.Lock:
    lock = _job_locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _job_locks[uid] = lock
    return lock


def load_users() -> dict:
    if not USERS_FILE.exists():
        return {}
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_users(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(USERS_FILE)


def user_cfg(uid: int) -> dict[str, str]:
    with _user_lock:
        users = load_users()
        row = users.get(str(uid)) or {}
        key = row.get("quality") or DEFAULT_KEY
        if key not in PRESETS:
            key = DEFAULT_KEY
        return {"quality": key}


def set_quality(uid: int, key: str) -> None:
    if key not in PRESETS:
        return
    with _user_lock:
        users = load_users()
        users[str(uid)] = {"quality": key}
        save_users(users)


def extract_url(text: str) -> str | None:
    if not text:
        return None
    m = URL_RE.search(text.strip())
    if not m:
        return None
    url = (m.group(0) or "").rstrip(").,]\"'")
    if url.startswith("www."):
        url = "https://" + url
    return url


def site_of(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for name, suffixes in HOSTS.items():
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return name
    return "web"


def format_for(key: str) -> str:
    if key == "mp3":
        return "bestaudio/ba/b"
    if key == "m4a":
        return "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/ba"
    if key == "best":
        return "bv*+ba/b"
    h = PRESETS[key]["height"]
    return (
        f"bv*[height={h}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height={h}]+ba/"
        f"b[height={h}]/"
        f"bv*[height<={h}][height>={max(h-80, 1)}][ext=mp4]+ba/"
        f"bv*[height<={h}]+ba/"
        f"b[height<={h}]/"
        f"bv*+ba/b"
    )


def base_opts(tmpdir: str) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 12,
        "fragment_retries": 12,
        "extractor_retries": 5,
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10_485_760,
        "socket_timeout": 25,
        "outtmpl": str(Path(tmpdir) / "%(id)s_%(autonumber)s.%(ext)s"),
        "restrictfilenames": True,
        "overwrites": True,
        "cachedir": False,
        "ignoreerrors": False,
        "merge_output_format": "mp4",
        "writethumbnail": False,
        "geo_bypass": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web", "ios"]}},
    }
    if COOKIES.exists() and COOKIES.stat().st_size > 32:
        opts["cookiefile"] = str(COOKIES)
    return opts


def probe(url: str) -> dict[str, Any]:
    opts = base_opts(tempfile.gettempdir())
    opts["skip_download"] = True
    opts["extract_flat"] = False
    last = None
    for _ in range(3):
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                return info
        except Exception as exc:
            last = exc
    raise last or DownloadError("Could not read this link")


def download_media(url: str, key: str, tmpdir: str) -> list[Path]:
    Path(tmpdir).mkdir(parents=True, exist_ok=True)
    spec = PRESETS[key]
    opts = base_opts(tmpdir)
    if spec["kind"] == "audio":
        opts["format"] = format_for(key)
        codec = "mp3" if key == "mp3" else "m4a"
        q = "320" if key == "mp3" else "0"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": q}
        ]
    else:
        opts["format"] = format_for(key)
    before = {p.name for p in Path(tmpdir).iterdir()}
    last = None
    for fmt in (opts.get("format"), "bv*+ba/b", "best"):
        try:
            opts["format"] = fmt
            with YoutubeDL(opts) as ydl:
                ydl.download([url])
            last = None
            break
        except Exception as exc:
            last = exc
    if last:
        raise last
    files = [
        p for p in Path(tmpdir).iterdir()
        if p.is_file() and p.name not in before and p.suffix.lower() not in {".json", ".vtt", ".srt"}
    ]
    if not files:
        files = [p for p in Path(tmpdir).iterdir() if p.is_file()]
    files = [p for p in files if p.stat().st_size > 0]
    files.sort(key=lambda p: p.name)
    if not files:
        raise FileNotFoundError("Download produced no file")
    return files


def _run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-300:] or "ffmpeg failed")


def compress_under_limit(src: Path, dest_dir: Path, duration: int, want_height: int, kind: str) -> Path:
    if src.stat().st_size <= MAX_BYTES:
        return src
    dest_dir.mkdir(parents=True, exist_ok=True)
    dur = max(int(duration or 0), 1)
    if kind == "audio" or src.suffix.lower() in {".mp3", ".m4a", ".opus", ".ogg"}:
        out = dest_dir / f"{src.stem}.tg.mp3"
        for br in ("256k", "192k", "160k", "128k", "96k"):
            _run_ffmpeg(["ffmpeg", "-y", "-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", br, str(out)])
            if out.exists() and out.stat().st_size <= MAX_BYTES:
                return out
        return out
    total_bps = int((MAX_BYTES * 8) / dur)
    video_bps = max(total_bps - 96_000, 120_000)
    height = want_height or 720
    if video_bps < 1_200_000 and height > 720:
        height = 720
    if video_bps < 700_000 and height > 480:
        height = 480
    if video_bps < 350_000:
        height = 360
    out = dest_dir / f"{src.stem}.{height}p.mp4"
    for scale_h, vb in ((height, video_bps), (min(height, 720), int(video_bps * 0.8)), (480, int(video_bps * 0.55)), (360, 180_000)):
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", str(src),
            "-vf", f"scale=-2:{scale_h}",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", str(vb), "-maxrate", str(vb), "-bufsize", str(vb * 2),
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", "-pix_fmt", "yuv420p",
            str(out),
        ])
        if out.exists() and out.stat().st_size <= MAX_BYTES:
            return out
    return out


def default_button(key: str) -> InlineKeyboardMarkup:
    label = PRESETS.get(key, PRESETS[DEFAULT_KEY])["label"]
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"Default ({label})", callback_data="open_settings")]])


def settings_keyboard(current: str) -> InlineKeyboardMarkup:
    def mark(k: str) -> str:
        lab = PRESETS[k]["label"]
        return f"\u2022 {lab}" if k == current else lab
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(mark("1080"), callback_data="set|1080"), InlineKeyboardButton(mark("720"), callback_data="set|720"), InlineKeyboardButton(mark("480"), callback_data="set|480")],
        [InlineKeyboardButton(mark("360"), callback_data="set|360"), InlineKeyboardButton(mark("best"), callback_data="set|best")],
        [InlineKeyboardButton(mark("mp3"), callback_data="set|mp3"), InlineKeyboardButton(mark("m4a"), callback_data="set|m4a")],
    ])


def classify(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return "image"
    if ext in {".mp3", ".m4a", ".opus", ".ogg", ".wav"}:
        return "audio"
    return "video"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    key = user_cfg(uid)["quality"]
    await update.message.reply_text(
        "Veltrix Downloader\n\n"
        "Send a link from YouTube, Instagram, Pinterest or Snapchat.\n"
        "Photos in a post are sent one by one.\n\n"
        f"Saved quality: Default ({PRESETS[key]['label']})\n"
        "Tap that button anytime to change it. New links use the saved default.\n\n"
        "/settings \u2014 change default\n"
        "/help \u2014 details",
        reply_markup=default_button(key),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "YouTube \u00b7 Instagram \u00b7 Pinterest \u00b7 Snapchat Spotlight\n"
        "Video defaults to 720p until you change Default.\n"
        "Audio: MP3 320 or M4A.\n"
        "Images keep original quality (document if photo would be crushed).\n"
        "Bot API cap is 50 MB. Private posts need cookies.txt on the server."
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = user_cfg(update.effective_user.id)["quality"]
    await update.message.reply_text(
        f"Default quality is {PRESETS[key]['label']}.\nChoose a new default:",
        reply_markup=settings_keyboard(key),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    url = extract_url(msg.text or "")
    if not url:
        await msg.reply_text("Send a YouTube, Instagram, Pinterest or Snapchat link.")
        return
    key = user_cfg(user.id)["quality"]
    context.user_data["last_url"] = url
    await run_job(msg, context, user.id, url, key, status_msg=None)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id
    if data == "open_settings":
        key = user_cfg(uid)["quality"]
        await q.message.reply_text(
            f"Default is {PRESETS[key]['label']}. Pick a new default:",
            reply_markup=settings_keyboard(key),
        )
        return
    if data.startswith("set|"):
        key = data.split("|", 1)[1]
        if key not in PRESETS:
            return
        set_quality(uid, key)
        await q.edit_message_text(
            f"Default saved: {PRESETS[key]['label']}\nNext links use this automatically."
        )


async def run_job(msg, context, uid: int, url: str, key: str, status_msg) -> None:
    lock = job_lock(uid)
    if lock.locked():
        await msg.reply_text("A download is already running. Wait for it.")
        return
    spec = PRESETS[key]
    site = site_of(url)
    status = status_msg or await msg.reply_text(
        f"Downloading...\nDefault ({spec['label']}) \u00b7 {site}",
        reply_markup=default_button(key),
    )

    async def typing() -> None:
        try:
            while True:
                await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_DOCUMENT)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return

    task = asyncio.create_task(typing())
    tmpdir = tempfile.mkdtemp(prefix="veltrix_")
    async with lock:
        try:
            info = await asyncio.to_thread(probe, url)
            title = (info.get("title") or info.get("fulltitle") or site)[:180]
            duration = int(info.get("duration") or 0)
            files = await asyncio.to_thread(download_media, url, key, tmpdir)
            images = [p for p in files if classify(p) == "image"]
            audios = [p for p in files if classify(p) == "audio"]
            videos = [p for p in files if classify(p) == "video"]
            sent = 0
            if images:
                await status.edit_text(f"Downloading... sending {len(images)} image(s)")
                batch: list[Path] = []
                for img in images:
                    if img.stat().st_size > MAX_PHOTO:
                        with img.open("rb") as fh:
                            await msg.reply_document(document=fh, filename=img.name, caption=title[:200])
                        sent += 1
                    else:
                        batch.append(img)
                        if len(batch) == 10:
                            sent += await _send_album(msg, batch, title)
                            batch = []
                if len(batch) == 1:
                    with batch[0].open("rb") as fh:
                        await msg.reply_photo(photo=fh, caption=title[:200])
                    sent += 1
                elif batch:
                    sent += await _send_album(msg, batch, title)
            out_dir = Path(tmpdir) / "out"
            for path in videos:
                if path.stat().st_size > MAX_BYTES:
                    await status.edit_text("Downloading... compressing to fit 50 MB")
                    path = await asyncio.to_thread(compress_under_limit, path, out_dir, duration, spec["height"], "video")
                if path.stat().st_size > MAX_BYTES:
                    await msg.reply_text("File still exceeds 50 MB after compression. Change Default to 480p.")
                    continue
                cap = f"{title}\n{spec['label']} \u00b7 {path.stat().st_size / 1048576:.1f} MB"
                with path.open("rb") as fh:
                    try:
                        await msg.reply_video(video=fh, caption=cap, filename=path.name, supports_streaming=True)
                    except TelegramError:
                        fh.seek(0)
                        await msg.reply_document(document=fh, caption=cap, filename=path.name)
                sent += 1
            for path in audios:
                if path.stat().st_size > MAX_BYTES:
                    path = await asyncio.to_thread(compress_under_limit, path, out_dir, duration, 0, "audio")
                cap = f"{title}\n{spec['label']} \u00b7 {path.stat().st_size / 1048576:.1f} MB"
                with path.open("rb") as fh:
                    await msg.reply_audio(audio=fh, caption=cap, title=title[:64], filename=path.name)
                sent += 1
            if sent == 0:
                raise RuntimeError("Nothing could be sent from this link")
            try:
                await status.edit_text(f"Sent {sent} file(s) \u00b7 Default ({spec['label']})", reply_markup=default_button(key))
            except TelegramError:
                pass
        except Exception as exc:
            log.exception("job failed")
            hint = _friendly(str(exc), site)
            try:
                await status.edit_text(hint, reply_markup=default_button(key))
            except TelegramError:
                await msg.reply_text(hint)
        finally:
            task.cancel()
            shutil.rmtree(tmpdir, ignore_errors=True)


async def _send_album(msg, paths: list[Path], caption: str) -> int:
    media = []
    handles = []
    try:
        for i, p in enumerate(paths):
            fh = p.open("rb")
            handles.append(fh)
            media.append(InputMediaPhoto(media=fh, caption=caption[:200] if i == 0 else None))
        await msg.reply_media_group(media=media)
        return len(paths)
    except TelegramError:
        n = 0
        for p in paths:
            with p.open("rb") as fh:
                await msg.reply_photo(photo=fh)
            n += 1
        return n
    finally:
        for fh in handles:
            try:
                fh.close()
            except Exception:
                pass


def _friendly(reason: str, site: str) -> str:
    r = reason.lower()
    if "403" in r or "forbidden" in r:
        return f"{site}: source blocked this server IP. Put cookies.txt next to bot.py on Render and redeploy."
    if "login" in r or "private" in r or "age" in r:
        return f"{site}: this post is private or login-only. Public links work without cookies."
    if "unsupported" in r or "no video" in r or "not a valid" in r:
        return f"{site}: this URL type is not downloadable (closed story / private snap)."
    return f"{site}: could not finish this one. Try another public link.\n{reason[:240]}"


def start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Veltrix Downloader OK")
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
    log.info("Veltrix Downloader started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
