"""One isolated download. Parent owns its lifetime, output directory and deadline."""
import json
import os
import sys
import re
from pathlib import Path

import bot


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
                   "diagnostic": f"{type(exc).__name__}: {detail[:600]}"}
    (root / "result.json").write_text(json.dumps(payload))


if __name__ == "__main__":
    main()
