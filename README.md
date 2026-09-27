# Veltrix Downloader 9.1

One public media link → automatic extraction → highest exposed quality → Telegram delivery.
No quality menu, paid downloader API or per-download subscription.

## Platform routes

| Platform | Implemented route | Media handled by that route |
| --- | --- | --- |
| YouTube | yt-dlp + yt-dlp-ejs + Deno + ffmpeg | Videos, Shorts, accessible long videos and audio streams |
| Instagram | parth-dl, then gallery-dl, then yt-dlp | Public Reels, posts, ordered mixed image/video carousels exposed by these extractors |
| Snapchat | Exact Spotlight ID / public story metadata; dedicated video preload; yt-dlp fallback | Spotlight videos; public story images/videos, selected story item or current public highlight |
| Pinterest | Exact pin metadata from gallery-dl or embedded page JSON + direct media/HLS | Images, GIFs, video, mixed carousels/Idea Pin pages and exposed audio blocks; no poster substitution |

This is an implementation support matrix, **not a claim that every current URL was tested live**.
Expired/deleted/private posts, unavailable music, DRM, unsupported page schemas and platform blocks can fail.
YouTube community image posts and all possible Snapchat share URL variants are not certified.

## Quality and Telegram behavior

- Automatic mode uses `bv*+ba/b` without a resolution ceiling. Instagram ranks available renditions by pixel area.
- “Highest” means the best stream the platform exposes to the extractor, not the creator's pre-upload master.
- Compatible H.264/AAC videos are remuxed to MP4 without re-encoding and sent as playable videos.
- Incompatible video codecs are converted to H.264/AAC for native video playback without reducing resolution. This compatibility conversion is lossy (CRF 18), not byte-identical to the original.
- Photos are always sent as photos, never documents. Telegram may recompress them; the old `PRESERVE_ORIGINALS` setting is no longer used. Oversize/unusual photos are fitted to photo limits.
- Ordered photo/video collections use albums of at most 10. Longer collections use multiple albums. Audio cannot share a photo/video album and is grouped separately in source order. Animations retain motion; within albums they use playable MP4.
- One post-level MP3 button remains on the status message because `sendMediaGroup` has no inline-keyboard parameter. It extracts every accessible audio-bearing source, including exposed Instagram soundtrack metadata. Cache misses re-extract the whole post, not just its first video.
- MP3 is encoded at 320 kbps; this cannot improve a lower-quality source or recover music that the platform does not expose. It extracts the mixed audio track, not isolated vocals/instruments.
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
python -m compileall -q bot.py media_io.py source_metadata.py diagnose.py download_worker.py render_free.py tests
bash -n termux_start.sh termux_setup.sh termux_supervisor.sh
```

The regression suite includes real generated MP4/M4A/PNG/GIF media, native animation/video conversion, MP3 extraction, stream-copy splits, mixed albums, cache ownership, exact-pin metadata, partial Instagram rejection and async delivery failures. Source-page and Telegram delivery tests use controlled fixtures; passing them is not live-platform certification.

Direct requests to all four platforms timed out in the editing environment. No live Telegram bot token or access to the running Termux session was available here. Therefore this release still needs live URL-to-Telegram acceptance on the actual runtime. See `docs/RELEASE_AUDIT.md`.

## Runtime diagnosis

Run `python diagnose.py` on the phone for package/runtime checks, DNS and authenticated bot identity. It never consumes updates, changes the webhook or prints the token. It does not prove downloader success or exclude a second polling instance.

Startup prints the authenticated bot username so a wrong token/chat is visible. `TELEGRAM_IPV4=1` optionally selects IPv4 for both polling and sends; default is automatic addressing. `TELEGRAM_PROXY` or the existing `HTTPS_PROXY` is honored, not silently removed. A successful `curl -4` does not establish that IPv6 is broken.

Optional owner-supplied cookie files are `INSTAGRAM_COOKIE_FILE`, `YOUTUBE_COOKIE_FILE`, and `PINTEREST_COOKIE_FILE`. They do not guarantee access; do not share them in chat or use them to bypass restrictions. Worker failures now include a URL/token-redacted diagnostic in `logs/termux.log`.

## Upstream references reviewed

- [yt-dlp EJS requirements](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
- [yt-dlp YouTube token constraints](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
- [gallery-dl Pinterest extractor](https://github.com/mikf/gallery-dl/blob/master/gallery_dl/extractor/pinterest.py)
- [Cobalt Snapchat implementation](https://github.com/imputnet/cobalt/blob/main/api/src/processing/services/snapchat.js)
- [Telegram local Bot API limits](https://core.telegram.org/bots/api#using-a-local-bot-api-server)

Cobalt was studied for behavior and public schema structure. No Cobalt service or paid endpoint is required.
