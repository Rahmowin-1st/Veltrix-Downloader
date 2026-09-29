"""One isolated download. Parent owns its lifetime, output directory and deadline."""
import json
import os
import sys
import re
from pathlib import Path

import bot


def failure_category(exc: Exception) -> str:
    message = str(exc).lower()
    if "confirm you’re not a bot" in message or "confirm you're not a bot" in message:
        return 'PlatformBotCheck'
    if any(word in message for word in ('429', 'rate limit', 'too many requests')):
        return 'RateLimited'
    if any(word in message for word in ('login', 'sign in', 'private', 'cookies')):
        return 'AccessRequired'
    if any(word in message for word in ('403', 'forbidden', 'blocked')):
        return 'SourceBlocked'
    if any(word in message for word in ('404', 'not found', 'expired', 'unavailable')):
        return 'SourceUnavailable'
    if any(word in message for word in ('incomplete', 'truncated')):
        return 'IncompleteSource'
    if any(word in message for word in ('timed out', 'timeout', 'deadline')):
        return 'SourceTimeout'
    return 'ExtractionFailed'


def main():
    root = Path(sys.argv[1]).resolve()
    os.environ["VELTRIX_JOB_ROOT"] = str(root)
    request = json.loads((root / "request.json").read_text())
    try:
        paths = bot.grab(request["url"], request["mode"], str(root), request.get("meta"))
        payload = {"files": [str(p.resolve()) for p in paths]}
    except Exception as exc:
        detail = re.sub(r"https?://\S+", "[url]", str(exc))
        detail = re.sub(r"\d{5,}:[\w-]+", "[token]", detail)
        payload = {"error": bot.friendly_error(bot.platform_of(request["url"]) or "", exc),
                   "category": failure_category(exc),
                   "diagnostic": f"{type(exc).__name__}: {detail[:600]}"}
    (root / "result.json").write_text(json.dumps(payload))


if __name__ == "__main__":
    main()
