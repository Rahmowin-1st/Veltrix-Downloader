#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo ".env not found. Run: bash termux_setup.sh"
  exit 1
fi

# An explicit per-command setting takes precedence over the saved .env value.
# This allows a one-run network comparison without editing a secret-bearing file.
START_IPV4_OVERRIDE="${TELEGRAM_IPV4-}"
set -a
. ./.env
set +a

# Use IPv4 by default on Termux; explicit TELEGRAM_IPV4=0 is still respected.
# A successful curl -4 alone does not diagnose the cause of a Python timeout.
export TELEGRAM_IPV4="${TELEGRAM_IPV4:-1}"
if [ -n "$START_IPV4_OVERRIDE" ]; then
  export TELEGRAM_IPV4="$START_IPV4_OVERRIDE"
fi

if [ -z "${BOT_TOKEN:-}" ]; then
  echo "BOT_TOKEN missing in .env"
  exit 1
fi

# This owner's Render service has a verified Telegram API route. The phone
# retains the worker, downloads and SQLite journal; Render only relays HTTPS.
# A deliberate TELEGRAM_RELAY_BASE=0 or a custom Bot API base opts out.
if [ -z "${TELEGRAM_RELAY_BASE+x}" ] && [ -z "${TELEGRAM_API_BASE:-}" ]; then
  export TELEGRAM_RELAY_BASE="https://veltrix-downloader.onrender.com"
  printf '\nTELEGRAM_RELAY_BASE=%s\n' "$TELEGRAM_RELAY_BASE" >> .env
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

python -m compileall -q bot.py media_io.py media_process.py runtime_jobs.py telegram_network.py transfer.py source_metadata.py download_worker.py diagnose.py
for bin in ffmpeg ffprobe deno; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "Missing required runtime: $bin"
    echo "Run: bash termux_setup.sh"
    exit 1
  fi
done

termux-wake-lock || true

# Authentication and webhook cleanup run inside the supervised worker.
# A temporary outage must not prevent an offline phone from starting recovery.
echo "Starting supervised Telegram connection; temporary outages will retry automatically."

if [ -f .veltrix.pid ]; then
  OLD_PID="$(cat .veltrix.pid 2>/dev/null || true)"
  if [ -n "$OLD_PID" ]; then
    . ./termux_process.sh
    veltrix_stop_previous "$OLD_PID"
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
