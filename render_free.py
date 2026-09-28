#!/usr/bin/env python3
"""Render Web Service entry for Veltrix Downloader."""

from __future__ import annotations

import base64
import asyncio
import hashlib
import logging
import os
import shutil
import sys
import threading
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


def probe_telegram_egress(token: str) -> None:
    """Read-only connectivity check; never logs the token or a request URL."""
    async def check():
        client = bot.application_builder(token).build().bot
        async with asyncio.timeout(36):
            async with client:
                await client.get_me()

    try:
        asyncio.run(check())
    except Exception as exc:
        log.warning("Render Telegram API egress: %s", type(exc).__name__)
    else:
        log.info("Render Telegram API egress: OK")


def probe_relay_loopback(token: str) -> None:
    """Verify the authenticated relay and Telegram together without polling."""
    import httpx

    async def check():
        for _ in range(8):
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
                    response = await client.post(
                        f"http://127.0.0.1:{os.getenv('PORT', '10000')}/relay/api/getMe",
                        headers={'X-Veltrix-Token': token},
                    )
                    return response.status_code == 200 and response.json().get('ok') is True
            except httpx.ConnectError:
                await asyncio.sleep(0.25)
        return False

    try:
        ready = asyncio.run(check())
    except Exception as exc:
        log.warning('Render relay loopback: %s', type(exc).__name__)
    else:
        log.info('Render relay loopback: %s', 'OK' if ready else 'unavailable')


def probe_webhook(token: str, expected: str) -> None:
    """Verify Telegram's registered webhook without reading any chat updates."""
    async def check():
        client = bot.application_builder(token).build().bot
        async with client:
            for _ in range(8):
                await asyncio.sleep(3)
                info = await client.get_webhook_info()
                if info.url == expected:
                    return True
        return False

    try:
        ready = asyncio.run(check())
    except Exception as exc:
        log.warning('Render webhook verification: %s', type(exc).__name__)
    else:
        log.info('Render webhook registered: %s', ready)


def probe_youtube_access() -> None:
    """Bounded metadata check for a public short previously sent by the owner."""
    from yt_dlp import YoutubeDL

    try:
        with YoutubeDL({'quiet': True, 'no_warnings': True, 'skip_download': True,
                        'socket_timeout': 10, 'retries': 0, 'extractor_retries': 0,
                        'noplaylist': True}) as ydl:
            info = ydl.extract_info('https://www.youtube.com/shorts/2Yhba6asmwg', download=False)
        log.info('Render YouTube metadata: %s', 'OK' if info and info.get('formats') else 'no formats')
    except Exception as exc:
        detail = str(exc).lower()
        reason = ('HTTP403' if '403' in detail else 'login-required' if 'sign in' in detail
                  else 'unavailable' if 'not available' in detail or '404' in detail
                  else type(exc).__name__)
        log.warning('Render YouTube metadata: %s', reason)


# Pass the local path to isolated workers; cookie values are never logged.
if YOUTUBE_COOKIE_FILE:
    os.environ["YOUTUBE_COOKIE_FILE"] = str(YOUTUBE_COOKIE_FILE)


def main() -> None:
    token = (bot.BOT_TOKEN or os.getenv("BOT_TOKEN", "")).strip()
    if not token:
        log.error("BOT_TOKEN is missing. Render → Environment → Add BOT_TOKEN")
        raise SystemExit("BOT_TOKEN is missing")
    bot.BOT_TOKEN = token
    bot.DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.info('Media tools: ffmpeg=%s ffprobe=%s deno=%s source_proxy=%s',
             bool(shutil.which('ffmpeg')), bool(shutil.which('ffprobe')),
             bool(shutil.which('deno')), bool(bot.PROXY))

    # Termux remains the only worker. This service is a credential-protected
    # transport bridge and never starts polling or sets a webhook.
    if os.getenv("TERMUX_PRIMARY", "").strip() == "1":
        if os.getenv('RELAY_ENABLED', '1') == '1':
            from telegram_relay import serve_relay
            threading.Thread(target=serve_relay, args=(token,), daemon=True).start()
            log.info('TERMUX_PRIMARY=1: Telegram relay enabled; no Render polling')
            threading.Thread(target=probe_relay_loopback, args=(token,), daemon=True).start()
        else:
            bot.start_health_server()
            log.info('TERMUX_PRIMARY=1: Telegram disabled on Render; health-only mode')
        threading.Thread(target=probe_telegram_egress, args=(token,), daemon=True).start()
        while True:
            time.sleep(3600)

    if not shutil.which('ffprobe'):
        raise SystemExit('ffprobe is required for video/photo validation on Render')
    app = bot.application_builder(token).build()
    bot.acquire_instance_lock()
    bot.register_handlers(app)

    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    port = int(os.getenv("PORT", "10000"))
    if not hostname:
        log.info("No Render hostname; polling")
        app.run_polling(drop_pending_updates=False)
        return

    webhook_url = f"https://{hostname}/telegram"
    log.info("webhook :%s -> %s", port, webhook_url)
    threading.Thread(target=probe_webhook, args=(token, webhook_url), daemon=True).start()
    threading.Thread(target=probe_youtube_access, daemon=True).start()
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path="telegram",
        webhook_url=webhook_url,
        drop_pending_updates=False,
        secret_token=hashlib.sha256(('veltrix-webhook:' + token).encode()).hexdigest(),
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise
