"""Read-only runtime check. Never prints credentials or consumes bot updates."""
import asyncio
import importlib.metadata
import os
import shutil
import socket
import subprocess
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

import httpx

import bot


async def main():
    print(f"Veltrix {bot.VERSION} runtime check")
    print(f"Free temp storage: {shutil.disk_usage('/data/data/com.termux/files/usr/tmp' if Path('/data/data/com.termux/files/usr/tmp').exists() else '/tmp').free // (1024 * 1024)} MiB")
    for package in ("python-telegram-bot", "yt-dlp", "yt-dlp-ejs", "gallery-dl", "parth-dl"):
        try:
            print(f"{package}: {importlib.metadata.version(package)}")
        except importlib.metadata.PackageNotFoundError:
            print(f"{package}: MISSING")
    for name in ("ffmpeg", "ffprobe", "deno"):
        path = shutil.which(name)
        print(f"{name}: {'OK' if path else 'MISSING'}")
        if path and name == "deno":
            result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
            print(result.stdout.splitlines()[0] if result.stdout else "deno: execution failed")
    async def dns(host):
        try:
            await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM), 10)
            print(f"DNS {host}: OK")
        except (OSError, TimeoutError) as exc:
            print(f"DNS {host}: {type(exc).__name__}")
    await asyncio.gather(*(dns(host) for host in ("api.telegram.org", "www.youtube.com", "www.instagram.com", "www.snapchat.com", "www.pinterest.com")))
    journal = bot.DATA_DIR / 'jobs.sqlite3'
    if journal.exists():
        try:
            with closing(sqlite3.connect(journal.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                counts = dict(db.execute('SELECT state, COUNT(*) FROM jobs GROUP BY state').fetchall())
                print(f"Saved jobs: {counts}")
        except sqlite3.Error as exc:
            print(f"Saved jobs: {type(exc).__name__}")
    log = Path('logs/termux.log')
    if log.exists():
        with log.open('rb') as stream:
            stream.seek(max(0, log.stat().st_size - 32768))
            tail = stream.read().decode('utf-8', errors='replace')
        # Only categories are printed, never raw request URLs/credentials.
        counts = Counter(re.findall(r'\b(?:ConnectError|ConnectTimeout|ReadTimeout|TimedOut|Conflict|InvalidToken|WorkerFailure|PollingConflict)\b', tail))
        print(f"Recent log error counts (may include older runs): {dict(counts)}")
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=3) as local:
            response = await local.get(f"http://127.0.0.1:{os.getenv('PORT', '10000')}/readyz")
            info = response.json()
            print(f"Local worker: version={info.get('version')}, ready={response.status_code == 200}, telegram={info.get('telegram')}")
    except Exception as exc:
        print(f"Local worker health unavailable: {type(exc).__name__}")
    print(f"Telegram IPv4 option: {os.getenv('TELEGRAM_IPV4', '0')}")
    if not bot.BOT_TOKEN:
        print("BOT_TOKEN: MISSING")
        return
    try:
        client = bot.application_builder(bot.BOT_TOKEN).build().bot
        async with asyncio.timeout(30):
            async with client:
                me = await client.get_me()
                webhook = await client.get_webhook_info()
                print(f"Telegram identity: @{me.username}")
                print(f"Webhook enabled: {bool(webhook.url)}; pending updates: {webhook.pending_update_count}")
    except Exception as exc:
        # Exception text may contain a token-bearing request URL.
        print(f"Telegram API: {type(exc).__name__}")
    print("This checks connectivity, not live media downloads or polling conflicts.")


if __name__ == "__main__":
    asyncio.run(main())
