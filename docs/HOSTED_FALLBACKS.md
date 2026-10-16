# Hosted extraction fallback

Local extractors remain first. Hosted providers are enabled only when their key
is set in the running host's environment. No account has been created, subscribed
or funded by this change. Provider claims are not an end-to-end success result.

| Platform | Provider | Environment | Scope |
| --- | --- | --- | --- |
| YouTube | [Tunelio](https://developers.tunelio.dev/docs/) | `TUNELIO_API_KEY` | One public video, Shorts or audio; merged video/audio URL |
| Snapchat | [Apify actor](https://apify.com/convertfleetdotonline/snapchat-downloader) | `APIFY_TOKEN` | Public clips accepted by the actor; one run per fallback |

Set these in Render → veltrix-downloader → Environment. Save and deploy. Do not
paste keys in Telegram, source code, issues or chat. Existing Termux workers need
the keys in their own `.env` too; Render environment does not configure Termux.
Startup logs report only whether the two hosted providers are configured.

Tunelio advertises 100 one-time trial credits, with `/info` costing 6 credits and
`/create` costing 10. An automatic video fallback therefore uses 16 credits; an
audio fallback uses 10. Trial resolution limits and availability depend on the
provider. Higher paid plans expose higher limits. The implementation does not
purchase a plan or retry different paid formats after a provider rejection.

The Apify actor advertises $5/1,000 resolved videos. Each run here has a $0.02
maximum total charge and a 120-second actor timeout. The same run is polled, so
polling does not submit duplicate jobs. The actor itself uses yt-dlp; hosting it
elsewhere can help with a host-specific block but does not prove that an expired
or inaccessible Snapchat link is recoverable.

API credentials are attached only to fixed API endpoints, with redirects disabled.
Returned media links use the existing SSRF checks, bounded transfer and ffprobe
inspection. API/CDN error text and signed links are not copied into user errors.
If the provider promises audio and sends a silent file, delivery is rejected.
Separate exposed Snapchat audio is merged without re-encoding video.

YouTube playlists stay with the collection-aware local extractor. They are never
replaced with a single item by this one-video API. Instagram and Pinterest retain
their existing source-aware engines; these two APIs do not repair their missing
music metadata.

## Validation

Unit tests use mock HTTP transports for authentication, provider state, quality,
and failure contracts. A real FFmpeg-generated video/audio fixture exercises
`grab` after a simulated local bot-check, through the hosted adapter and ffprobe.
This verifies integration but is not a real provider media download.

Before claiming success, enable a provider with an owner-created key, send a known
public link, confirm delivered video audio and its inline MP3, then test the
previously failing URL. No live provider extraction was verified without a key.

## Research

- [Cobalt API](https://github.com/imputnet/cobalt/blob/main/docs/api.md) requires
  permission to use hosted instances; no random public instance is hardcoded.
- [Snapchat export downloader](https://github.com/ethanwheatthin/All-In-One-Snapchat-Downloader)
  processes owner-exported Memories/chat data rather than arbitrary Spotlight URLs.
- [yt-dlp PO tokens](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) are a
  separate option for supported clients, not proof that a blocked server will work.
