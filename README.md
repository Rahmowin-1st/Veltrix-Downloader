# Veltrix Downloader

A Telegram bot that downloads public media from **YouTube, Instagram, Snapchat and Pinterest**. Photos arrive as photos and videos as playable videos with their sound, in the best quality the source exposes. Carousels keep their order, videos up to three hours are supported and every delivery has an **MP3** button.

[Open the Telegram bot](https://t.me/VeltrixDownloaderBot) · [Explore the project map](docs/PROJECT_MAP.md) · [Read the Claude continuation notes](CLAUDE.md)

## Try it

| Send to the bot | What you get |
| --- | --- |
| A public link | Photos as Telegram photos, videos as playable H.264/AAC videos with sound, in source order. A live status shows progress, speed and time left |
| **MP3** button or `/mp3 <link>` | The audio as MP3: copied if it already is MP3, otherwise 320 kbps (long audio uses the highest bitrate that keeps few parts) |
| `/original <link>` | The untouched source files as documents, including the highest stream such as 4K |
| `/status` | The state of your recent requests |

## Supported media

| Source | Common supported inputs |
| --- | --- |
| YouTube | Videos, Shorts, archived streams, explicit playlists and exposed Community attachments |
| Instagram | Public Reels, posts, share links and mixed photo/video carousels |
| Snapchat | Public Spotlight, Story and Highlight media |
| Pinterest | Images, GIFs, videos and mixed Pin collections |

## Long videos

Videos up to `MAX_MEDIA_SECONDS` (4 hours by default) are accepted. The hosted Bot API takes at most 50 MB per file, so a long video arrives as numbered parts that each play on their own; parts are cut on keyframes as close to the limit as possible. For long sources the bot picks the highest rendition that still fits a reasonable number of parts, never below 360p.

Set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` (from [my.telegram.org](https://my.telegram.org), *API development tools*) to raise the limit to 2 GB per file: large files are then uploaded by the same bot over MTProto and sent by the normal Bot API request. If that route fails, the bot falls back to 50 MB parts automatically.

Quality and availability depend on the source, the configured providers and file size. Private, expired or removed links may be unavailable. YouTube's provider can select a lower quality when a higher one is unavailable, exceeds its quota or would need too many parts.

YouTube asks cloud servers to sign in. On Render, add the cookies of a secondary YouTube account as the Secret File `youtube_cookies.txt` ([step by step](docs/OPERATIONS.md#youtube-sign-in)); VidFetch quota remains the fallback. A nightly workflow keeps yt-dlp and gallery-dl on their newest stable releases.

## Where it runs

**Render** runs the production bot as a Python web service in Frankfurt. Telegram delivers updates to the webhook and Render downloads and uploads the media, including when your phone is off. The Free plan can cold-start after inactivity, and its local state is temporary. While a job runs, the service keeps itself awake; `KEEP_AWAKE=1` also avoids cold starts when idle.

| Location | Open |
| --- | --- |
| Production service | [Render dashboard](https://dashboard.render.com/web/srv-dah9pd1t0dsc73f6a20g) · [Service address](https://veltrix-downloader.onrender.com) |
| Cloud setup and Termux alternative | [Render setup](RENDER.md) · [Runtime and operations](docs/OPERATIONS.md) |
| Code, folders and runtime state | [Project map](docs/PROJECT_MAP.md#where-to-edit) |
| Latest verified release and test evidence | [Version and evidence](docs/PROJECT_MAP.md#version-and-evidence) |

## Continue development

Use Python **3.12 or 3.13** with `ffmpeg` and `ffprobe` on your PATH.

```bash
git clone https://github.com/Rahmowin-1st/Veltrix-Downloader.git
cd Veltrix-Downloader
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

For production diagnosis, start with the deployed commit and logs, then reproduce one public URL. Use the [configuration template](.env.example), [Claude continuation notes](CLAUDE.md) and [CI workflow](.github/workflows/backend-gates.yml).

## Project history

[Historical release audit](docs/RELEASE_AUDIT.md) · [Competitor review](docs/COMPETITOR_REVIEW.md)
