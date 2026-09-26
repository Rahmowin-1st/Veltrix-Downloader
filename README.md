# Veltrix Downloader 9

One public media link → automatic extraction → highest exposed quality → Telegram delivery.
No quality menu, paid downloader API or per-download subscription.

## Platform routes

| Platform | Implemented route | Media handled by that route |
| --- | --- | --- |
| YouTube | yt-dlp + yt-dlp-ejs + Deno + ffmpeg | Videos, Shorts, accessible long videos and audio streams |
| Instagram | parth-dl, then gallery-dl, then yt-dlp | Public Reels, posts, ordered mixed image/video carousels exposed by these extractors |
| Snapchat | Exact Spotlight ID / public story metadata; dedicated video preload; yt-dlp fallback | Spotlight videos; public story images/videos, selected story item or current public highlight |
| Pinterest | gallery-dl ordered manifest + direct media/HLS | Pin images, GIFs, video, mixed Idea Pin pages, image carousels and exposed audio blocks |

This is an implementation support matrix, **not a claim that every current URL was tested live**.
Expired/deleted/private posts, unavailable music, DRM, unsupported page schemas and platform blocks can fail.
YouTube community image posts and all possible Snapchat share URL variants are not certified.

## Quality and Telegram behavior

- Automatic mode uses `bv*+ba/b` without a resolution ceiling. Instagram ranks available renditions by pixel area.
- “Highest” means the best stream the platform exposes to the extractor, not the creator's pre-upload master.
- Compatible H.264/AAC videos are remuxed to MP4 without re-encoding and sent as playable videos.
- Other codecs remain original files. The bot does not silently reduce resolution or bitrate.
- Original photos are sent as documents because Telegram's photo endpoint recompresses them. Set `PRESERVE_ORIGINALS=0` only if a compressed photo display is preferred.
- MP3 buttons refer to the exact item. On-demand MP3 is encoded at 320 kbps; this does not improve a lower-quality source.
- Files are streamed during upload rather than read entirely into RAM.
- Hosted Bot API: conservative 49,000,000-byte upload ceiling. Oversize audio/videos are split using stream copy, preserving quality. A single request may therefore produce multiple messages.
- An existing local Bot API server can be selected with `TELEGRAM_API_BASE=http://127.0.0.1:8081`; it supports uploads up to 2,000,000,000 bytes. A server must actually be installed/configured; setting the variable alone does not create it. Switching from Telegram's cloud server requires Telegram's documented `logOut` migration first.
- If even one keyframe-sized segment exceeds the upload limit, the bot reports the limit instead of silently damaging the original.

## Reliability

- Per-user serialization and bounded global work concurrency.
- Isolated download process; a 30-minute configurable deadline kills its process group, including downloader children.
- ffprobe validates downloaded files; HTML/JSON masquerading as media is rejected.
- Carousel order is retained; failed entries, extractor error records and item caps are reported instead of silently truncating a post.
- Snapchat will not substitute a recommended item when the requested Spotlight ID is missing.
- No generic page-wide download fallback: these can return thumbnails, recommendations or unrelated media.
- Telegram flood-control delays are honored in full. Ambiguous network/upload timeouts are not blindly retried because Telegram may already have accepted the file.
- Long uploads have dedicated timeouts; MP3 conversions share the same resource semaphore.
- Termux supervisor exits on shutdown signals. Startup refuses to launch a second supervisor while the previous one is still stopping.
- Dependency changes are detected during normal restart. Pending Telegram updates are preserved.

No program can guarantee zero interruption on sleeping Android/free hosting. In-flight jobs are not durably resumed after power loss. Telegram's Bot API has no idempotency key for uploads; exactly-once delivery across ambiguous network failures cannot be guaranteed.

## Free primary runtime: existing Termux phone

Update:

```bash
cd ~/Veltrix-Downloader
git pull --ff-only
bash termux_start.sh
```

Fresh installation:

```bash
git clone https://github.com/Rahmowin-1st/Veltrix-Downloader.git
cd Veltrix-Downloader
bash termux_setup.sh
```

Keep Termux battery optimization disabled. Reboot startup additionally requires Termux:Boot to be installed and opened once. Device storage, internet and power remain required; no paid API is used.

`.env.example` documents normal settings. Default caps: 100 items, 4 GiB per source, 6 GiB total download workspace, 128 MiB disk reserve, one concurrent job. Remuxing/splitting also needs additional free disk space.

`BOT_TOKEN` stays in `.env`. Never commit tokens, cookies, sessions or proxy credentials. Existing optional authorized YouTube cookie configuration on Render is passed to the isolated worker; age restrictions remain enforced.

Render with `TERMUX_PRIMARY=1` is health-only and must not poll alongside Termux.

## Validation

```bash
python -m unittest discover -s tests -v
python -m compileall -q bot.py media_io.py download_worker.py render_free.py tests
bash -n termux_start.sh termux_setup.sh termux_supervisor.sh
```

50 tests passed locally on Python 3.12, including generated real MP4/M4A/PNG/GIF media, stream-copy splits with audio/resolution/duration checks, remuxing, extraction manifests and async delivery failures. Source-page tests use controlled fixtures.

Direct requests to all four platforms timed out in the editing environment. No live Telegram bot token or access to the running Termux session was available here. Therefore this release still needs live URL-to-Telegram acceptance on the actual runtime. See `docs/RELEASE_AUDIT.md`.

## Upstream references reviewed

- [yt-dlp EJS requirements](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
- [yt-dlp YouTube token constraints](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
- [gallery-dl Pinterest extractor](https://github.com/mikf/gallery-dl/blob/master/gallery_dl/extractor/pinterest.py)
- [Cobalt Snapchat implementation](https://github.com/imputnet/cobalt/blob/main/api/src/processing/services/snapchat.js)
- [Telegram local Bot API limits](https://core.telegram.org/bots/api#using-a-local-bot-api-server)

Cobalt was studied for behavior and public schema structure. No Cobalt service or paid endpoint is required.
