#!/usr/bin/env python3
"""Render Web Service entry for Veltrix Downloader."""

from __future__ import annotations

import logging
import os
import shutil
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


ensure_ffmpeg()

import bot  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("veltrix.render")


def main() -> None:
    if not bot.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing — set it in Render Environment")

    bot.DATA_DIR.mkdir(parents=True, exist_ok=True)
    app = Application.builder().token(bot.BOT_TOKEN).concurrent_updates(True).build()
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
    main()
