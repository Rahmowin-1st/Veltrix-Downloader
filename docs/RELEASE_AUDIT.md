# Veltrix Downloader release audit — 2026-09-29

## v9.7.2 source-probe follow-up

- Live Render probes of the failing Snapchat Spotlight ID: normal and locale pages HTTP 404. The `/embed` page returned HTTP 200; compressed-response decoding failed, while `Accept-Encoding: identity` yielded HTML. Its Next.js query matched the requested ID, but the top-level `contentUrl` was empty and no story matched. The embed did not expose the video. The bot now requests identity encoding and tries a verified embed video when one is present. It rejects this specific empty source instead of substituting a recommendation.
- The two actual failing YouTube video IDs returned `Sign in to confirm you’re not a bot` on Render under default, `web_safari` and `android_sdkless` clients. A synthetic public-video success on the same host was misleading, so the startup probe was removed. This needs platform-granted access from the runtime; no universal free bypass or successful download is claimed.
- The actual Pinterest share link exposed eight gallery entries, all image or other non-audio formats; no audio stream or video was declared by the source metadata. The eight image deliveries were already verified. MP3 stays hidden when no audio exists.
- Local regressions cover compressed-page request headers, 404-to-embed fallback, exact-ID ownership, empty metadata rejection and a partial story's top-level video. Live Telegram delivery of another Snapchat clip is still unverified.

## v9.7.1 live follow-up

- User screenshot and Render logs at 00:47–01:12 UTC: Instagram carousel job delivered all 15 entries; Pinterest Pin delivered all 8 entries. The album HTTP 400 fix is therefore verified end to end on those posts. Telegram sent 15 items in two albums because its API caps one media group at ten.
- The Snapchat Spotlight job failed during extraction: yt-dlp received HTTP 404 for that exact public page. Its dedicated metadata path now avoids a redundant redirect fetch and may use a single primary Snapchat CDN video on an exact Spotlight page without canonical tags; an actual HTTP 404 remains unresolved unless a public route exposes the media. The app opening a share link does not prove its server-side page is public.
- Two YouTube jobs failed before Telegram upload with `Sign in to confirm you’re not a bot` from Render's IP. Another public video's metadata probe succeeded; it cannot establish access for these two. The bot now labels this platform check and does not offer a futile retry. Authorized cookies or a permitted working source route may be required, and a link alone cannot provide those credentials.
- MP3 was shown after a Pin even though none of its delivered items exposed audio. The button is now conditional on a probed audio stream or separate soundtrack. A photo-only post cannot produce background music that the platform does not expose. The Instagram MP3 re-extraction also returned 15 items with no accessible audio; it is an audio availability failure, not a failed carousel upload.
- Render free spun down between 01:04 and 01:11 UTC and woke for a new link. Its temporary cache and SQLite are not durable. Strict uninterrupted 24/7 availability remains out of scope for this free instance.

## v9.7.0 Render worker and verified carousel root cause

- Live Render log for the owner's new test: Instagram downloaded **15** carousel entries, then Telegram rejected the first `sendMediaGroup` with HTTP **400**. Source extraction was successful. `bot.send_album` wrapped already constructed `InputFile` objects without `attach=True`; python-telegram-bot serialized each album entry without the required `media: attach://...` value. Set `attach=True` only for group uploads; a regression test checks the actual encoded Telegram parameter and multipart parts. An explicit HTTP 400 is treated as a rejected upload eligible for retry, while read/write timeouts remain uncertain.
- A subsequent Snapchat job failed in the extraction process (`WorkerFailure`) before upload. The isolated worker now reports a bounded source error category to Render, without link or chat text. Exact Snapchat Spotlight detection supports a matching canonical `<link>` and a matching `__NEXT_DATA__` query when a unique preload exists. Actual owner Spotlight links still need a new live run.
- Render logs confirmed `ffmpeg=True ffprobe=True deno=True` before switching receivers. The webhook runtime has a signed Telegram secret header and an independent read-only `getWebhookInfo` registration probe. Render stores active media files locally for the duration of a job, and processes updates for other bot users while the phone is off.
- Render Free's filesystem and SQLite journal are ephemeral. After a restart, an MP3 callback can reconstruct the exact source URL from the bot's Original button using an HMAC tied to the requesting user, then re-extract available audio. Pending jobs, user history and source bytes cannot survive Render restarts on this plan; Telegram's pending webhook updates are subject to its retention window. Free Render cannot provide a strict always-on SLA or guarantee source downloads from datacenter IPs. Private posts require authorized source access.

## v9.6.0 screenshot-driven fixes

- Owner screenshots: YouTube and Instagram Reels delivered, while a Pinterest short link and an Instagram carousel reported unconfirmed Telegram delivery; Snapchat Spotlight returned unavailable. Render emitted a `BrokenPipeError` near the Pinterest attempts. Render did not retain the Telegram chat or the phone's worker log, so that log line alone cannot identify an exact post.
- Media uploads through the relay now carry a random request ID. The relay retains a bounded response receipt after the phone disconnects; on a read timeout, the phone queries that receipt instead of sending the media again. If the result cannot be confirmed, the job remains interrupted for manual inspection. Controlled test drops a response after Telegram accepts a photo and verifies a single upload and recovered message ID.
- Snapchat exact Spotlight lookup accepts the page's own top-level video when its query ID matches, even if recommendations are present. HTML fallback accepts one video tag only when the canonical Open Graph URL names the requested Spotlight ID; it refuses recommendation pages.
- If Telegram rejects an edit to the album status, the MP3 button is sent in a separate text message. Readable failure class replaces the formerly hidden status-edit error. Authenticated Render job events record stage/item count/error class only; chat text, links and tokens are rejected. A missing Termux reboot script is now installed at normal startup.
- Limitations: the exact Pinterest/Snapchat/Instagram URLs could not be fetched from this editing environment or from the phone; end-to-end platform acceptance remains unverified. The phone remains the only downloader and poller. Free Render sleeps and loses local state on restart, so it cannot provide guaranteed 24/7 service while the phone is off; an always-on host with durable storage and a single polling worker is required for that guarantee. Private account media needs valid authorized access, and no extractor guarantees all private posts.

## v9.5.0 polling recovery

- Phone report: `getMe` succeeded through Render, while `last_poll_ok` remained zero. The relay emitted a fresh startup after hours without traffic; a nine-second health probe timed out during its cold wake. That probe cannot establish failure when authenticated `getMe` later succeeds.
- Relay polling uses five-second Telegram long polls to avoid long HTTP holds and report failures sooner. Render records the first `getUpdates`, the upstream status periodically and failures by exception class; it never logs request bodies or credentials.
- The phone now records the beginning and error category of each poll separately from other API requests. A later successful `getMe` cannot erase a polling conflict.
- The launcher waits up to 90 seconds for a successful Telegram poll before reporting ready. On failure it leaves the supervisor running and prints only a safe error category and whether polling was attempted. The standalone diagnostic no longer sends a redundant nine-second relay health request.
- Tests cover PTB → local HTTP relay → upstream mock → empty `getUpdates`, multipart/file transfer, polling conflict and temporary network state. A real phone `/start` and source download still have not been observed.

Render's free service sleeps after 15 minutes without inbound traffic, and phone background execution depends on Android battery policy. The free plan cannot guarantee an always-on bot.

## v9.4.1 polling readiness

- `/readyz` now reports ready only after a successful `getUpdates` response. A successful `getMe` confirms API access but cannot prove that the bot is receiving messages.
- `diagnose.py` distinguishes a verified polling worker from one still waiting for its first poll; the relay polling path has an isolated client test.
- Live `/start` replies and public platform media delivery still require a phone and Telegram chat check; unit tests cannot establish those outcomes.

## v9.4.0 authenticated Render relay

The phone's direct Telegram routes and a proxy-free curl call all timed out; the existing free Frankfurt Render service successfully authenticated `getMe` with Telegram while staying health-only. Termux now configures the owner's Render URL once when no custom API target exists. Render routes Bot API methods and file downloads to Telegram while the phone remains the only polling/downloading worker. Bot credentials travel in an HTTPS header and are compared to the service's existing token; public paths never include the token. The relay streams request/response bytes, bounds concurrent calls and request size, never redirects upstream and restricts its destination to the official Telegram Bot API host.

This is a route around the phone's blocked direct Telegram connection, not a claim that source platform downloads or end-to-end Telegram media delivery are certified. Free Render instance hours and inbound/outbound usage are limited, and Render may sleep or redeploy. Termux still needs access to the Render URL; synthetic upload tests cannot establish phone-to-Render reachability or the behavior of actual large uploads at the platform edge.

## v9.3.3 Telegram connectivity follow-up

Phone evidence: v9.3.2 started its supervisor, DNS worked, but authenticated direct automatic and IPv4 Bot API `getMe` each ended in `ConnectTimeout`; the direct client also timed out. The worker was still initializing at the first health read. This establishes no successful Telegram connection and therefore cannot establish live media delivery.

The worker now attempts a configured standard HTTPS proxy only if both direct routes fail while establishing a connection, respects `NO_PROXY` and keeps normal TLS certificate verification. It does not replay requests after read or write errors. The diagnostic contrasts direct authenticated requests, the environment proxy if one exists, and an unauthenticated direct curl homepage HEAD; it rereads worker health after probes. Private proxy details and the token are never printed. Unnecessary direct connection retries were removed to avoid delaying fallback. If the phone has no reachable Telegram route or configured working proxy, this code cannot make it connect; a working network route is required. No live phone acceptance has been observed yet.

## v9.3.2 restart follow-up

Termux reported `The previous worker is still finishing its shutdown. No second worker was started.` after a fast-forward update. The old launcher waited only 20 seconds and stopped; the supervisor's exit trap can wait on the Python child without a deadline. The new launcher detects unreaped zombie supervisors, signals only the previous supervisor's verified direct `python bot.py` child after a grace period, and bounds that child's shutdown before deciding whether a new worker is safe to start. It still refuses to launch a second worker if the old supervisor remains active or the PID points to an unrelated process. A forced restart during media upload has uncertain delivery; the durable job journal records interrupted uploads for user inspection. A synthetic hanging-child integration test runs in CI; this editing sandbox has a mismatched `/proc` PID namespace and skips that subprocess integration test locally.

## v9.3.1 phone connectivity follow-up

The reported Termux check showed `ready=False`, no successful Bot API call yet, and a `TimedOut` during `getMe`. DNS and a previous unauthenticated `curl -4 -I` do not establish Bot API connectivity. The worker may have still been initializing when the local health endpoint was read.

- Both address preferences now retain the other route as a connect-only fallback when no Telegram proxy is configured. Read/write failures cannot be replayed safely.
- A one-run `TELEGRAM_IPV4=1 bash termux_start.sh` setting takes precedence over `.env` for that worker; the local health endpoint exposes the effective setting without exposing the proxy or token.
- `diagnose.py` makes bounded, read-only authenticated `getMe` requests on both address routes and reports only HTTP status or error type. It also distinguishes an initializing worker from a disconnected one. It skips direct route tests when a custom API base or explicit proxy is configured.

105 local tests, shell parsing and compilation verify this change. The phone's Bot API response and end-to-end source downloads require a live follow-up; no connection fix is claimed if both phone route probes time out.

## v9.3 recovery audit

| Confirmed weakness | Implemented fix | Evidence |
| --- | --- | --- |
| Long jobs occupied all eight Telegram update slots | Bound and detach work from update processing | 12 pending jobs while `/start` still completes |
| Restart lost admitted jobs | SQLite journal and bounded recovery before upload | New manager instance reloads queued/downloading work |
| Restart during upload could cause duplicate delivery | Persist sending intent before API call; mark uncertain work interrupted | Simulated upload timeout, no replay after recovery |
| A lost acknowledgement dropped a user request | Deferred status creation; job already admitted durably | Failed acknowledgement still invokes downloader |
| Direct-download disconnect discarded all bytes | Validated Range/If-Range continuation | Interrupted stream resumes exact bytes; changed object never concatenates |
| Any Telegram timeout was terminal | Retry only proven pre-send connection failures; IPv4/automatic fallback | Connection, read and write error tests |
| Startup required an online preflight | Move authentication to supervised worker | Shell/compilation check; live phone remains required |
| Conversion threads could keep shutdown alive | Track, terminate and reap ffmpeg/ffprobe processes | Real child process shutdown test |
| Cache requests bypassed duplicate admission | Shared durable queue for link and MP3 actions | Queue ownership/cap tests and post-cache fallback test |
| Empty gallery errors prevented Pinterest fallback | Distinguish no extraction from partial extraction | Empty metadata allows fallback; partial metadata still rejected |
| Instagram swallowed incomplete metadata errors | Propagate completeness failure | Dedicated parser regression |
| Audio suffix could misrepresent its codec/container | Prepare audio from probed streams | Real Opus bytes mislabeled M4A become playable MP3 |
| Health endpoint only reported process existence | Add observed API/poll connectivity and readiness | Request observer plus bounded read-only diagnosis |

101 local tests pass. Compilation, shell syntax, whitespace and dependency integrity checks pass. Source fixtures and Telegram mocks are not production platform certification. A direct Telegram connectivity probe from the editing environment failed at its configured proxy (CONNECT timeout), so it provides no evidence about the phone's current network.

Remaining boundaries: Android can suspend/kill Termux; internet and free storage are required; source blocks and expired/private/unsupported posts can fail. Recovery needs persistent `data/jobs.sqlite3`. In-progress uploads are not resumable or safely replayable without user inspection. No paid extraction service was added. The existing [competitor review](COMPETITOR_REVIEW.md) informed the queue/retry decisions; this release does not establish superiority over those products.

## v9.2 follow-up

| Problem | Fix | Verification |
| --- | --- | --- |
| Video-link guard rejected MP3-mode audio | Apply poster guard only to visual downloads | Reel audio fallback regression |
| Generic `limit` check stopped fallback on rate limits | Separate resource/incomplete failures from rate limits | Fallback and incomplete-carousel regressions |
| MP3 encoded twice | Keep source audio until delivery; copy existing MP3 | Real encoded-packet hash comparison |
| H.264 video re-encoded for incompatible audio | Choose copy/encode independently per track | H.264 packet hashes equal before/after Opus-to-AAC conversion |
| Worker error lost its category | Preserve sanitized worker failure type | Safe category/message regression |
| Duplicate link requests repeated work | Active link deduplication and three-request per-user cap | Concurrent requests and failed-ack cleanup tests |
| Silent input rejection and unclear waiting | Guidance, stage/elapsed status and pre-upload retry action | Input and ambiguous-upload regressions |
| Nested startup connection retries could stall | Whole-attempt timeout, three bounded attempts | Python heredoc compilation and shell syntax |

81 local tests pass, including generated media and async error paths. Compilation, shell syntax, whitespace and dependency integrity checks pass. See [competitor review](COMPETITOR_REVIEW.md) for the researched product decisions.

No new live URL-to-Telegram success is claimed: the actual failing source links and the phone runtime are still unavailable. The research is a focused comparison, not an exhaustive benchmark of every downloader.

## v9.1 follow-up

- Confirmed and removed document-only photo delivery; use ordered native albums.
- Confirmed upstream Pinterest carousel extraction chooses slot images without checking slot videos. Rebuild the manifest from exact-pin metadata and reject missing video URLs instead of delivering a cover.
- Added exact-ID embedded-JSON Pinterest fallback, never related/recommended pins.
- Wrapped the pinned Instagram parser to reject omitted children and preserve exposed background-audio URLs. All-post MP3 actions cache every available source; MP3 conversion and upload are grouped.
- Made IPv4 optional and configured separate polling and upload transports. The pasted DNS traceback predates the latest successful startup, so it does not prove the current failure or an IPv6 fault.
- Added read-only diagnostics and authenticated startup identity. Worker error categories survive temporary-directory cleanup with URL/token redaction.
- Remaining blocker: no current failing media URLs, live bot token or access to the phone was provided. YouTube/Instagram/Snapchat full production recovery is NOT verified.
- Photos may be recompressed by Telegram. Native playback of incompatible video requires lossy codec conversion; the highest source stream is still requested. Albums have 10-item/type constraints; their MP3 keyboard is on the status message.

## Previous v9 audit (historical)

Baseline: `4c999bbae572e62e308c1633cd27be8853ff7ee3`.

| Observed code problem | Change | Evidence / remaining limit |
| --- | --- | --- |
| 720p ranked over 1080p/4K | Best available format policy | Quality and downloader-option regression tests |
| Snapchat could choose the first unrelated recommendation | Match requested ID; reject missing match | Matching, missing-ID and public story fixture tests |
| Snapchat always forced video | Parse story media type per item | Mixed image/video story fixture |
| Flattened Pinterest formats lost page identity | Ordered gallery manifest with per-page download | Mixed video/image/audio manifest test |
| Gallery process could return partial files as success | Parse error records even on exit code zero; reject missing entries | Partial carousel and gallery error tests |
| Filename extension treated as authoritative media type | Inspect real media streams with ffprobe | Real fixtures with deliberately wrong suffixes |
| Large files silently recompressed | Stream-copy splitting / remux; original-codec file fallback | Real audio, dimensions, codec and duration validation |
| Photos recompressed despite maximum-quality request | Send original document by default | Original byte-preservation delivery test |
| Upload retries could duplicate completed sends | Retry explicit flood rejection only | Timeout single-send test and RetryAfter test |
| RetryAfter cut to 30 seconds | Wait full server-specified interval | 90-second delay calculation test |
| Long upload used short library defaults | Dedicated 1800-second media upload timeout; streaming InputFile | Builder and file-handle inspection; live transport still unverified |
| Preview threads continued after wait_for expired | Immediate queue acknowledgement; one isolated download process | Worker deadline/reaping test |
| Cached audio bypassed global work limit | Share semaphore | Code review |
| Supervisor TERM cleanup could resume its loop | Exit trap + reject overlapping restart | Shell syntax and code review |
| Dependency preflight tested imports but not changed requirements | Requirements checksum invalidation | Code review |
| Render dropped pending updates | Preserve pending updates in polling and webhook modes | Code review |

## Completed verification

- 50 local unit/regression tests, including real ffmpeg/ffprobe fixtures.
- Python compilation, shell parsing and git whitespace checks.
- Installed dependency integrity (`pip check`) passed in the editing runtime.
- Read current upstream yt-dlp, gallery-dl and Cobalt implementations/documentation.

## Not verified / not claimed complete

- All four direct platform HTTP probes timed out in this environment. This does not establish a platform outage or a downloader success.
- Live Telegram upload, token health and the user's running Termux state were not accessible.
- Full coverage of every platform/media/link combination is not established.
- Local Bot API server is supported by configuration, not deployed by this change.
- Automatic recovery of an in-flight job after device power loss is not implemented.
- Platform 403/rate limits, expired URLs and upstream schema changes remain external failure modes.

## Runtime acceptance checklist

Use public posts you own or are authorized to download. Send links normally to the bot, with no quality selection.

1. YouTube: Short, ordinary video, long video larger than cloud Telegram limit; verify highest selected rendition, sound, full duration and ordered parts.
2. Instagram: photo, Reel and mixed carousel; compare source item count and order, check every video's sound.
3. Snapchat: Spotlight, exact public story photo, exact public story video, public story sequence. Confirm no recommendation is substituted for an expired item.
4. Pinterest: image, GIF, video, mixed Idea Pin; compare every page and available audio block.
5. Queue two requests; confirm both finish in request order. Resend an active link and confirm only one job runs. Tap the post-level MP3 button and confirm all exposed audio sources are included.
6. Restart when idle; confirm a single polling worker and normal `/start`. Inspect `logs/termux.log` for real failures without sharing the bot token.

A release should be called production-verified only after these checks pass on the actual network/runtime.
