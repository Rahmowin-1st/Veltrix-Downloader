#!/usr/bin/env python3
"""Free Render Web Service entrypoint for Veltrix Downloader.

Uses Telegram webhooks so a sleeping free Render service can be woken by
incoming Telegram requests. Provides an ffmpeg binary via imageio-ffmpeg when
Render's native Python runtime does not ship one.
"""

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

log = logging.getLogger("veltrix.render")


def main() -> None:
    if not bot.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing")

    app = Application.builder().token(bot.BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CallbackQueryHandler(bot.on_format))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))

    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if not hostname:
        log.info("No Render hostname detected; falling back to polling")
        app.run_polling(drop_pending_updates=True)
        return

    port = int(os.getenv("PORT", "10000"))
    webhook_path = "telegram"
    webhook_url = f"https://{hostname}/{webhook_path}"
    log.info("Starting webhook server on :%s -> %s", port, webhook_url)
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
