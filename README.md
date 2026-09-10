# Veltrix Downloader

Telegram media downloader for **YouTube, Instagram, Snapchat and Pinterest**.

## Download modes

- Video: Best, 4K, 1440p, 1080p, 720p, 480p, 360p
- Audio: MP3 320 / 192 / 128, M4A
- Original media: public photos, videos and supported carousels
- Files that exceed the hosted Telegram Bot API upload size are compressed or split into playable video parts when possible

## Backend

The backend uses a layered public-media pipeline:

1. `yt-dlp[default]` for video/audio extraction
2. Deno + `yt-dlp-ejs` for current YouTube JavaScript challenges and broader format availability
3. `gallery-dl` fallback for supported Instagram/Pinterest media and carousels
4. public Open Graph media fallback for share pages, including platforms without a dedicated extractor
5. `ffmpeg` for merge, audio conversion, quality conversion and Telegram-size fitting

Only **public media links** are intended to be handled. The supported host allowlist is limited to the four branded platforms.

## Render — free mode

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Rahmowin-1st/Veltrix-Downloader)

`render.yaml` creates one **Free Web Service** in Frankfurt. `render_free.py` runs Telegram in webhook mode so incoming Telegram requests can wake a sleeping free Render service.

Required environment variable:

```text
BOT_TOKEN=...
```

Optional operational variables:

```text
PROXY=...                 # only if you already operate an outbound proxy
MAX_CONCURRENT_JOBS=1    # keep 1 on the free Render instance
MAX_SOURCE_BYTES=314572800
MAX_GALLERY_ITEMS=20
```

Never commit secrets or tokens.

### Free-tier limitation

Render Free Web Services can spin down when idle. The first Telegram request after inactivity can be delayed by a cold start. Media platforms can also independently rate-limit or reject data-center IP addresses; the backend reports those failures rather than pretending the download succeeded.

## Local run

```bash
pip install -r requirements.txt
export BOT_TOKEN=...
python bot.py
```

## Supabase

The existing job-log table is `public.veltrix_downloader_jobs` in the Veltrix Ultron project.
