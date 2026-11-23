# Veltrix Downloader

A Telegram bot that downloads public media from **YouTube, Instagram, Snapchat and Pinterest**. It uses the best exposed quality, keeps carousel items in order and extracts audio when the source makes a track available.

[Open the Telegram bot](https://t.me/VeltrixDownloaderBot) · [Explore the project map](docs/PROJECT_MAP.md) · [Read the Claude continuation notes](CLAUDE.md)

## Try it

| Send to the bot | What you get |
| --- | --- |
| A public link or `/original <link>` | Source-quality files as Telegram documents. Large files may arrive as numbered parts |
| `/playable <link>` | Media prepared for playback inside Telegram |
| **MP3** button | Audio from the delivered post when exposed by the platform |
| `/status` | The state of your recent requests |

## Supported media

| Source | Common supported inputs |
| --- | --- |
| YouTube | Videos, Shorts, archived streams, explicit playlists and exposed Community attachments |
| Instagram | Public Reels, posts, share links and mixed photo/video carousels |
| Snapchat | Public Spotlight, Story and Highlight media |
| Pinterest | Images, GIFs, videos and mixed Pin collections |

Quality and availability depend on the source, the configured providers and file size. Private, expired or removed links may be unavailable. YouTube's provider can select a lower quality when a higher one is unavailable or exceeds its quota.

## Where it runs

**Render** runs the production bot as a Python web service in Frankfurt. Telegram delivers updates to the webhook and Render downloads and uploads the media, including when your phone is off. The Free plan can cold-start after inactivity, and its local state is temporary.

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
