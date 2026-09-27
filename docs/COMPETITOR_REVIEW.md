# Downloader comparison — 2026-09-27

Scope: official documentation and public repository features. These products were not benchmarked against the user's failing links. Feature claims below describe their documentation, not verified current platform success.

| Project and primary source | Observed approach | Veltrix decision |
| --- | --- | --- |
| [Cobalt API](https://github.com/imputnet/cobalt/blob/main/docs/api.md) | Automatic/audio modes, typed multi-item responses, separate audio and local processing operations | Keep link-first automatic delivery, source item types and a post-level audio action; select copy/remux/encode per stream |
| [Seal](https://github.com/JunkFood02/Seal) | Android yt-dlp interface, audio metadata and managed downloads | Keep extractor/runtime health checks; avoid exposing command templates or extra setup in the normal chat flow |
| [YTDLnis](https://github.com/deniscerri/ytdlnis) | Queue, quick download, failure logs and redownload actions | Add visible stage/time, active-link deduplication and a retry action when delivery has not begun |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Format selection, stream merging and site-specific extractors | Retain best exposed streams; obtain source audio once and convert only at delivery |
| [gallery-dl](https://github.com/mikf/gallery-dl) | Gallery and collection extraction | Keep ordered collection metadata, completeness checks and dedicated Pinterest interpretation |

Cobalt's hosted API requires permission from its owner for integration. Veltrix does not depend on it or on a new paid extraction service. An alternative UI built around the same extractor is not evidence that it can overcome a particular source failure.

Platform routes remain distinct: YouTube uses yt-dlp/EJS; Instagram tries its dedicated parser then gallery-dl/yt-dlp; Snapchat selects the requested public story/Spotlight metadata; Pinterest preserves pin/page boundaries and chooses video over poster metadata. A temporary extractor rate limit may advance to another route, while incomplete collections and explicit age restrictions stop the job.

[Telegram Bot API](https://core.telegram.org/bots/api#sendmediagroup) constrains album delivery: photo/video items can share an album, audio is grouped separately, and a media group contains 2–10 items. Its media-group method has no inline keyboard parameter. Veltrix therefore places the single MP3 button on the status message and splits larger collections without silently dropping items.

Remaining validation requires public test URLs on the actual phone: each media type, complete item count/order, duration/audio, available source quality and successful native Telegram delivery. Current fixture tests do not establish a production success rate for any competitor or Veltrix.
