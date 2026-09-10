# Veltrix Downloader

Telegram YouTube downloader. Official Bot API upload cap is **50 MB** even if the user has Telegram Premium (Premium is 4 GB for people, not for bots). The bot compresses with ffmpeg and keeps 1080p when the bitrate still fits.

## Run locally

```bash
pip install -r requirements.txt
export BOT_TOKEN=...
python bot.py
```

Needs `ffmpeg` locally.

## Render — free mode

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Rahmowin-1st/Veltrix-Downloader)

The included `render.yaml` creates one **Free Web Service** in Frankfurt. `render_free.py` switches Telegram from polling to webhook mode so incoming Telegram requests can wake a sleeping free Render service. It also provides an ffmpeg binary through `imageio-ffmpeg` on Render's native Python runtime.

Set `BOT_TOKEN` in Render Environment when prompted. Never commit the token.

### Free-tier limitation

Render Free Web Services can spin down when idle, so the first bot response after inactivity can be delayed by a cold start. This mode is for free/testing use; an always-on paid Background Worker remains the stronger production option.

## Supabase

Job log table lives on project Veltrix Ultron: `public.veltrix_downloader_jobs`.
