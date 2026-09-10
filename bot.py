#!/usr/bin/env python3
"""Veltrix Downloader — Telegram YouTube downloader bot."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

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
MAX_TG_BYTES = 49 * 1024 * 1024
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
        "label": "Best quality",
        "ydl": "bv*+ba/b",
        "kind": "video",
        "ext": "mp4",
    },
    "1080": {
        "label": "1080p MP4",
        "ydl": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b",
        "kind": "video",
        "ext": "mp4",
    },
    "720": {
        "label": "720p MP4",
        "ydl": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/bv*[height<=720]+ba/b",
        "kind": "video",
        "ext": "mp4",
    },
    "480": {
        "label": "480p MP4",
        "ydl": "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/bv*[height<=480]+ba/b",
        "kind": "video",
        "ext": "mp4",
    },
    "mp3": {
        "label": "Audio MP3",
        "ydl": "bestaudio/ba/b",
        "kind": "audio",
        "ext": "mp3",
    },
    "m4a": {
        "label": "Audio M4A",
        "ydl": "bestaudio[ext=m4a]/bestaudio/ba",
        "kind": "audio",
        "ext": "m4a",
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
        "ignoreerrors": False,
        "retries": 8,
        "fragment_retries": 8,
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10_485_760,
        "socket_timeout": 20,
        "outtmpl": str(Path(tmpdir) / "%(id)s.%(ext)s"),
        "restrictfilenames": True,
        "overwrites": True,
        "cachedir": False,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
        "source_address": "0.0.0.0",
    }


def probe(url: str) -> dict:
    opts = base_opts(tempfile.gettempdir())
    opts.update({"skip_download": True})
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise DownloadError("No video information returned")
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0] or info
    return info


def download_file(url: str, key: str, tmpdir: str) -> Path:
    spec = FORMATS[key]
    opts = base_opts(tmpdir)
    opts["format"] = spec["ydl"]
    opts["merge_output_format"] = "mp4" if spec["kind"] == "video" else spec["ext"]
    if spec["ext"] == "mp3":
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    elif spec["kind"] == "video":
        opts["postprocessors"] = [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
        ]

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))

    if spec["ext"] == "mp3":
        path = path.with_suffix(".mp3")
    if not path.exists():
        files = [p for p in Path(tmpdir).iterdir() if p.is_file()]
        if not files:
            raise FileNotFoundError("Download finished but file is missing")
        path = max(files, key=lambda p: p.stat().st_size)
    return path


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def keyboard(url: str) -> InlineKeyboardMarkup:
    rows = [
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
    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Veltrix Downloader\n\n"
        "Send any YouTube link (video or Shorts).\n"
        "Choose video or audio format. I will reply Downloading... then send the file.\n\n"
        "Commands: /start  /help"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Paste a youtube.com or youtu.be URL.\n"
        "Formats: Best, 1080p, 720p, 480p, MP3, M4A.\n"
        "Telegram file limit is about 50 MB for this bot API."
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
    duration = format_duration(info.get("duration"))
    context.user_data["url"] = info.get("webpage_url") or url
    context.user_data["title"] = title

    text = (
        f"<b>{title}</b>\n"
        f"Duration: {duration}\n\n"
        f"Choose format:"
    )
    await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard(url))


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
        size = path.stat().st_size
        if size > MAX_TG_BYTES:
            mb = size / (1024 * 1024)
            await query.edit_message_text(
                f"File is {mb:.1f} MB. Telegram Bot API limit is ~50 MB.\n"
                f"Pick 480p or audio, or use a shorter video."
            )
            return

        caption = f"{title}\n{spec['label']}\nVeltrix Downloader"
        with path.open("rb") as fh:
            if spec["kind"] == "audio":
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=fh,
                    caption=caption,
                    title=title[:64],
                    filename=path.name,
                )
            else:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=fh,
                    caption=caption,
                    filename=path.name,
                    supports_streaming=True,
                )
        await query.edit_message_text("Sent.")
    except DownloadError as exc:
        log.exception("download failed")
        await query.edit_message_text(f"Download failed.\n{exc}")
    except TelegramError as exc:
        log.exception("telegram send failed")
        await query.edit_message_text(f"Could not send file.\n{exc}")
    except Exception as exc:
        log.exception("job failed")
        await query.edit_message_text(f"Error.\n{exc}")
    finally:
        typer.cancel()
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Set it in .env or the environment.")
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found — MP3 convert and some merges will fail")

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_format))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Veltrix Downloader started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
