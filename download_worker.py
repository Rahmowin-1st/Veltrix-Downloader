"""One isolated download. Parent owns its lifetime, output directory and deadline."""
import json
import os
import sys
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
        payload = {"error": bot.friendly_error(bot.platform_of(request["url"]) or "", exc)}
    (root / "result.json").write_text(json.dumps(payload))


if __name__ == "__main__":
    main()
