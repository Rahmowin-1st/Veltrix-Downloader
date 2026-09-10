#!/usr/bin/env python3
"""Veltrix Downloader — max media from public links."""

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
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_BYTES = 48 * 1024 * 1024
MAX_PHOTO = 9 * 1024 * 1024
MAX_FILES = 40
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
USERS_FILE = DATA_DIR / "users.json"
COOKIES = Path(os.getenv("COOKIES_FILE", "cookies.txt"))
GDL_CONF = Path("gallery-dl.conf")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("veltrix")
_user_lock = Lock()
_job_locks: dict[int, asyncio.Lock] = {}

URL_RE = re.compile(r"(https?://[^\s<>\"']+)|(www\.[^\s<>\"']+)", re.I)
HOSTS = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com"),
    "instagram": ("instagram.com", "instagr.am"),
    "pinterest": ("pinterest.com", "pinterest.co", "pinterest.ru", "pin.it"),
    "snapchat": ("snapchat.com", "snap.com"),
    "tiktok": ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com"),
    "x": ("twitter.com", "x.com"),
    "facebook": ("facebook.com", "fb.watch", "fb.com"),
    "reddit": ("reddit.com", "redd.it"),
    "vimeo": ("vimeo.com"),
    "threads": ("threads.net", "threads.com"),
    "vk": ("vk.com", "vk.ru", "vkvideo.ru"),
    "soundcloud": ("soundcloud.com"),
    "dailymotion": ("dailymotion.com", "dai.ly"),
    "twitch": ("twitch.tv", "clips.twitch.tv"),
    "tumblr": ("tumblr.com"),
}
IMAGE_SITES = {"instagram", "pinterest", "snapchat", "tumblr", "reddit"}
DEFAULT_KEY = "720"
PRESETS = {
    "best": {"label": "Best", "kind": "video", "height": 2160},
    "1080": {"label": "1080p", "kind": "video", "height": 1080},
    "720": {"label": "720p", "kind": "video", "height": 720},
    "480": {"label": "480p", "kind": "video", "height": 480},
    "360": {"label": "360p", "kind": "video", "height": 360},
    "mp3": {"label": "MP3 320", "kind": "audio", "height": 0},
    "m4a": {"label": "M4A", "kind": "audio", "height": 0},
}
HAS_ARIA = bool(shutil.which("aria2c"))


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
        users = load_users()
        row = users.get(str(uid)) or {}
        q = row.get("quality") or DEFAULT_KEY
        if q not in PRESETS:
            q = DEFAULT_KEY
        return {"quality": q, "last_url": row.get("last_url") or ""}


def patch_user(uid: int, **fields: str) -> dict:
    with _user_lock:
        users = load_users()
        row = users.get(str(uid)) or {}
        row.update({k: v for k, v in fields.items() if v is not None})
        if row.get("quality") not in PRESETS:
            row["quality"] = DEFAULT_KEY
        users[str(uid)] = row
        save_users(users)
        return row


def ensure_cookie_file() -> Path | None:
    if COOKIES.exists() and COOKIES.stat().st_size > 32:
        return COOKIES
    sid = os.getenv("INSTAGRAM_SESSIONID", "").strip()
    if not sid:
        return None
    COOKIES.write_text(
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t" + sid + "\n",
        encoding="utf-8",
    )
    return COOKIES


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
    return host.split(":")[0] or "web"


def format_for(key: str) -> str:
    if key == "mp3":
        return "bestaudio/ba/b"
    if key == "m4a":
        return "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/ba"
    if key == "best":
        return "bv*+ba/b"
    h = PRESETS[key]["height"]
    return (
        f"bv*[height={h}][ext=mp4]+ba[ext=m4a]/bv*[height={h}]+ba/b[height={h}]/"
        f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
    )


def ydl_opts(tmpdir: str, key: str, site: str) -> dict[str, Any]:
    spec = PRESETS[key]
    opts: dict[str, Any] = {
        "noplaylist": site == "youtube",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 8,
        "fragment_retries": 8,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 16,
        "file_access_retries": 3,
        "socket_timeout": 15,
        "http_chunk_size": 10_485_760,
        "outtmpl": str(Path(tmpdir) / "%(id)s_%(autonumber)03d.%(ext)s"),
        "restrictfilenames": True,
        "overwrites": True,
        "cachedir": False,
        "ignoreerrors": True,
        "skip_unavailable_fragments": True,
        "merge_output_format": "mp4",
        "geo_bypass": True,
        "playlistend": MAX_FILES,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
        "format": format_for(key),
    }
    ck = ensure_cookie_file()
    if ck:
        opts["cookiefile"] = str(ck)
    if HAS_ARIA and site not in {"youtube"}:
        opts["external_downloader"] = {"http": "aria2c", "https": "aria2c"}
        opts["external_downloader_args"] = {"aria2c": ["-x16", "-s16", "-k1M", "--file-allocation=none"]}
    if spec["kind"] == "audio":
        codec = "mp3" if key == "mp3" else "m4a"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": "320" if key == "mp3" else "0"}]
    return opts


def download_ytdlp(url: str, key: str, tmpdir: str, site: str) -> list[Path]:
    Path(tmpdir).mkdir(parents=True, exist_ok=True)
    before = {p.name for p in Path(tmpdir).iterdir()}
    opts = ydl_opts(tmpdir, key, site)
    last = None
    for fmt in (opts["format"], "bv*+ba/b", "best", "bestvideo+bestaudio/best"):
        try:
            opts["format"] = fmt
            with YoutubeDL(opts) as ydl:
                ydl.download([url])
            last = None
            break
        except Exception as exc:
            last = exc
    files = _new_files(tmpdir, before)
    if files:
        return files
    if last:
        raise last
    return []


def download_gallery(url: str, tmpdir: str) -> list[Path]:
    dest = Path(tmpdir) / "gdl"
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["python", "-m", "gallery_dl", "-d", str(dest), "--no-mtime", "-q", "--range", f"1-{MAX_FILES}"]
    if GDL_CONF.exists():
        cmd.extend(["-c", str(GDL_CONF)])
    cmd.extend([
        "-o", "extractor.pinterest.videos=true",
        "-o", "extractor.pinterest.stories=true",
        "-o", "extractor.pinterest.sections=true",
        "-o", "extractor.instagram.videos=true",
    ])
    ck = ensure_cookie_file()
    if ck:
        cmd.extend(["--cookies", str(ck)])
    cmd.append(url)
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    files = [p for p in dest.rglob("*") if p.is_file() and p.stat().st_size > 0]
    if proc.returncode != 0 and not files:
        log.warning("gallery-dl: %s", (proc.stderr or "")[-200:])
        return []
    return sorted(files, key=lambda p: p.name)


def _new_files(tmpdir: str, before: set[str]) -> list[Path]:
    skip = {".json", ".vtt", ".srt", ".ass", ".nfo", ".part"}
    files = [
        p for p in Path(tmpdir).rglob("*")
        if p.is_file() and p.suffix.lower() not in skip and p.stat().st_size > 0 and p.name not in before
    ]
    files.sort(key=lambda p: p.name)
    return files


def merge_unique(base: list[Path], extra: list[Path]) -> list[Path]:
    seen = {p.name for p in base}
    out = list(base)
    for p in extra:
        if p.name not in seen:
            out.append(p)
            seen.add(p.name)
    return out


def grab(url: str, key: str, tmpdir: str, site: str) -> list[Path]:
    files: list[Path] = []
    err = None
    try:
        files = download_ytdlp(url, key, tmpdir, site)
    except Exception as exc:
        err = exc
        log.warning("yt-dlp %s: %s", site, exc)
    if site in IMAGE_SITES or site in {"pinterest", "snapchat", "instagram"} or not files:
        extra = download_gallery(url, tmpdir)
        files = merge_unique(files, extra)
    if not files and err:
        raise err
    if not files:
        raise RuntimeError("no media from this link")
    return files[:MAX_FILES]


def _ff(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-240:] or "ffmpeg failed")


def compress(src: Path, dest_dir: Path, duration: int, want_height: int, kind: str) -> Path:
    if src.stat().st_size <= MAX_BYTES:
        return src
    dest_dir.mkdir(parents=True, exist_ok=True)
    dur = max(int(duration or 0), 1)
    if kind == "audio" or src.suffix.lower() in {".mp3", ".m4a", ".opus", ".ogg"}:
        out = dest_dir / f"{src.stem}.tg.mp3"
        for br in ("256k", "192k", "128k", "96k"):
            _ff(["ffmpeg", "-y", "-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", br, str(out)])
            if out.exists() and out.stat().st_size <= MAX_BYTES:
                return out
        return out
    vb = max(int((MAX_BYTES * 8) / dur) - 96_000, 120_000)
    h = want_height or 720
    if vb < 1_200_000 and h > 720:
        h = 720
    if vb < 700_000 and h > 480:
        h = 480
    out = dest_dir / f"{src.stem}.{h}p.mp4"
    _ff([
        "ffmpeg", "-y", "-i", str(src), "-vf", f"scale=-2:{h}",
        "-c:v", "libx264", "-preset", "ultrafast", "-b:v", str(vb),
        "-maxrate", str(vb), "-bufsize", str(vb * 2), "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", "-pix_fmt", "yuv420p", str(out),
    ])
    return out if out.exists() else src


def action_kb(key: str) -> InlineKeyboardMarkup:
    lab = PRESETS[key]["label"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Default ({lab})", callback_data="open_settings")],
        [
            InlineKeyboardButton("1080p", callback_data="go|1080"),
            InlineKeyboardButton("720p", callback_data="go|720"),
            InlineKeyboardButton("480p", callback_data="go|480"),
            InlineKeyboardButton("MP3", callback_data="go|mp3"),
        ],
    ])


def settings_kb(current: str) -> InlineKeyboardMarkup:
    def mark(k: str) -> str:
        return ("• " if k == current else "") + PRESETS[k]["label"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(mark("1080"), callback_data="set|1080"), InlineKeyboardButton(mark("720"), callback_data="set|720"), InlineKeyboardButton(mark("480"), callback_data="set|480")],
        [InlineKeyboardButton(mark("360"), callback_data="set|360"), InlineKeyboardButton(mark("best"), callback_data="set|best")],
        [InlineKeyboardButton(mark("mp3"), callback_data="set|mp3"), InlineKeyboardButton(mark("m4a"), callback_data="set|m4a")],
    ])


def classify(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        return "image"
    if ext in {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac"}:
        return "audio"
    return "video"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = user_row(update.effective_user.id)["quality"]
    await update.message.reply_text(
        "Veltrix Downloader\n\n"
        "YouTube · Instagram · Pinterest · Snapchat\n"
        "Also TikTok, X, Facebook, Reddit, Vimeo, Threads, VK, SoundCloud…\n"
        "Pinterest: originals + carousel + video pins.\n"
        "Snapchat: Spotlight and other public media yt-dlp can see.\n\n"
        f"Now: Default ({PRESETS[key]['label']})",
        reply_markup=action_kb(key),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Any public media link is tried (yt-dlp + gallery-dl).\n"
        "Pinterest carousel/story/video pins: original files, sent one by one.\n"
        "Snapchat private snaps still need the owner's session.\n"
        "Login-visible posts: cookies.txt or INSTAGRAM_SESSIONID on Render."
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = user_row(update.effective_user.id)["quality"]
    await update.message.reply_text(f"Default is {PRESETS[key]['label']}", reply_markup=settings_kb(key))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    uid = update.effective_user.id
    url = extract_url(msg.text or "")
    if not url:
        await msg.reply_text("Send a media link.")
        return
    key = user_row(uid)["quality"]
    patch_user(uid, last_url=url)
    await run_job(msg, context, uid, url, key)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id
    if data == "open_settings":
        key = user_row(uid)["quality"]
        await q.message.reply_text(f"Default is {PRESETS[key]['label']}", reply_markup=settings_kb(key))
        return
    if data.startswith("set|"):
        key = data.split("|", 1)[1]
        if key not in PRESETS:
            return
        patch_user(uid, quality=key)
        await q.edit_message_text(f"Default saved: {PRESETS[key]['label']}")
        return
    if data.startswith("go|"):
        key = data.split("|", 1)[1]
        if key not in PRESETS:
            return
        url = user_row(uid).get("last_url") or ""
        if not url:
            await q.message.reply_text("Send a link first.")
            return
        await run_job(q.message, context, uid, url, key)


async def run_job(msg, context, uid: int, url: str, key: str) -> None:
    lock = job_lock(uid)
    if lock.locked():
        await msg.reply_text("Still sending the previous file…")
        return
    spec = PRESETS[key]
    site = site_of(url) or "web"
    status = await msg.reply_text(f"Downloading… {spec['label']} · {site}", reply_markup=action_kb(key))

    async def typing() -> None:
        try:
            while True:
                await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_DOCUMENT)
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            return

    task = asyncio.create_task(typing())
    tmpdir = tempfile.mkdtemp(prefix="vx_")
    async with lock:
        try:
            files = await asyncio.to_thread(grab, url, key, tmpdir, site)
            images = [p for p in files if classify(p) == "image"]
            audios = [p for p in files if classify(p) == "audio"]
            videos = [p for p in files if classify(p) == "video"]
            sent = 0
            if images:
                sent += await send_images(msg, images, site)
            out_dir = Path(tmpdir) / "out"
            for path in videos:
                if path.stat().st_size > MAX_BYTES:
                    path = await asyncio.to_thread(compress, path, out_dir, 0, spec["height"], "video")
                if path.stat().st_size > MAX_BYTES:
                    continue
                cap = f"{spec['label']} · {path.stat().st_size / 1048576:.1f} MB"
                with path.open("rb") as fh:
                    try:
                        await msg.reply_video(video=fh, caption=cap, filename=path.name, supports_streaming=True)
                    except TelegramError:
                        fh.seek(0)
                        await msg.reply_document(document=fh, caption=cap, filename=path.name)
                sent += 1
            for path in audios:
                if path.stat().st_size > MAX_BYTES:
                    path = await asyncio.to_thread(compress, path, out_dir, 0, 0, "audio")
                cap = f"{spec['label']} · {path.stat().st_size / 1048576:.1f} MB"
                with path.open("rb") as fh:
                    await msg.reply_audio(audio=fh, caption=cap, filename=path.name)
                sent += 1
            if sent == 0:
                raise RuntimeError("nothing to send")
            await status.edit_text(
                f"Sent {sent} · Default ({PRESETS[user_row(uid)['quality']]['label']})",
                reply_markup=action_kb(user_row(uid)["quality"]),
            )
        except Exception as exc:
            log.exception("job")
            await status.edit_text(_friendly(str(exc), site), reply_markup=action_kb(key))
        finally:
            task.cancel()
            shutil.rmtree(tmpdir, ignore_errors=True)


async def send_images(msg, paths: list[Path], title: str) -> int:
    sent = 0
    batch: list[Path] = []
    for img in paths:
        if img.stat().st_size > MAX_PHOTO:
            with img.open("rb") as fh:
                await msg.reply_document(document=fh, filename=img.name, caption=title[:200])
            sent += 1
            continue
        batch.append(img)
        if len(batch) == 10:
            sent += await album(msg, batch, title)
            batch = []
    if len(batch) == 1:
        with batch[0].open("rb") as fh:
            await msg.reply_photo(photo=fh, caption=title[:200])
        sent += 1
    elif batch:
        sent += await album(msg, batch, title)
    return sent


async def album(msg, paths: list[Path], caption: str) -> int:
    media, handles = [], []
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
    if "403" in r or "login" in r or "private" in r or "cookie" in r:
        return f"{site}: needs your login. Add cookies.txt on Render."
    return f"{site}: {reason[:220]}"


def start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
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
    log.info("started aria=%s", HAS_ARIA)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
