#!/usr/bin/env python3
"""Render Web Service entry for Veltrix Downloader."""

from __future__ import annotations

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


ensure_ffmpeg()
from source_auth import cookie_state, materialize_source_cookies  # noqa: E402
SOURCE_COOKIE_FILES = materialize_source_cookies()

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
log.info('Veltrix %s source access: youtube=%s instagram=%s pinterest=%s snapchat=%s proxy=%s cobalt=%s newisty=%s vidfetch=%s',
         # missing | signed-out | signed-in | present (cookie names only, never secret values)
         bot.VERSION, *(cookie_state(name) for name in ('YOUTUBE', 'INSTAGRAM', 'PINTEREST', 'SNAPCHAT')),
         bool(bot.PROXY), bool(os.getenv('COBALT_API_URL', '').strip()), bot.newisty_enabled(), bot.vidfetch_enabled())


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
                    return info
        return None

    try:
        info = asyncio.run(check())
    except Exception as exc:
        log.warning('Render webhook verification: %s', type(exc).__name__)
    else:
        if info is None:
            log.warning('Render webhook registered: False')
            return
        last_error = info.last_error_date
        recent_error = bool(last_error and time.time() - last_error.timestamp() < 3600)
        log.info('Render webhook registered: True pending=%s error_within_hour=%s',
                 info.pending_update_count, recent_error)


def keep_awake(url: str, *, sleep=time.sleep, clock=time.monotonic, rounds=None) -> None:
    """Render Free stops an instance after 15 minutes without inbound requests,
    which would cut a long download or upload. While a job is queued or running,
    request the service's own public address every four minutes. KEEP_AWAKE=1
    also keeps an idle bot warm so the first reply is immediate (this uses the
    plan's monthly instance hours)."""
    import httpx

    always = os.getenv('KEEP_AWAKE', '').strip() == '1'
    last = None
    while rounds is None or rounds > 0:
        if rounds is not None:
            rounds -= 1
        sleep(60)
        metrics = bot.metric_snapshot()
        busy = metrics.get('active_jobs', 0) + metrics.get('queued_jobs', 0) > 0
        interval = 240 if busy else 600
        if not (busy or always) or (last is not None and clock() - last < interval):
            continue
        try:
            with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
                client.get(url)
            last = clock()
        except httpx.HTTPError as exc:
            log.info('Keep-awake request: %s', type(exc).__name__)


# The isolated workers inherit cookie file paths; cookie values are never logged.


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
    threading.Thread(target=keep_awake, args=(f"https://{hostname}/",), daemon=True).start()
    log.info('Large uploads (MTProto, up to 2 GB): %s', 'configured' if bot.mtproto_configured() else 'off')
    if os.getenv('SOURCE_PROBE_UNTIL'):
        from source_diagnostics import run as probe_actual_sources
        threading.Thread(target=probe_actual_sources, daemon=True).start()
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
