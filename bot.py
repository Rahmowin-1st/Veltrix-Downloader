#!/usr/bin/env python3
"""Veltrix Downloader — Telegram YouTube bot. Caps every send at 50 MB."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
MAX_BYTES = 48 * 1024 * 1024  # stay under official Bot API 50 MB
YT_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.|m\.)?(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/\S+"
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("veltrix")

FORMATS = {
    "best": {
        "label": "Best under 50 MB",
        "ydl": (
            "bv*[height<=1080][filesize<48M]+ba[filesize<10M]/"
            "bv*[height<=1080][filesize_approx<48M]+ba/"
            "bv*[height<=1080]+ba/b"
        ),
        "kind": "video",
        "height": 1080,
    },
    "1080": {
        "label": "1080p (compressed if needed)",
        "ydl": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "kind": "video",
        "height": 1080,
    },
    "720": {
        "label": "720p",
        "ydl": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]/b",
        "kind": "video",
        "height": 720,
    },
    "480": {
        "label": "480p",
        "ydl": "bv*[height<=480][ext=mp4]+ba[ext=m4a]/bv*[height<=480]+ba/b[height<=480]/b",
        "kind": "video",
        "height": 480,
    },
    "mp3": {
        "label": "Audio MP3",
        "ydl": "bestaudio/ba/b",
        "kind": "audio",
        "height": 0,
    },
    "m4a": {
        "label": "Audio M4A",
        "ydl": "bestaudio[ext=m4a]/bestaudio/ba",
        "kind": "audio",
        "height": 0,
    },
}


def extract_url(text: str) -> str | None:
    if not text:
        return None
    m = YT_RE.search(text.strip())
    return m.group(0) if m else None


def base_opts(tmpdir: str) -> dict:
    return {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10_485_760,
        "socket_timeout": 20,
        "outtmpl": str(Path(tmpdir) / "%(id)s.%(ext)s"),
        "restrictfilenames": True,
        "overwrites": True,
        "cachedir": False,
        "merge_output_format": "mp4",
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }


def probe(url: str) -> dict:
    opts = base_opts(tempfile.gettempdir())
    opts["skip_download"] = True
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise DownloadError("No video information returned")
    if info.get("_type") == "playlist" and info.get("entries"):
        first = next((e for e in info["entries"] if e), None)
        if first:
            info = first
    return info


def download_file(url: str, key: str, tmpdir: str) -> Path:
    spec = FORMATS[key]
    opts = base_opts(tmpdir)
    opts["format"] = spec["ydl"]
    if spec["kind"] == "audio":
        codec = "mp3" if key == "mp3" else "m4a"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": "192"}
        ]
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    if key == "mp3":
        path = path.with_suffix(".mp3")
    elif key == "m4a":
        cand = path.with_suffix(".m4a")
        path = cand if cand.exists() else path
    if not path.exists():
        files = [p for p in Path(tmpdir).iterdir() if p.is_file()]
        if not files:
            raise FileNotFoundError("Download finished but file is missing")
        path = max(files, key=lambda p: p.stat().st_size)
    return path


def _run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-400:] or "ffmpeg failed")


def compress_under_limit(src: Path, dest_dir: Path, duration: int, want_height: int, kind: str) -> Path:
    if src.stat().st_size <= MAX_BYTES:
        return src
    dest_dir.mkdir(parents=True, exist_ok=True)
    dur = max(int(duration or 0), 1)
    if kind == "audio":
        out = dest_dir / f"{src.stem}.tg.mp3"
        for br in ("160k", "128k", "96k", "64k"):
            _run_ffmpeg(["ffmpeg", "-y", "-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", br, str(out)])
            if out.exists() and out.stat().st_size <= MAX_BYTES:
                return out
        return out

    total_bps = int((MAX_BYTES * 8) / dur)
    audio_bps = 96_000
    video_bps = max(total_bps - audio_bps, 120_000)
    height = want_height or 1080
    if video_bps < 1_200_000 and height > 720:
        height = 720
    if video_bps < 700_000 and height > 480:
        height = 480
    if video_bps < 350_000:
        height = 360

    out = dest_dir / f"{src.stem}.{height}p.mp4"
    for scale_h, vb in ((height, video_bps), (min(height, 720), int(video_bps * 0.75)), (480, int(video_bps * 0.55)), (360, 180_000)):
        _run_ffmpeg(
            [
                "ffmpeg", "-y", "-i", str(src),
                "-vf", f"scale=-2:{scale_h}",
                "-c:v", "libx264", "-preset", "veryfast",
                "-b:v", str(vb), "-maxrate", str(vb), "-bufsize", str(vb * 2),
                "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart", "-pix_fmt", "yuv420p",
                str(out),
            ]
        )
        if out.exists() and out.stat().st_size <= MAX_BYTES:
            return out
    return out


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Best", callback_data="d|best"),
                InlineKeyboardButton("1080p", callback_data="d|1080"),
                InlineKeyboardButton("720p", callback_data="d|720"),
            ],
            [
                InlineKeyboardButton("480p", callback_data="d|480"),
                InlineKeyboardButton("MP3", callback_data="d|mp3"),
                InlineKeyboardButton("M4A", callback_data="d|m4a"),
            ],
        ]
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Veltrix Downloader\n\n"
        "Send any YouTube link (video or Shorts).\n"
        "I reply Downloading... then send the file.\n\n"
        "Bot API hard limit is 50 MB. Larger videos are compressed. "
        "1080p is kept when it still fits."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Paste a youtube.com or youtu.be URL.\n"
        "Formats: Best, 1080p, 720p, 480p, MP3, M4A.\n"
        "Telegram Premium raises YOUR upload limit to 4 GB. "
        "Bots on the official API still cap at 50 MB. We compress to that cap."
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = extract_url(update.message.text or "")
    if not url:
        await update.message.reply_text("Send a valid YouTube URL.")
        return
    status = await update.message.reply_text("Looking up video...")
    try:
        info = await asyncio.to_thread(probe, url)
    except Exception as exc:
        log.exception("probe failed")
        await status.edit_text(f"Cannot read this video.\n{exc}")
        return
    title = (info.get("title") or "YouTube video")[:200]
    duration = info.get("duration") or 0
    context.user_data["url"] = info.get("webpage_url") or url
    context.user_data["title"] = title
    context.user_data["duration"] = duration
    text = (
        f"<b>{title}</b>\n"
        f"Duration: {format_duration(duration)}\n\n"
        f"Choose format. Files over 50 MB are compressed automatically."
    )
    await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard())


async def on_format(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("d|"):
        return
    key = data.split("|", 1)[1]
    if key not in FORMATS:
        await query.edit_message_text("Unknown format.")
        return
    url = context.user_data.get("url")
    title = context.user_data.get("title") or "file"
    duration = int(context.user_data.get("duration") or 0)
    if not url:
        await query.edit_message_text("Send the YouTube link again.")
        return
    spec = FORMATS[key]
    await query.edit_message_text(f"Downloading...\n{title}\n{spec['label']}")
    chat_id = query.message.chat_id

    async def keep_typing() -> None:
        try:
            while True:
                await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return

    typer = asyncio.create_task(keep_typing())
    tmpdir = tempfile.mkdtemp(prefix="veltrix_")
    try:
        path = await asyncio.to_thread(download_file, url, key, tmpdir)
        if path.stat().st_size > MAX_BYTES:
            await query.edit_message_text(f"Downloading... compressing to fit 50 MB\n{title}")
            path = await asyncio.to_thread(
                compress_under_limit, path, Path(tmpdir) / "out", duration, spec["height"], spec["kind"]
            )
        size = path.stat().st_size
        if size > MAX_BYTES:
            await query.edit_message_text(
                f"Still over 50 MB after compression ({size / 1048576:.1f} MB). Try 480p or MP3."
            )
            return
        caption = f"{title}\n{spec['label']} · {size / 1048576:.1f} MB\nVeltrix Downloader"
        with path.open("rb") as fh:
            if spec["kind"] == "audio":
                await context.bot.send_audio(
                    chat_id=chat_id, audio=fh, caption=caption, title=title[:64], filename=path.name
                )
            else:
                try:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=fh,
                        caption=caption,
                        filename=path.name,
                        supports_streaming=True,
                    )
                except TelegramError:
                    fh.seek(0)
                    await context.bot.send_document(
                        chat_id=chat_id, document=fh, caption=caption, filename=path.name
                    )
        await query.edit_message_text("Sent.")
    except DownloadError as exc:
        log.exception("download failed")
        await query.edit_message_text(f"Download failed.\n{exc}")
    except Exception as exc:
        log.exception("job failed")
        await query.edit_message_text(f"Error.\n{exc}")
    finally:
        typer.cancel()
        shutil.rmtree(tmpdir, ignore_errors=True)


def start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Veltrix Downloader OK")

        def log_message(self, fmt, *args):
            return

    Thread(target=lambda: ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever(), daemon=True).start()
    log.info("health server on :%s", port)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing")
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found")
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_format))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Veltrix Downloader started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
