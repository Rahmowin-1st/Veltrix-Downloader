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
from telegram_network import environment_proxy


async def probe_telegram_route(token: str, label: str, local_address: str | None,
                               proxy: str | None = None) -> str:
    """Exercise an authenticated, read-only API method through one address route."""
    try:
        transport = (httpx.AsyncHTTPTransport(proxy=proxy, retries=0) if proxy else
                     httpx.AsyncHTTPTransport(local_address=local_address, retries=0))
        async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=httpx.Timeout(8.0)) as client:
            # Never include the authenticated URL or response body in output.
            response = await client.post(f"https://api.telegram.org/bot{token}/getMe")
            if response.status_code == 200:
                return f"{label}: OK (getMe HTTP 200)"
            if response.status_code == 401:
                return f"{label}: invalid bot token (HTTP 401)"
            return f"{label}: HTTP {response.status_code}"
    except (httpx.HTTPError, TimeoutError, OSError, ValueError) as exc:
        return f"{label}: {type(exc).__name__}"


def probe_curl_direct() -> str:
    """Compare with curl without putting the bot credential on a command line."""
    if not shutil.which('curl'):
        return 'curl direct IPv4 homepage: curl missing'
    try:
        result = subprocess.run(
            ['curl', '--noproxy', '*', '-4', '-I', '--silent', '--output', '/dev/null',
             '--write-out', '%{http_code}', '--connect-timeout', '7', '--max-time', '9',
             'https://api.telegram.org'], capture_output=True, text=True, timeout=11,
        )
        if result.returncode:
            return f'curl direct IPv4 homepage: curl exit {result.returncode}'
        return f'curl direct IPv4 homepage: HTTP {result.stdout.strip()[:3]}'
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f'curl direct IPv4 homepage: {type(exc).__name__}'


async def worker_health(label: str) -> None:
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=3) as local:
            response = await local.get(f"http://127.0.0.1:{os.getenv('PORT', '10000')}/readyz")
            info = response.json()
            state = info.get('telegram') or {}
            stage = ('connected' if response.status_code == 200 else
                     'initializing' if not state.get('last_api_ok') and not state.get('last_error') else 'disconnected')
            print(f"{label}: version={info.get('version')}, status={stage}, telegram={state}, route={info.get('telegram_route')}")
    except Exception as exc:
        print(f"{label} unavailable: {type(exc).__name__}")


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
    await worker_health('Local worker')
    configured_proxy = environment_proxy()
    print(f"Diagnostic IPv4 preference: {os.getenv('TELEGRAM_IPV4', '0')}; Telegram proxy configured: {bool(os.getenv('TELEGRAM_PROXY'))}; environment proxy available: {bool(configured_proxy)}; relay enabled: {bool(bot.TELEGRAM_RELAY_BASE)}")
    if not bot.BOT_TOKEN:
        print("BOT_TOKEN: MISSING")
        return
    if bot.TELEGRAM_RELAY_BASE:
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=9) as remote:
                response = await remote.get(bot.TELEGRAM_RELAY_BASE + '/healthz')
                print(f'Render relay HTTPS: HTTP {response.status_code}')
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            print(f'Render relay HTTPS: {type(exc).__name__}')
    elif bot.TELEGRAM_API_BASE:
        print("Direct Telegram route checks skipped: custom Bot API endpoint configured")
    elif os.getenv('TELEGRAM_PROXY'):
        print("Direct Telegram route checks skipped: explicit Telegram proxy configured")
    else:
        checks = [
            probe_telegram_route(bot.BOT_TOKEN, "Telegram automatic route", None),
            probe_telegram_route(bot.BOT_TOKEN, "Telegram IPv4 route", "0.0.0.0"),
            asyncio.to_thread(probe_curl_direct),
        ]
        if configured_proxy:
            checks.append(probe_telegram_route(bot.BOT_TOKEN, 'Telegram environment proxy', None,
                                               proxy=configured_proxy))
        routes = await asyncio.gather(*checks)
        for route in routes:
            print(route)
    try:
        client = bot.application_builder(bot.BOT_TOKEN).build().bot
        # The worker may need two 10s direct connects before an optional proxy.
        async with asyncio.timeout(40 if configured_proxy else 28):
            async with client:
                me = await client.get_me()
                print(f"Telegram identity: @{me.username}")
                webhook = await client.get_webhook_info()
                print(f"Webhook enabled: {bool(webhook.url)}; pending updates: {webhook.pending_update_count}")
    except Exception as exc:
        # Exception text may contain a token-bearing request URL.
        print(f"Telegram API: {type(exc).__name__}")
    await worker_health('Worker after probes')
    print("HTTP route checks use getMe; they do not consume updates or test media delivery.")


if __name__ == "__main__":
    asyncio.run(main())
