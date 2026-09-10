"""Pick a YouTube format that actually has a URL (SABR-safe)."""

from __future__ import annotations

from typing import Any


def choose_format_id(info: dict[str, Any], key: str, want_height: int) -> str | None:
    fmts: list[dict[str, Any]] = []
    for f in info.get("formats") or []:
        if not f.get("url"):
            continue
        fid = str(f.get("format_id") or "")
        if fid.startswith("sb"):
            continue
        if str(f.get("protocol") or "").startswith("mhtml"):
            continue
        fmts.append(f)
    if not fmts:
        return "best" if info.get("url") else None
    if key == "mp3":
        pool = [f for f in fmts if (f.get("acodec") or "none") != "none"] or fmts
        pool.sort(key=lambda f: f.get("tbr") or f.get("abr") or 0, reverse=True)
        return str(pool[0]["format_id"])
    vids = [f for f in fmts if (f.get("vcodec") or "none") != "none"] or fmts
    below = [f for f in vids if (f.get("height") or 0) <= want_height]
    use = below or vids
    use.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    return str(use[0]["format_id"])
