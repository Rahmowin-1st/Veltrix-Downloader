# Veltrix Downloader

Telegram downloader for **YouTube, Instagram, Snapchat and Pinterest**.

## User flow

1. Send a supported public media link.
2. Veltrix resolves it automatically — no quality menu or extra confirmation.
3. Video quality preference is **720p → 1080p → lower fallbacks**.
4. Photos are sent as photos, videos as videos, GIF-like media as animations, audio as audio.
5. Every delivered video gets an **MP3** inline action bound to that exact media item.

## Extraction stack

Veltrix uses platform-specific engines first, then conservative fallbacks:

- **YouTube:** yt-dlp + Deno/yt-dlp-ejs + ffmpeg.
- **Instagram:** parth-dl for public Reels/posts/mixed carousels, then yt-dlp and gallery-dl fallbacks.
- **Snapchat:** exact Spotlight-ID matching against public `__NEXT_DATA__`, then CDN download with redirect validation.
- **Pinterest:** pinterest-downloader for authoritative media type/quality, plus gallery-dl traversal for multi-page Idea Pins.

Generic public-page/Open Graph extraction is only a last fallback. Known video links never degrade into poster images.

## Reliability

- per-user job serialization + global concurrency limit
- Telegram retry/backoff for transient network and flood-control errors
- automatic H.264/AAC normalization when Telegram rejects a source container
- ffmpeg size fitting / splitting for hosted Bot API limits
- media-type validation, HTML/error-payload rejection and duplicate suppression
- temporary-file cleanup and bounded exact-media cache for MP3 actions
- stale cache cleanup, health diagnostics and Termux crash supervisor
- SSRF/private-address protection and redirect revalidation
- Python 3.12 + 3.13 CI gates, dependency integrity, compile, shell syntax and unit tests

Only media the platform exposes publicly is intended to be downloaded. Private/login-only content is not bypassed.

## $0 primary runtime — Termux

Initial setup:

```bash
git clone https://github.com/Rahmowin-1st/Veltrix-Downloader.git
cd Veltrix-Downloader
bash termux_setup.sh
```

Normal update/restart:

```bash
cd ~/Veltrix-Downloader
git pull
bash termux_start.sh
```

`termux_start.sh` performs dependency/runtime preflight before replacing the running worker. The supervisor restarts the bot after crashes with bounded exponential backoff.

Keep Android battery optimization disabled for Termux if you want reliable 24/7 operation.

## Render

Render remains a **health-only fallback** in the all-free architecture when:

```text
TERMUX_PRIMARY=1
```

It must not compete with Termux for Telegram updates.

Required secret:

```text
BOT_TOKEN=...
```

Never commit tokens, cookies, proxies, or account sessions.
