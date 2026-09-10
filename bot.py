#!/usr/bin/env python3
"""Veltrix Downloader — free Telegram API: split large files."""

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
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_BYTES = 48 * 1024 * 1024
MAX_TOTAL = 300 * 1024 * 1024
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
YT_ID_RE = re.compile(r"(?:v=|/shorts/|/live/|youtu\.be/)([A-Za-z0-9_-]{11})")
HOSTS = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com"),
    "instagram": ("instagram.com", "instagr.am"),
    "pinterest": ("pinterest.com", "pinterest.co", "pin.it"),
    "snapchat": ("snapchat.com", "snap.com"),
    "tiktok": ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com"),
    "x": ("twitter.com", "x.com"),
    "facebook": ("facebook.com", "fb.watch"),
    "reddit": ("reddit.com", "redd.it"),
    "vimeo": ("vimeo.com"),
    "soundcloud": ("soundcloud.com"),
}
IMAGE_SITES = {"instagram", "pinterest", "snapchat", "reddit"}
VIDEO_KEYS = ("best", "1080", "720", "480", "360")
DEFAULT_KEY = "720"
PRESETS = {
    "best": {"label": "Best", "kind": "video", "height": 2160},
    "1080": {"label": "1080p", "kind": "video", "height": 1080},
    "720": {"label": "720p", "kind": "video", "height": 720},
    "480": {"label": "480p", "kind": "video", "height": 480},
    "360": {"label": "360p", "kind": "video", "height": 360},
    "mp3": {"label": "MP3", "kind": "audio", "height": 0},
}
HAS_ARIA = bool(shutil.which("aria2c"))


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
        log.warning("ffmpeg bootstrap failed: %s", exc)


ensure_ffmpeg()


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
        if q not in VIDEO_KEYS:
            q = DEFAULT_KEY
        return {"quality": q, "last_url": row.get("last_url") or ""}


def patch_user(uid: int, **fields: str) -> dict:
    with _user_lock:
        users = load_users()
        row = users.get(str(uid)) or {}
        row.update({k: v for k, v in fields.items() if v is not None})
        if row.get("quality") not in VIDEO_KEYS:
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
    return clean_url(url)


def clean_url(url: str) -> str:
    yt = YT_ID_RE.search(url)
    if yt:
        return f"https://www.youtube.com/watch?v={yt.group(1)}"
    parts = urlparse(url)
    if parts.query:
        q = parse_qs(parts.query)
        keep = {k: v for k, v in q.items() if k in {"v", "list", "t", "p"}}
        query = urlencode({k: v[0] for k, v in keep.items()})
        url = urlunparse((parts.scheme, parts.netloc, parts.path, "", query, ""))
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
    cap = "[filesize<300M][filesize_approx<300M]"
    if key == "best":
        return f"bv*{cap}+ba/b{cap}/bv*+ba/b"
    h = PRESETS[key]["height"]
    return (
        f"bv*[height={h}]{cap}+ba/bv*[height={h}]+ba/b[height={h}]/"
        f"bv*[height<={h}]{cap}+ba/bv*[height<={h}]+ba/b"
    )


def ydl_opts(tmpdir: str, key: str, site: str) -> dict[str, Any]:
    spec = PRESETS[key]
    out_dir = Path(tmpdir) / "ytdl"
    out_dir.mkdir(parents=True, exist_ok=True)
    opts: dict[str, Any] = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 12,
        "fragment_retries": 12,
        "extractor_retries": 5,
        "concurrent_fragment_downloads": 8,
        "socket_timeout": 20,
        "http_chunk_size": 10_485_760,
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "restrictfilenames": True,
        "overwrites": True,
        "cachedir": False,
        "ignoreerrors": False,
        "merge_output_format": "mp4",
        "geo_bypass": True,
        "nocheckcertificate": True,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "tv", "mweb", "web"]}},
        "format": format_for(key),
    }
    proxy = os.getenv("PROXY") or os.getenv("HTTPS_PROXY") or ""
    if proxy:
        opts["proxy"] = proxy
    ck = ensure_cookie_file()
    if ck:
        opts["cookiefile"] = str(ck)
    if spec["kind"] == "audio":
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    return opts


def _files_in(root: Path) -> list[Path]:
    if not root.exists():
        return []
    skip = {".json", ".vtt", ".srt", ".ass", ".nfo", ".part", ".ytdl"}
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() not in skip and p.stat().st_size > 0]
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return files


def download_ytdlp(url: str, key: str, tmpdir: str, site: str) -> list[Path]:
    opts = ydl_opts(tmpdir, key, site)
    last = None
    fmts = [opts["format"], "bv*+ba/b", "best", "bestaudio/ba/b"]
    seen: set[str] = set()
    for fmt in fmts:
        if fmt in seen:
            continue
        seen.add(fmt)
        try:
            opts["format"] = fmt
            with YoutubeDL(opts) as ydl:
                ydl.download([url])
            files = _files_in(Path(tmpdir) / "ytdl")
            if files:
                return files
        except Exception as exc:
            last = exc
            log.warning("yt-dlp %s fmt=%s: %s", site, fmt, exc)
    if last:
        raise last
    return []


def download_gallery(url: str, tmpdir: str) -> list[Path]:
    dest = Path(tmpdir) / "gdl"
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["python", "-m", "gallery_dl", "-d", str(dest), "--no-mtime", "-q", "--range", f"1-{MAX_FILES}"]
    if GDL_CONF.exists():
        cmd.extend(["-c", str(GDL_CONF)])
    ck = ensure_cookie_file()
    if ck:
        cmd.extend(["--cookies", str(ck)])
    cmd.append(url)
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    return _files_in(dest)


def grab(url: str, key: str, tmpdir: str, site: str) -> list[Path]:
    Path(tmpdir).mkdir(parents=True, exist_ok=True)
    err = None
    files: list[Path] = []
    try:
        files = download_ytdlp(url, key, tmpdir, site)
    except Exception as exc:
        err = exc
        log.warning("yt-dlp failed: %s", exc)
    if not files and site in IMAGE_SITES:
        files = download_gallery(url, tmpdir)
    if not files and err:
        raise err
    if not files:
        raise RuntimeError("YouTube blocked this server. Free Render IP is filtered.")
    kept = []
    for p in files:
        if p.stat().st_size > MAX_TOTAL:
            log.warning("skip >300MB %s", p.name)
            continue
        kept.append(p)
    if not kept:
        raise RuntimeError("File is over 300 MB free cap.")
    return kept[:MAX_FILES]


def _ff(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-240:] or "ffmpeg failed")


def probe_duration(path: Path) -> int:
    proc = subprocess.run(["ffmpeg", "-i", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+)", proc.stderr or "")
    if not m:
        return 0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def split_for_telegram(src: Path, dest_dir: Path) -> list[Path]:
    if src.stat().st_size <= MAX_BYTES:
        return [src]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dur = probe_duration(src) or 1
    n = max(2, (src.stat().st_size + MAX_BYTES - 1) // MAX_BYTES)
    seg = max(15, dur // n)
    pattern = str(dest_dir / f"{src.stem}_p%03d{src.suffix or '.mp4'}")
    try:
        _ff([
            "ffmpeg", "-y", "-i", str(src), "-c", "copy", "-map", "0",
            "-f", "segment", "-segment_time", str(seg), "-reset_timestamps", "1",
            pattern,
        ])
        parts = sorted(p for p in dest_dir.glob(f"{src.stem}_p*{src.suffix or '.mp4'}") if p.stat().st_size > 0)
        if parts and all(p.stat().st_size <= MAX_BYTES + 2_000_000 for p in parts):
            return parts
    except Exception as exc:
        log.warning("segment failed: %s", exc)
    return binary_split(src, dest_dir)


def binary_split(src: Path, dest_dir: Path) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    with src.open("rb") as fh:
        i = 1
        while True:
            chunk = fh.read(MAX_BYTES)
            if not chunk:
                break
            p = dest_dir / f"{src.stem}.part{i:02d}{src.suffix}"
            p.write_bytes(chunk)
            parts.append(p)
            i += 1
    return parts


def compress_audio(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{src.stem}.mp3"
    for br in ("192k", "160k", "128k", "96k", "64k"):
        _ff(["ffmpeg", "-y", "-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", br, str(out)])
        if out.exists() and out.stat().st_size <= MAX_BYTES:
            return out
    return out if out.exists() else src


def ask_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001F3AC Media", callback_data="ask|media"),
        InlineKeyboardButton("\U0001F3B5 MP3", callback_data="ask|mp3"),
    ]])


def settings_kb(current: str) -> InlineKeyboardMarkup:
    def mark(k: str) -> str:
        return ("• " if k == current else "") + PRESETS[k]["label"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(mark("best"), callback_data="set|best"), InlineKeyboardButton(mark("1080"), callback_data="set|1080")],
        [InlineKeyboardButton(mark("720"), callback_data="set|720"), InlineKeyboardButton(mark("480"), callback_data="set|480"), InlineKeyboardButton(mark("360"), callback_data="set|360")],
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
        "\U0001F680 Veltrix Downloader\n\n"
        "Link tashla → Media yoki MP3.\n"
        f"Default video: {PRESETS[key]['label']}\n"
        "50 MB dan katta fayl qismlarga bo\u2018linadi (maks 300 MB).\n"
        "/settings"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Free Telegram limiti 50 MB.\n"
        "Katta video 300 MB gacha yuklanadi va qismlarda yuboriladi."
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = user_row(update.effective_user.id)["quality"]
    await update.message.reply_text(
        f"\U0001F3A5 Default video: {PRESETS[key]['label']}",
        reply_markup=settings_kb(key),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    uid = update.effective_user.id
    url = extract_url(msg.text or msg.caption or "")
    if not url:
        await msg.reply_text("\U0001F517 Link yubor.")
        return
    patch_user(uid, last_url=url)
    await msg.reply_text("\U0001F449 Qaysi format?", reply_markup=ask_kb())


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id
    if data.startswith("set|"):
        key = data.split("|", 1)[1]
        if key not in VIDEO_KEYS:
            return
        patch_user(uid, quality=key)
        await q.edit_message_text(f"\u2705 Default video: {PRESETS[key]['label']}")
        return
    if data.startswith("ask|"):
        kind = data.split("|", 1)[1]
        url = user_row(uid).get("last_url") or ""
        if not url:
            await q.edit_message_text("\U0001F517 Avval link yubor.")
            return
        key = "mp3" if kind == "mp3" else user_row(uid)["quality"]
        try:
            await q.edit_message_text("\U0001F4E5 Downloading...")
        except TelegramError:
            pass
        await run_job(q.message, context, uid, url, key, status=q.message)


async def send_path(msg, path: Path, caption: str, kind: str) -> int:
    n = 0
    parts = [path] if path.stat().st_size <= MAX_BYTES else split_for_telegram(path, path.parent / f"{path.stem}_parts")
    total = len(parts)
    for i, part in enumerate(parts, 1):
        cap = caption if total == 1 else f"{caption}\n\U0001F4E6 {i}/{total}"
        with part.open("rb") as fh:
            if kind == "audio" and total == 1:
                await msg.reply_audio(audio=fh, caption=cap, filename=part.name)
            elif kind == "video" and total == 1:
                try:
                    await msg.reply_video(video=fh, caption=cap, filename=part.name, supports_streaming=True)
                except TelegramError:
                    fh.seek(0)
                    await msg.reply_document(document=fh, caption=cap, filename=part.name)
            else:
                await msg.reply_document(document=fh, caption=cap, filename=part.name)
        n += 1
    return n


async def run_job(msg, context, uid: int, url: str, key: str, status=None) -> None:
    lock = job_lock(uid)
    if lock.locked():
        await msg.reply_text("\u23F3 Hali oldingi fayl ketmoqda...")
        return
    spec = PRESETS[key]
    site = site_of(url) or "web"
    if status is None:
        status = await msg.reply_text("\U0001F4E5 Downloading...")

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
            if key == "mp3" and videos and not audios:
                out_dir = Path(tmpdir) / "out"
                audios = [await asyncio.to_thread(compress_audio, p, out_dir) for p in videos]
                videos = []
            sent = 0
            if images:
                sent += await send_images(msg, images)
            for path in videos:
                sent += await send_path(msg, path, f"\U0001F3AC {spec['label']} \u00b7 {path.stat().st_size / 1048576:.1f} MB", "video")
            for path in audios:
                if path.stat().st_size > MAX_BYTES:
                    path = await asyncio.to_thread(compress_audio, path, Path(tmpdir) / "out")
                sent += await send_path(msg, path, f"\U0001F3B5 MP3 \u00b7 {path.stat().st_size / 1048576:.1f} MB", "audio")
            if sent == 0:
                raise RuntimeError("nothing to send")
            try:
                await status.edit_text(f"\u2705 {sent} ta fayl yuborildi")
            except TelegramError:
                pass
        except Exception as exc:
            log.exception("job")
            try:
                await status.edit_text(f"\u274C {site}: {str(exc)[:180]}")
            except TelegramError:
                await msg.reply_text(f"\u274C {str(exc)[:180]}")
        finally:
            task.cancel()
            shutil.rmtree(tmpdir, ignore_errors=True)


async def send_images(msg, paths: list[Path]) -> int:
    sent = 0
    batch: list[Path] = []
    for img in paths:
        if img.stat().st_size > MAX_PHOTO:
            with img.open("rb") as fh:
                await msg.reply_document(document=fh, filename=img.name)
            sent += 1
            continue
        batch.append(img)
        if len(batch) == 10:
            sent += await album(msg, batch)
            batch = []
    if len(batch) == 1:
        with batch[0].open("rb") as fh:
            await msg.reply_photo(photo=fh)
        sent += 1
    elif batch:
        sent += await album(msg, batch)
    return sent


async def album(msg, paths: list[Path]) -> int:
    media, handles = [], []
    try:
        for p in paths:
            fh = p.open("rb")
            handles.append(fh)
            media.append(InputMediaPhoto(media=fh))
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
    log.info("started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
