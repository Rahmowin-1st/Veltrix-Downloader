#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

echo "[1/4] Installing Termux packages..."
pkg update -y
pkg install -y python git ffmpeg nodejs deno

echo "[2/4] Installing Python dependencies..."
python -m pip install --upgrade pip
python -m pip install -r requirements-termux.txt

echo "[3/4] Configuring BOT_TOKEN locally..."
if [ ! -f .env ] || ! grep -q '^BOT_TOKEN=' .env; then
  printf 'Paste BOT_TOKEN here (hidden): '
  IFS= read -r -s TOKEN
  printf '\n'
  if [ -z "$TOKEN" ]; then
    echo "BOT_TOKEN cannot be empty"
    exit 1
  fi
  printf 'BOT_TOKEN=%s\nMAX_CONCURRENT_JOBS=1\n' "$TOKEN" > .env
  chmod 600 .env
fi

echo "[4/4] Starting Veltrix Downloader..."
termux-wake-lock || true
bash ./termux_start.sh

echo "Done. Test /start in Telegram."
