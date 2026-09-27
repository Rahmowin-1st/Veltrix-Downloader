# Veltrix Downloader v9.2 audit — 2026-09-27

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
