# Veltrix Downloader

Telegram bot that downloads YouTube videos and Shorts in video or audio format.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
# put BOT_TOKEN in .env
python bot.py
```

Requires `ffmpeg` on PATH for merge / MP3 convert.

## Usage

Send a YouTube link (video or Shorts). Pick a format. Bot replies `Downloading...` then sends the file.

Telegram Bot API limit without a local Bot API server is ~50 MB per file.
