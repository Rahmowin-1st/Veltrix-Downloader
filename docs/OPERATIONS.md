# Runtime and operations

Use the [project map](PROJECT_MAP.md) to find the code, active service and verification results. The [Render setup](../RENDER.md) lists the deployment commands.

## Extraction and fallback behavior

- YouTube tries yt-dlp first. For eligible single public videos, configured providers can help with a bot check, missing player response or media HTTP 403. The anonymous public player fallback tries the public watch page for its initial context. It waits five minutes across worker processes after HTTP 403 from both public API origins.
- Instagram checks exact post metadata and verifies carousel length and type order. When the highest video rendition lacks audio, the worker can copy audio from a compatible rendition of the same item. Separate post-wide audio stays associated with that post.
- Snapchat looks for the requested Spotlight ID on the direct and embed pages, then tries its exact-ID public lookup. Pinterest reads the exact Pin Resource and falls back to gallery-dl or embedded page data. A public page can explicitly report unavailable media.

## Optional extraction APIs

Set `VIDFETCH_API_KEY` in Render Environment to use [VidFetch](https://www.vidfetch.dev/youtube-downloader-api)
when direct extraction for a single public YouTube video hits the bot check, a missing player response or media HTTP 403.
VidFetch bills delivered gigabytes (the Free plan includes 1 GB per month). It receives only the
canonical video URL and requested format. A plain link requests the best H.264 MP4 up to
`NATIVE_MAX_HEIGHT` (1080p), which Telegram plays inline; `/original` requests up to 2160p. The free
`/info` call reports per-quality sizes, so a long video starts at the tallest tier that fits the
delivery budget (never below 360p), which also saves quota. Jobs for long videos may wait up to 40
minutes on the provider. The worker validates the file and passes it to the usual Telegram delivery pipeline. VidFetch may
convert the source and serves direct MP3 requests at 128 kbps. The inline MP3 button uses the downloaded
video's audio when available. Provider secrets are sent only to its API. The worker requests deletion of
its temporary provider job after the transfer attempt. If VidFetch rejects job creation for insufficient
quota, the worker tries progressively smaller video formats. Playlists stay with the direct extractor.

Set `NEWISTY_YOUTUBE=1` to enable the [Newisty developer API](https://newisty.com/pages/developers#video-downloader)
for individual public YouTube videos when direct extraction hits YouTube's bot check.
The service receives the canonical video URL and requested format. Its queue and
rate limits apply. The worker verifies the video ID in metadata and checks the
download's type and duration. `/original` returns the provider's best MP4 when this
route is used. Leave the setting at `0` to use only the other configured extractors.

An owner-operated or explicitly authorized [Cobalt API](https://github.com/imputnet/cobalt/blob/main/docs/api.md)
can be used after built-in extractors fail or return silent videos:

```dotenv
COBALT_API_URL=https://your-authorized-cobalt-host.example/
COBALT_API_KEY=
```

Leave these empty to disable it. Cobalt does not provide an official unrestricted
public API endpoint; this repository does not include an instance or API key.
Self-hosting on the same blocked IP does not guarantee YouTube access. A provider
must actually expose the requested post and audio; integration alone cannot
establish live success.

The adapter requests maximum quality with sound, downloads all picker items in
order, keeps background audio for MP3, and checks real media streams. API keys
are sent only to the configured HTTPS endpoint, never to media/CDN hosts.
Explicit YouTube playlists stay on the collection-capable built-in route.

## Platform routes

| Platform | Implemented route | Media handled by that route |
| --- | --- | --- |
| YouTube | yt-dlp + yt-dlp-ejs + Deno + ffmpeg, plus matching Community post metadata | Videos, Shorts, clips, archived streams and explicit playlists. Community post image attachments and linked video when exposed |
| Instagram | parth-dl, gallery-dl and yt-dlp | Public Reels, posts, shared links, and ordered mixed image/video carousels |
| Snapchat | Exact Spotlight, Story and Highlight metadata, with CDN and HLS download | Spotlight videos and public story or highlight photos/videos, including a specific snap selected by the link |
| Pinterest | Exact Pin Resource, gallery-dl or embedded page JSON + direct media/HLS | Images, GIFs, video, mixed carousels/Idea Pin pages and exposed audio blocks; no poster substitution |

This is an implementation support matrix, **not a claim that every current URL was tested live**.
Expired/deleted/private posts, unavailable music, DRM, unsupported page schemas and platform blocks can fail.
Text-only posts have no media attachment. Public community posts and short links depend on the payload returned by the platform.

## Quality and Telegram behavior

- A plain link delivers **native media**: photos with `sendPhoto`, videos with `sendVideo` as fast-start H.264/AAC MP4 with width, height, duration and a thumbnail, audio with `sendAudio`. Compatible tracks are remuxed without re-encoding. Other codecs are converted to H.264 only when the estimated encode time fits `MAX_ENCODE_SECONDS` on the container's CPU share (Render Free has 0.1 CPU); longer HEVC/VP9/AV1 sources are copied into MP4 and still sent as video. If Telegram rejects a native format, that batch is resent as documents.
- YouTube in the default mode sorts by `vcodec:h264, res, fps, acodec:m4a`, so the delivered stream plays inline (usually up to 1080p). `/original` sorts by `res, fps, hdr` and sends the exact files as documents, e.g. 4K VP9/AV1. Both modes read the player once, reject live streams and media longer than `MAX_MEDIA_SECONDS`, and cap the rendition to the delivery budget using the published format sizes.
- The delivery budget is the per-file limit times `MAX_VIDEO_PARTS` (20 parts of 49 MB on the hosted Bot API, 4 parts of 2 GB with MTProto uploads), bounded by `MAX_SOURCE_BYTES` and 42 % of free disk. Nothing below 360p is chosen to save parts.
- “Highest” means the best stream the platform exposes to the extractor, not the creator's pre-upload master.
- Long videos are split on existing keyframes. The split points come from the packet sizes, so each part is close to the limit (about 94 %); every part is a fast-start MP4 that plays on its own and arrives as its own message with `Part i/N`. A 12-minute 210 MB test video became five parts, a three-hour 388 MB video nine parts.
- Collections use albums of at most 10 in source order; visual media, audio and documents use separate groups. GIF and animated WebP retain motion, and AVIF stills are sent as photos. Telegram may recompress photos.
- `/original <link>` sends the highest available downloaded files as Telegram documents, preserving their bytes, followed by separate soundtracks. Large originals are streamed as numbered `.partNNN` documents (concatenate them in order), or as single files up to 2 GB with MTProto uploads.
- An MP3 button appears after every delivered post (on the last media item, or as a message after an album). `/mp3 <link>` downloads only the audio. Existing MP3 audio is copied; other audio is encoded once at 320 kbps while the file fits one upload, otherwise at the highest of 256/192/160/128 kbps that does, and long audio that fits none is sent at 192 kbps in numbered parts. Files carry the post title and creator as ID3 tags and Telegram title/performer. This cannot improve a lower-quality source or recover music that the platform does not expose.
- Video variants are checked for an actual audio stream. Instagram's dedicated extractor and gallery-dl both read DASH audio for the matching carousel item. A post-wide soundtrack is added to each silent video independently and offered once through MP3. Videos without an exposed track are marked in the delivery caption.
- Captions carry the post title without hashtags, the platform and any missing-audio note.
- Files are streamed during upload rather than read entirely into RAM, and the status message shows upload progress.

### Large uploads (up to 2 GB)

The hosted Bot API accepts 50 MB per file. With `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from [my.telegram.org](https://my.telegram.org) (*API development tools*, any app name), files above 50 MB are uploaded by the same bot through Telethon (MTProto) with `messages.uploadMedia` and no chat, then sent through the normal Bot API call using the returned `file_id`. Captions, buttons, replies and topics are unchanged and no updates are read. The session is stored in `DATA_DIR/mtproto.session` (or `TELEGRAM_SESSION`) and the bot token signs in again after a Render restart. If an upload fails, large uploads pause for ten minutes (or Telegram's flood wait) and the file is re-split into 49 MB parts, so delivery continues. Keep both values in Render's secret environment.

An existing local Bot API server can be selected with `TELEGRAM_API_BASE=http://127.0.0.1:8081` instead; it also supports 2 GB files. A server must actually be installed and configured, and switching from Telegram's cloud server requires Telegram's documented `logOut` migration first.

## Reliability

- Downloads and file-to-MP3 conversions run outside Telegram update handlers. One status message shows elapsed time, the current phase, and transfer progress when measured. Edits start at most once a second in a private chat and every four seconds in a group, shared by all jobs in that chat. Telegram's retry delay takes priority. The same card keeps the final receipt or error. `/status` shows recent requests.
- Status cards use animated Telegram custom emoji from `STATUS_EMOJI_PACK` (`LoadingEmoji` by default) when Telegram permits them for the bot. The bot owner's Premium subscription can grant this access. The bot renders regular emoji if the pack is unavailable or Telegram rejects custom emoji. An empty value disables the pack lookup.
- A SQLite journal admits at most 20 pending jobs overall and 3 per user. Links and MP3 buttons share admission and duplicate checks; Instagram tracking parameters do not create another active job.
- Queued/downloading jobs are re-extracted after a restart, up to 3 recoveries and within 24 hours. Jobs that had started sending are marked interrupted and never replayed automatically; check received media before retrying.
- Direct CDN transfers resume within a job using Range/If-Range when the server supplies a stable validator. If a server ignores Range, the file is restarted cleanly. Resume across a process/device restart is not byte-level: the source is extracted again.
- Download failures offer a retry button before any media upload is attempted. Unsupported input gets a short explanation.
- Rate limits allow the next source-aware extractor to run; incomplete collections remain failures. MP3 mode accepts audio-only results for video links.
- H.264 video is copied when only its audio codec needs conversion. Existing MP3 streams are not re-encoded.
- Render can run the bot with a Telegram webhook and download media without Termux. The webhook authenticates Telegram's secret header. Termux remains available as an optional, mutually exclusive runtime when Render is configured only as a relay.
- Per-user serialization and bounded global work concurrency (`MAX_CONCURRENT_JOBS`, default 2, so one long video does not block other users; a worker uses about 60 MB of memory).
- Isolated download process; a 60-minute configurable deadline (`JOB_TIMEOUT_SECONDS`) kills its process group, including downloader children.
- CDN files are renamed after probing so their extension matches the bytes (Snapchat serves `.IRZXSOY` names).
- ffprobe validates downloaded files; HTML/JSON masquerading as media is rejected.
- Carousel order is retained; failed entries, extractor error records and item caps are reported instead of silently truncating a post.
- Snapchat will not substitute a recommended item when the requested Spotlight ID is missing.
- Snapchat keeps some Spotlights out of the web: the page says "This Snap isn't available on web — Try watching it in the Snapchat app" (`data-resolved="unavailable"`), and the exact-ID lookup returns nothing. Only the signed-in app can play them, so the bot explains this and suggests a screen recording. A video or audio file sent in a private chat (up to 20 MB on api.telegram.org, 2 GB with a local Bot API server) is returned as MP3, named after its caption or file name. The bot does not imitate the Snapchat app with an account.
- No generic page-wide download fallback: these can return thumbnails, recommendations or unrelated media.
- Telegram flood-control delays are honored in full. Proven connection-establishment failures may retry. For relay uploads, a unique request ID lets the phone retrieve a cached Telegram result after a lost reply; when the result is still unknown, the upload is not replayed.
- The carousel previously produced Telegram HTTP 400: multipart album files lacked their `attach://` names. Attachments now receive a unique name and the serialized Telegram request is checked in tests. Telegram's explicit HTTP 400 is reported as a rejection and allows a safe retry; uncertain timeouts still require inspection.
- A source's definitive HTTP 404 or YouTube automated-traffic check does not offer a futile immediate retry button. Snapchat Spotlight extraction can use the one Snapchat CDN video preloaded by the exact public post even when the page omits canonical tags; mismatched page IDs are still rejected.
- If the album arrives but editing the status to add the MP3 button fails, the bot sends a small fallback button message. Render records only a short job ID, platform, stage, item count and error class, without chat contents or links; historic chat messages and the phone's local log are not available from Render.
- Long uploads have dedicated timeouts; MP3 conversions share the same resource semaphore.
- Conversion processes and download process groups are stopped on shutdown. A local process lock prevents two workers using the same data directory. Separate devices/services must still avoid polling the same token.
- Dependency changes are detected during normal restart. Pending Telegram updates are preserved.

Render Free sleeps after 15 minutes without inbound traffic and has a 750-hour shared monthly limit; webhook traffic wakes it, with a possible cold-start delay. While a job is queued or running, `render_free.py` requests the service's own address every four minutes so a long download or upload is not cut off. `KEEP_AWAKE=1` also does this every ten minutes while idle, which removes the cold start but uses instance hours around the clock. Its local files, MP3 cache and SQLite job journal vanish on a restart or sleep, so admitted or unfinished work is not recoverable on that tier. A paid persistent runtime or a separately provisioned durable database is needed for strict always-on and crash recovery. Telegram's Bot API has no idempotency key for uploads; exactly-once delivery across ambiguous network failures cannot be guaranteed.

## Primary runtime: Render webhook

The existing `veltrix-downloader` Render service runs `python render_free.py` with `TERMUX_PRIMARY=0`. Telegram sends new links to its webhook; Render extracts and uploads media for any Telegram user who can message the bot. Media files live in Render's temporary job directory during extraction, conversion and upload. No phone process is required. Set one active receiver per bot token; switching to the webhook makes the old Termux poller receive a conflict until it is stopped.

Open [@VeltrixDownloaderBot](https://t.me/VeltrixDownloaderBot) for the active deployment. The startup log verifies the bot username, webhook URL match, pending updates and whether Telegram reported a recent webhook error.

The free service can sleep or restart during long jobs, cannot retain its local SQLite/cache, and has limited CPU/disk/network resources. This setup does **not** promise permanent 24/7 availability, every private post, or success from Render's IP when a platform blocks cloud hosts. Existing authorized cookie files on a phone are not silently copied to Render.

## Optional phone runtime: Termux

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

If operating in phone mode, set `TERMUX_PRIMARY=1` on Render and use Termux. Keep Termux battery optimization disabled. Normal startup installs a missing reboot script; reboot startup also requires Termux:Boot to be installed from the same source as Termux and opened once. Android can still terminate the process.

`.env.example` documents normal settings. Default caps: 100 items, 4 GiB per source, 9 GiB total download workspace, 128 MiB disk reserve, two concurrent jobs, four hours per media. Remuxing/splitting also needs additional free disk space.

`BOT_TOKEN` stays in `.env` on the phone and in Render's secret environment for cloud mode. Never commit tokens, cookies, sessions or proxy credentials. Existing optional authorized YouTube cookie configuration on Render is passed to the isolated worker; age restrictions remain enforced.

With `TERMUX_PRIMARY=1`, Render only relays Telegram API traffic; it never polls, sets a webhook or downloads source media. The relay verifies the bot token in an HTTPS request header. Public Render URLs and access logs never contain the token. Existing Termux installs add `TELEGRAM_RELAY_BASE=https://veltrix-downloader.onrender.com` once on startup if no custom Bot API server or relay setting is present; `TELEGRAM_RELAY_BASE=0` disables that default. **Do not expose your `.env` or relay authorization header.**

In phone mode, a relay request needs connectivity from the phone to Render and from Render to Telegram. Render free instances have [monthly shared instance-hour limits, network usage limits and spin-down behavior](https://render.com/docs/free). The free Render filesystem is temporary.

`bash termux_start.sh` now waits for the first successful Telegram update poll (up to 90 seconds, including relay wake time). It prints `Telegram polling verified` only when the bot can fetch updates; a startup timeout leaves the supervised worker retrying and reports the safe poll error. After a verified start, send `/start` and a public media link for the first live delivery check. On phones with aggressive battery management, exempt Termux from battery restrictions so Android does not suspend polling.

## Validation

```bash
python -m unittest discover -s tests -v
python -m compileall -q bot.py media_io.py media_process.py runtime_jobs.py telegram_network.py telegram_relay.py telegram_mtproto.py transfer.py source_metadata.py diagnose.py download_worker.py render_free.py tests
bash -n termux_start.sh termux_setup.sh termux_supervisor.sh termux_process.sh
```

The regression suite includes real generated MP4/M4A/PNG/GIF media, native animation/video conversion, MP3 extraction and bitrate selection, keyframe-planned splits, numbered part delivery, MTProto file_id encoding and fallback, rendition caps for long videos, progress text, mixed albums, cache ownership, exact-pin metadata, partial Instagram rejection and async delivery failures. Source-page and Telegram delivery tests use controlled fixtures; passing them is not live-platform certification.

See the [project map](PROJECT_MAP.md#version-and-evidence) for the current release and verified results. The [release audit](RELEASE_AUDIT.md) records older results.

### YouTube sign-in

YouTube asks cloud servers such as Render to sign in ("confirm you're not a bot"). A server bot then needs one of: the cookies of a signed-in YouTube session (free, recommended), VidFetch quota, or a residential connection (the Termux phone runtime). With a signed-in session yt-dlp uses its `tv_samsung`/`web_embedded` clients, which need no PO token, and Deno solves the player challenges.

Set it up once:

1. Use a **secondary** Google account, never your main one: YouTube can restrict accounts used by downloaders.
2. Install a cookie exporter that writes the Netscape format: on Android, Firefox with the add-on [cookies.txt](https://addons.mozilla.org/android/addon/cookies-txt/) (allow it in private browsing); on a computer, Firefox or Chrome with *cookies.txt* / *Get cookies.txt LOCALLY*.
3. Open a **private window**, sign in at youtube.com, then open `https://www.youtube.com/robots.txt` as the only private tab and export the cookies for youtube.com.
4. Close the private window immediately. YouTube rotates the cookies of sessions that stay open in a browser, which would invalidate the exported copy.
5. In Render, open the service → **Environment → Secret Files → Add Secret File**, name it `youtube_cookies.txt`, paste the file's text and save. Render redeploys.
6. The startup log line `source access: youtube=signed-in` confirms the session. `youtube=signed-out` means the file has no signed-in session (`LOGIN_INFO` and a `SAPISID` cookie are missing or expired); export again while signed in. Only cookie names are inspected and logged, never values.

When YouTube stops accepting the session, users see "YouTube no longer accepts the bot’s sign-in" and the owner repeats steps 3–5. Bot messages never ask users for cookies: account files must never be sent in chat. VidFetch and Newisty, when configured, still back up a refused sign-in or a session that YouTube rate-limits ("try again later"); a limited session is not retried with other format selectors.

Each download works on a private copy of the cookie file, because yt-dlp and gallery-dl rewrite their cookie file when they finish and concurrent jobs could otherwise read a half-written file. Refreshed cookies replace the shared copy atomically, and only when none of the owner's unexpired cookies were lost. A read-only Render Secret File is never modified.

### Owner accounts on every platform

The same sign-in route works for Instagram and Pinterest. Each platform takes the cookies of one **secondary** account as a Render Secret File:

| Secret File | What the account adds | How the bot uses it |
| --- | --- | --- |
| `youtube_cookies.txt` | Passes YouTube's server check. Unlisted videos, and private videos shared with that account | yt-dlp |
| `instagram_cookies.txt` | Posts, Reels, carousels, Stories and Highlights of private accounts that this account follows | gallery-dl first, then yt-dlp |
| `pinterest_cookies.txt` | Pins and secret boards that this account owns or collaborates on | gallery-dl and yt-dlp after the public Pin route |

A Snapchat account adds nothing on a server. Snapchat plays private and friends-only Snaps, and Spotlights it keeps off the web, only in its app.

Export each file as for YouTube: private window, sign in, export the site's cookies in Netscape format, then close the window without logging out (logging out ends the session). Instagram may ask to confirm a sign-in used from another country, because Render runs in Frankfurt. If possible, sign in and export through a connection located in Germany, then approve "This was me" in the Instagram app if asked.

The startup log reports each file as `missing`, `signed-out`, `signed-in` or `present` (Snapchat, state unknown). The check looks only at cookie names and Pinterest's non-secret `_auth` flag. Signed-in jobs on one platform start at least `ACCOUNT_SPACING_SECONDS` apart (default 5, at most 60), across worker processes, like a person opening links in turn; fallback routes of the same job do not wait again. When a post needs an account, the reply says whether the bot has a sign-in for that platform. It never asks users for cookies.

### Extractor updates

YouTube and Instagram changes are fixed by new yt-dlp and gallery-dl releases, but Render installs requirements only when it builds. The [Refresh extractors](../.github/workflows/refresh-extractors.yml) workflow checks PyPI every night (21:41 UTC). When a newer stable release exists, it raises the minimum version in `requirements.txt`, runs the tests with it and commits to `main`, and Render redeploys with the fix. Nightly and pre-release builds are never adopted. After a sudden YouTube failure, run it at once from **Actions → Refresh extractors → Run workflow**.

For a temporary Render source check, set `SOURCE_PROBE_UNTIL` to a short future UTC ISO 8601 time and `SOURCE_PROBE_URLS` to a JSON object such as `{"version":"9.17.2","urls":["https://www.youtube.com/watch?v=PUBLIC_VIDEO_ID"]}`. Use the target build's version and a real public test URL. On startup, `source_diagnostics.py` checks the worker output and logs media counts, dimensions and audio. It expires automatically. Use one relevant URL when possible to limit provider usage. For Telegram delivery, check the result and MP3 button in an authorized test chat.

## Runtime diagnosis

Run `python diagnose.py` on the phone for package/runtime checks, parallel DNS checks, free storage, saved job counts, local worker readiness and error categories. It independently tests authenticated `getMe` over the automatic and IPv4 routes, a direct IPv4 curl homepage request, and `getMe` through an available configured environment proxy before checking the bot identity and webhook. These direct checks are skipped when an explicit Telegram proxy or custom Bot API server is configured. With the relay it relies on authenticated `getMe` rather than a second health request. It never consumes updates, changes the webhook or prints raw log lines/tokens. It does not prove downloader success or exclude a second remote polling instance. `status=initializing` means no successful API call yet; `waiting for first poll` means `getMe` succeeded but fetching updates has not; `poll failing` reports a failed poll; `polling` requires a recent successful update fetch.

The worker log prints the authenticated bot username when initialization succeeds. Termux defaults `TELEGRAM_IPV4=1`: IPv4 is attempted first, with automatic address selection as a fallback only after a connection-establishment failure. `TELEGRAM_IPV4=0` prefers automatic selection, with IPv4 as a fallback on connection failure. A command setting such as `TELEGRAM_IPV4=1 bash termux_start.sh` overrides `.env` for that run. The worker health response reports the effective preference without revealing secrets. Explicit `TELEGRAM_PROXY` takes precedence; if absent, the worker uses a configured `HTTPS_PROXY`/`ALL_PROXY` HTTP(S) tunnel only after **both** direct connections fail before transmitting any request. `NO_PROXY` exclusions are respected. Unsupported proxy schemes are ignored. No hardcoded IP, DNS override or disabled TLS verification is used. A 302 response to `curl -4 -I https://api.telegram.org` checks the homepage, not the authenticated Bot API request; `getMe` is the relevant test. If all `getMe` routes time out, an accessible network or a working HTTPS proxy is still required.

To check a device's YouTube download before changing the active Telegram receiver, run `python diagnose.py --youtube '<video URL>'`. It downloads to a temporary folder, reports resolution, file size and whether audio is present, and removes the files when done. It does not call the Telegram API.

Restarting Termux waits for the previous supervisor to finish, identifies its direct `python bot.py` child and signals that child if graceful shutdown stalls. After a further bounded wait it terminates only that verified child. Restart never starts a second bot while the previous supervisor remains active. An upload interrupted by restart may need manual inspection before retrying; check `/status` rather than resending the same link blindly. `python diagnose.py` can be run on its own even when startup reports a shutdown delay.

Optional owner-supplied Netscape cookie files are `INSTAGRAM_COOKIE_FILE`, `YOUTUBE_COOKIE_FILE`, `PINTEREST_COOKIE_FILE`, and `SNAPCHAT_COOKIE_FILE`. On Render the simplest way is a **Secret File** named `youtube_cookies.txt` (or `instagram_cookies.txt`, …) under Environment → Secret Files: the bot copies `/etc/secrets/<platform>_cookies.txt` to a private writable file at startup, so no base64 step is needed. For YouTube, follow [YouTube sign-in](#youtube-sign-in). With YouTube cookies the configured providers (VidFetch, Newisty) remain a fallback for public videos when the session expires. On Render, the corresponding `INSTAGRAM_COOKIES_B64`, `YOUTUBE_COOKIES_B64`, `PINTEREST_COOKIES_B64`, `SNAPCHAT_COOKIES_B64` secret environment variables create permission-restricted temporary cookie files for supported extractors. Base64 is encoding, **not encryption**: set these directly as Render secrets, never in chat or GitHub. Instagram's authenticated gallery route runs first when a cookie file exists. Cookies may help with content the signed-in owner is authorized to view; a link or cookies alone do not guarantee extraction, and private Snapchat/Friends media may have no extractable web URL. Worker failures include a URL/token-redacted diagnostic in `logs/termux.log`.

## Upstream references reviewed

- [yt-dlp EJS requirements](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
- [yt-dlp YouTube token constraints](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
- [gallery-dl Pinterest extractor](https://github.com/mikf/gallery-dl/blob/master/gallery_dl/extractor/pinterest.py)
- [Cobalt Snapchat implementation](https://github.com/imputnet/cobalt/blob/main/api/src/processing/services/snapchat.js)
- [Telegram local Bot API limits](https://core.telegram.org/bots/api#using-a-local-bot-api-server)

Cobalt was studied for behavior and public schema structure. No Cobalt service or paid endpoint is required.
