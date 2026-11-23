# Veltrix Downloader on Render

The active service is `veltrix-downloader`. It uses a Python web service
and a Telegram webhook, as configured in [render.yaml](render.yaml).

1. Connect the GitHub repository to Render as a **Python Web Service**.
2. Set the build command to `pip install -r requirements.txt` and the start
   command to `python render_free.py`.
3. Set `BOT_TOKEN` in Render's secret environment and set `TERMUX_PRIMARY=0`.
   For public YouTube downloads after a server block, set `VIDFETCH_API_KEY` from a
   [VidFetch account](https://www.vidfetch.dev/). `NEWISTY_YOUTUBE=1` enables the secondary
   fallback. Keep provider keys in Render's secret environment.
4. Optional, for files up to 2 GB instead of 50 MB parts: create an app at
   [my.telegram.org](https://my.telegram.org) (*API development tools*) and set
   `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` as secrets.
   YouTube blocks cloud servers, so YouTube needs VidFetch quota or cookies:
   add a Secret File `youtube_cookies.txt` (Netscape format, exported from a
   secondary Google account in a private window) under Environment → Secret
   Files. VidFetch stays the fallback when the cookies expire. The startup log then shows
   `Large uploads (MTProto, up to 2 GB): configured`. `KEEP_AWAKE=1` keeps the free
   instance warm between requests; while a job runs it stays awake automatically.
5. In the startup logs, check the verified bot username and
   `Render webhook registered: True`. A growing pending count or a recent
   error means Telegram delivery to the webhook needs investigation.
6. Open [@VeltrixDownloaderBot](https://t.me/VeltrixDownloaderBot), send a
   public link and confirm that the media arrives. Tap **MP3** on an audible
   video to verify audio delivery too.

Render Free can sleep and its filesystem is temporary. Run a single webhook
or polling instance per token. See the [operations guide](docs/OPERATIONS.md#primary-runtime-render-webhook)
for the limits and the optional Termux setup.
