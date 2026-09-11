#!/usr/bin/env python3
"""Render Web Service entry for Veltrix Downloader."""

from __future__ import annotations

import base64
import asyncio
import logging
import os
import shutil
import sys
import time
import traceback
from pathlib import Path


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg"):
        return
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


def materialize_youtube_cookies() -> Path | None:
    """Create a temporary Netscape cookies file from a Render secret.

    YOUTUBE_COOKIES_B64 is deliberately never logged. The decoded file lives
    only in /tmp and is permission-restricted. This allows yt-dlp to use a
    user-authorized YouTube session without committing credentials to GitHub.
    """
    raw = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    if not raw:
        return None
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise SystemExit(f"YOUTUBE_COOKIES_B64 is invalid base64: {exc}") from exc
    if not data or len(data) > 2 * 1024 * 1024:
        raise SystemExit("YOUTUBE_COOKIES_B64 is empty or unexpectedly large")
    path = Path("/tmp/veltrix-youtube-cookies.txt")
    path.write_bytes(data)
    path.chmod(0o600)
    return path


ensure_ffmpeg()
YOUTUBE_COOKIE_FILE = materialize_youtube_cookies()

import bot  # noqa: E402
from telegram import Bot  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("veltrix.render")


# Inject the optional YouTube cookie file into every yt-dlp session. It is
# harmless for non-YouTube extractors and keeps the core downloader portable.
if YOUTUBE_COOKIE_FILE:
    _base_ydl_opts = bot.base_ydl_opts

    def _base_ydl_opts_with_cookie(tmpdir: str):
        opts = _base_ydl_opts(tmpdir)
        opts["cookiefile"] = str(YOUTUBE_COOKIE_FILE)
        return opts

    bot.base_ydl_opts = _base_ydl_opts_with_cookie
    log.info("YouTube authenticated-session support enabled")


def main() -> None:
    token = (bot.BOT_TOKEN or os.getenv("BOT_TOKEN", "")).strip()
    if not token:
        log.error("BOT_TOKEN is missing. Render → Environment → Add BOT_TOKEN")
        raise SystemExit("BOT_TOKEN is missing")
    bot.BOT_TOKEN = token
    bot.DATA_DIR.mkdir(parents=True, exist_ok=True)

    # In the all-free setup, Termux is the Telegram worker. Render stays health-only
    # and must not recreate a webhook that would steal updates from Termux polling.
    if os.getenv("TERMUX_PRIMARY", "").strip() == "1":
        asyncio.run(Bot(token).delete_webhook(drop_pending_updates=False))
        bot.start_health_server()
        log.info("TERMUX_PRIMARY=1: Telegram disabled on Render; health-only mode")
        while True:
            time.sleep(3600)

    app = Application.builder().token(token).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("settings", bot.cmd_settings))
    app.add_handler(CallbackQueryHandler(bot.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))

    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    port = int(os.getenv("PORT", "10000"))
    if not hostname:
        log.info("No Render hostname; polling")
        app.run_polling(drop_pending_updates=True)
        return

    webhook_url = f"https://{hostname}/telegram"
    log.info("webhook :%s -> %s", port, webhook_url)
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path="telegram",
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise
