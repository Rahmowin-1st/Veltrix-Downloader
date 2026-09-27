"""Exact-post metadata adapters. Never search recommendations or poster URLs."""
from __future__ import annotations


def instagram_audio_urls(item: dict) -> list[str]:
    urls = []
    # These fields belong to the selected post, not related posts or album art.
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"progressive_download_url", "audio_src", "audio_url"}:
                    if isinstance(value, str) and value.startswith("https://"):
                        urls.append(value)
                elif isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    for key in ("music_metadata", "clips_metadata", "music_info", "audio", "audio_info"):
        walk(item.get(key))
    return list(dict.fromkeys(urls))


def instagram_extractor(downloader):
    """Extend the pinned parth parser without changing its network behavior."""
    from parth_dl.extractors import MediaExtractor

    class CompleteMediaExtractor(MediaExtractor):
        def _parse_media_item(self, item):
            result = super()._parse_media_item(item)
            expected = max(int(item.get("carousel_media_count") or 0), len(item.get("carousel_media") or [item]))
            if len(result.get("entries") or []) != expected:
                raise RuntimeError("Incomplete Instagram carousel metadata")
            result["audio_urls"] = instagram_audio_urls(item)
            return result

        def _parse_graphql_media(self, item):
            result = super()._parse_graphql_media(item)
            sidecar = item.get("edge_sidecar_to_children") or {}
            expected = max(int(sidecar.get("count") or 0), len(sidecar.get("edges") or [item]))
            if len(result.get("entries") or []) != expected:
                raise RuntimeError("Incomplete Instagram carousel metadata")
            result["audio_urls"] = instagram_audio_urls(item)
            return result

    # Preserve the existing extractor's rate limiter and request configuration.
    downloader.media_extractor.__class__ = CompleteMediaExtractor
    return downloader


def pinterest_entries(pin: dict) -> list[dict]:
    """Build the complete ordered list from one pin, with video taking priority."""
    def media(node):
        video = node.get("videos") or node.get("video")
        if video:
            formats = [v for v in (video.get("video_list") or {}).values()
                       if isinstance(v, dict) and v.get("url")]
            if not formats:
                raise RuntimeError("Incomplete Pinterest video metadata; poster rejected")
            formats.sort(key=lambda f: (int(f.get("width") or 0) * int(f.get("height") or 0),
                                       ".m3u8" in f["url"]), reverse=True)
            return {"kind": "video", "formats": formats}
        if node.get("is_video") or node.get("type") == "story_pin_video_block":
            raise RuntimeError("Incomplete Pinterest video metadata; poster rejected")
        if node.get("audio"):
            audio = node["audio"]
            return {"kind": "audio", "formats": [{"url": audio.get("audio_url") or audio.get("url")}]}
        images = node.get("images") or (node.get("image") or {}).get("images") or {}
        formats = [v for v in images.values() if isinstance(v, dict) and v.get("url")]
        if not formats:
            sig = node.get("image_signature")
            if sig and len(sig) >= 6 and sig.isalnum():
                base = f"https://i.pinimg.com/originals/{sig[:2]}/{sig[2:4]}/{sig[4:6]}/{sig}"
                formats = [{"url": base + ext} for ext in (".jpg", ".png", ".webp")]
        if not formats:
            raise RuntimeError("Incomplete Pinterest image metadata")
        formats.sort(key=lambda f: int(f.get("width") or 0) * int(f.get("height") or 0), reverse=True)
        return {"kind": "image", "formats": formats}

    if pin.get("story_pin_data"):
        result = []
        for page in pin["story_pin_data"].get("pages") or []:
            for block in page.get("blocks") or []:
                kind = block.get("type", "")
                if kind in {"story_pin_paragraph_block", "story_pin_product_sticker_block", "story_pin_static_sticker_block"}:
                    continue
                result.append(media({"image_signature": page.get("image_signature"), **block}))
        if not result:
            raise RuntimeError("Incomplete Pinterest story metadata")
        return result
    if pin.get("carousel_data"):
        slots = pin["carousel_data"].get("carousel_slots") or []
        if not slots:
            raise RuntimeError("Incomplete Pinterest carousel metadata")
        return [media(slot) for slot in slots]
    return [media(pin)]


def find_pinterest_pin(document, pin_id: str) -> dict:
    candidates = []
    pending = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, dict):
            if str(node.get("id")) == pin_id and any(node.get(k) for k in ("images", "videos", "carousel_data", "story_pin_data")):
                candidates.append(node)
            pending.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return max(candidates, key=lambda p: (bool(p.get("story_pin_data") or p.get("carousel_data")),
                                         bool(p.get("videos")), len(p)), default={})
