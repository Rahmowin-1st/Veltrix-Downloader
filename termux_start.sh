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

if [ -z "${BOT_TOKEN:-}" ]; then
  echo "BOT_TOKEN missing in .env"
  exit 1
fi

termux-wake-lock || true

# Polling and webhook cannot be active at the same time. Remove the Render webhook
# without printing the token.
python - <<'PY'
import asyncio, os
from telegram import Bot
async def main():
    bot = Bot(os.environ['BOT_TOKEN'])
    await bot.delete_webhook(drop_pending_updates=True)
asyncio.run(main())
PY

if [ -f .veltrix.pid ]; then
  OLD_PID="$(cat .veltrix.pid 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" || true
    sleep 1
  fi
fi

mkdir -p logs
nohup python bot.py >> logs/termux.log 2>&1 &
echo $! > .veltrix.pid
sleep 2

PID="$(cat .veltrix.pid)"
if kill -0 "$PID" 2>/dev/null; then
  echo "Veltrix Downloader running. PID=$PID"
  echo "Log: tail -f logs/termux.log"
else
  echo "Bot failed to start."
  tail -n 30 logs/termux.log || true
  exit 1
fi
