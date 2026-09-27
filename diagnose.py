"""Read-only runtime check. Never prints credentials or consumes bot updates."""
import asyncio
import importlib.metadata
import os
import shutil
import socket
import subprocess

import bot


async def main():
    print(f"Veltrix {bot.VERSION} runtime check")
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
    for host in ("api.telegram.org", "www.youtube.com", "www.instagram.com", "www.snapchat.com", "www.pinterest.com"):
        try:
            await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM), 10)
            print(f"DNS {host}: OK")
        except (OSError, TimeoutError) as exc:
            print(f"DNS {host}: {type(exc).__name__}")
    print(f"Telegram IPv4 option: {os.getenv('TELEGRAM_IPV4', '0')}")
    if not bot.BOT_TOKEN:
        print("BOT_TOKEN: MISSING")
        return
    try:
        client = bot.application_builder(bot.BOT_TOKEN).build().bot
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
