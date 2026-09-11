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

# Polling and webhook cannot be active at the same time. Keep pending updates so
# restarts do not silently lose links users sent while the worker was offline.
python - <<'PY'
import asyncio, os
from telegram import Bot
async def main():
    bot = Bot(os.environ['BOT_TOKEN'])
    await bot.delete_webhook(drop_pending_updates=False)
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
  echo "Veltrix Downloader running. Supervisor PID=$PID"
  echo "Log: tail -f logs/termux.log"
else
  echo "Bot failed to start."
  tail -n 60 logs/termux.log || true
  exit 1
fi
