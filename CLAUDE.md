# Veltrix Downloader continuation

Read the [project map](docs/PROJECT_MAP.md) for the current runtime, locations and verified samples. Use the [operations guide](docs/OPERATIONS.md) for quality behavior, credentials, limits and diagnostics.

## Start here

1. Check `git status --short --branch` and `git log -3 --oneline`. Check Render's active deployment if the task concerns production. Documentation commits can be newer than the deployed code.
2. Reproduce the reported public URL through the actual download path. Identify whether extraction, media preparation or Telegram upload failed before editing.
3. Run the relevant test module and `python -m unittest discover -s tests -v`. Match any live result to its commit and report count, resolution and audio when applicable.

`render_free.py` starts production. For a source failure, follow `bot.py:grab()` into the appropriate extractor and `source_metadata.py`; for a Telegram delivery failure, follow `bot.py:run_job()`, `bot.py:send_album()` and `media_io.py`. Large files use `telegram_mtproto.py` only when `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` are set. YouTube on Render depends on the owner's sign-in (`source_auth.py`, startup log `youtube=signed-in`) or provider quota. Commits by `github-actions[bot]` that raise extractor versions come from [Refresh extractors](.github/workflows/refresh-extractors.yml). See [Backend Gates](.github/workflows/backend-gates.yml) for the verification environment.

The operating goal is to return the exact post and all available album items in source order, photos as photos and videos as playable videos, with the best exposed quality and source audio where available, including videos up to three hours and an MP3 for every delivery. Render secrets and local session files stay in the owner's existing environment. Use the existing Render service and configured providers for authorized production work. Termux is an optional alternative.
