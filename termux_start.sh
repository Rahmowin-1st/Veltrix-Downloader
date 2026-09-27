#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo ".env not found. Run: bash termux_setup.sh"
  exit 1
fi

set -a
. ./.env
set +a

# Use IPv4 by default on Termux; explicit TELEGRAM_IPV4=0 is still respected.
# A successful curl -4 alone does not diagnose the cause of a Python timeout.
export TELEGRAM_IPV4="${TELEGRAM_IPV4:-1}"

if [ -z "${BOT_TOKEN:-}" ]; then
  echo "BOT_TOKEN missing in .env"
  exit 1
fi

echo "Veltrix preflight..."
if ! python - <<'PY'
import importlib
from pathlib import Path
import hashlib
stamp = Path('.dependencies.sha256')
wanted = hashlib.sha256(Path('requirements-termux.txt').read_bytes()).hexdigest()
if not stamp.exists() or stamp.read_text().strip() != wanted:
    raise SystemExit(1)
for name in ("telegram", "yt_dlp", "gallery_dl", "parth_dl", "httpx"):
    importlib.import_module(name)
PY
then
  echo "Dependencies changed; syncing Termux Python packages..."
  python -m pip install -U --upgrade-strategy only-if-needed -r requirements-termux.txt
  python -c "import hashlib,pathlib; pathlib.Path('.dependencies.sha256').write_text(hashlib.sha256(pathlib.Path('requirements-termux.txt').read_bytes()).hexdigest())"
fi

python -m compileall -q bot.py media_io.py source_metadata.py download_worker.py
for bin in ffmpeg ffprobe deno; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "Missing required runtime: $bin"
    echo "Run: bash termux_setup.sh"
    exit 1
  fi
done

termux-wake-lock || true

# Polling and webhook cannot be active at the same time. Keep pending updates so
# restarts do not silently lose links users sent while the worker was offline.
python - <<'PY'
import asyncio, os
from telegram.error import NetworkError, TimedOut
async def main():
    from bot import application_builder
    for attempt in range(1, 4):
        try:
            bot = application_builder(os.environ['BOT_TOKEN']).build().bot
            # Bound the whole attempt, including HTTP transport retries.
            async with asyncio.timeout(45):
                async with bot:
                    me = await bot.get_me()
                    print(f'Telegram identity verified: @{me.username}')
                    await bot.delete_webhook(drop_pending_updates=False)
            return
        except (TimeoutError, TimedOut, NetworkError):
            if attempt == 3:
                raise SystemExit(
                    'Telegram API unreachable after 3 bounded attempts. '
                    'Run python diagnose.py, check the phone network, then retry bash termux_start.sh.'
                )
            print(f'Telegram connection attempt {attempt}/3 failed; retrying...')
            await asyncio.sleep(attempt * 2)
asyncio.run(main())
PY

if [ -f .veltrix.pid ]; then
  OLD_PID="$(cat .veltrix.pid 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$OLD_PID" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$OLD_PID" 2>/dev/null; then
      echo "The previous worker is still finishing its shutdown. No second worker was started."
      exit 1
    fi
  fi
fi

mkdir -p logs

# Rotate local logs before they become huge on the phone.
if [ -f logs/termux.log ] && [ "$(wc -c < logs/termux.log)" -gt 5242880 ]; then
  [ -f logs/termux.log.2 ] && mv -f logs/termux.log.2 logs/termux.log.3 || true
  [ -f logs/termux.log.1 ] && mv -f logs/termux.log.1 logs/termux.log.2 || true
  mv -f logs/termux.log logs/termux.log.1
fi

nohup bash ./termux_supervisor.sh >> logs/termux.log 2>&1 &
echo $! > .veltrix.pid
sleep 3

PID="$(cat .veltrix.pid)"
if kill -0 "$PID" 2>/dev/null; then
  echo "Veltrix supervisor started. PID=$PID (media delivery still needs a live test)"
  echo "Log: tail -f logs/termux.log"
else
  echo "Bot failed to start."
  tail -n 60 logs/termux.log || true
  exit 1
fi
