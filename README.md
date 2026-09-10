# Veltrix Downloader

Telegram YouTube downloader. Official Bot API upload cap is **50 MB** even if the user has Telegram Premium (Premium is 4 GB for people, not for bots). The bot compresses with ffmpeg and keeps 1080p when the bitrate still fits.

## Run locally

```bash
pip install -r requirements.txt
export BOT_TOKEN=...
python bot.py
```

Needs `ffmpeg`.

## Render

Connect this GitHub repo as a **Background Worker** (Docker). Set `BOT_TOKEN` in Render env. Do not commit the token.

## Supabase

Job log table lives on project Veltrix Ultron: `public.veltrix_downloader_jobs`.
