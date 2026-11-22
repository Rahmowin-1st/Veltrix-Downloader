"""Exact-post metadata adapters. Never search recommendations or poster URLs."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from urllib.parse import urlparse, urlunparse


class IncompletePost(RuntimeError):
    """A known post is incomplete. Another extractor must recover every slot."""

    def __init__(self, message: str, expected_kinds: tuple[str, ...]):
        super().__init__(message)
        self.expected_kinds = expected_kinds


def media_quality(fmt: dict) -> tuple[float, float, float]:
    def number(value):
        try:
            return max(0, float(value or 0))
        except (ValueError, TypeError):
            return 0

    width, height = number(fmt.get('width')), number(fmt.get('height'))
    return width * height or max(width, height), number(fmt.get('fps')), number(fmt.get('bitrate') or fmt.get('tbr'))


def youtube_post_attachment(page: str, post_id: str) -> dict:
    """Read the one Community post that owns the requested ID."""
    for match in re.finditer(r'(?:\bytInitialData\b\s*=|\[\s*["\']ytInitialData["\']\s*\]\s*=)\s*(?=\{)', page):
        try:
            document, _ = json.JSONDecoder().raw_decode(page, match.end())
        except ValueError:
            continue
        for node in dictionaries(document):
            for name in ('backstagePostRenderer', 'postRenderer'):
                post = node.get(name)
                if isinstance(post, dict) and post.get('postId') == post_id:
                    attachment = post.get('backstageAttachment') or post.get('attachment')
                    if isinstance(attachment, dict) and attachment:
                        return attachment
    return {}


def youtube_photo_entries(attachment: dict) -> list[dict]:
    """Use attached images only, with each post or poll image in source order."""
    if 'backstageImageRenderer' in attachment:
        images = [attachment['backstageImageRenderer'].get('image') or {}]
    elif 'postMultiImageRenderer' in attachment:
        images = [(row.get('backstageImageRenderer') or {}).get('image') or {}
                  for row in attachment['postMultiImageRenderer'].get('images', [])]
    else:
        poll = attachment.get('pollRenderer') or attachment.get('imagePollRenderer') or {}
        images = [row['image'] for row in poll.get('choices', []) if row.get('image')]

    entries = []
    for image in images:
        formats = []
        for row in image.get('thumbnails', []):
            if not isinstance(row, dict) or not isinstance(row.get('url'), str):
                continue
            parsed = urlparse(row['url'])
            host = parsed.hostname or ''
            if parsed.scheme == 'https' and (host.endswith('.googleusercontent.com') or host in {'i.ytimg.com', 'yt3.ggpht.com'}):
                formats.append(row)
        entries.append({'kind': 'image', 'formats': sorted(formats, key=media_quality, reverse=True)})
    return entries


def dictionaries(document):
    """Walk JSON containers without descending through scalar values."""
    pending = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            yield node
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)


def snapchat_modern_video(page: str, requested_id: str) -> dict:
    """Read the requested Spotlight from JSON-LD or streamed Next.js data."""
    decoder = json.JSONDecoder()
    documents, chunks = [], []
    for script in re.finditer(r'<script\b([^>]*)>(.*?)</script\s*>', page, re.I | re.S):
        body = script[2].strip()
        if re.search(r'\btype\s*=\s*["\']application/ld\+json["\']', script[1], re.I):
            try:
                documents.append(json.loads(body))
            except ValueError:
                pass
        for marker in re.finditer(r'\bself\.__next_f\.push\(\s*', body):
            try:
                chunk, _ = decoder.raw_decode(body, marker.end())
            except ValueError:
                continue
            if isinstance(chunk, list) and len(chunk) == 2 and chunk[0] == 1 and isinstance(chunk[1], str):
                chunks.append(chunk[1])
    # A record can span several script tags. Text records prefix JSON with
    # T<byte length>, while model records start directly with the JSON value.
    stream = ''.join(chunks)
    for marker in re.finditer(r'(?:^|\n)[0-9a-f]+:(?:T[0-9a-f]+,)?(?=[{\[])', stream):
        try:
            document, _ = decoder.raw_decode(stream, marker.end())
            documents.append(document)
        except ValueError:
            continue

    formats, title = [], ''
    for document in documents:
        for item in dictionaries(document):
            if item.get('rawSnapId') == requested_id:
                url = item.get('videoUrl')
            elif item.get('@type') == 'VideoObject':
                identity = item.get('url')
                parsed = urlparse(identity) if isinstance(identity, str) else None
                if (parsed is None or parsed.hostname not in {'snapchat.com', 'www.snapchat.com'}
                        or not re.fullmatch(r'/(?:@[^/]+/)?spotlight/' + re.escape(requested_id) + r'/?', parsed.path)):
                    continue
                url = item.get('contentUrl')
            else:
                continue
            if not isinstance(url, str) or not url.startswith('https://'):
                continue
            if any(fmt['url'] == url for fmt in formats):
                continue
            formats.append({'url': url, 'width': item.get('width'), 'height': item.get('height')})
            title = title or str(item.get('caption') or item.get('name') or '')
    if not formats:
        return {}
    formats.sort(key=media_quality, reverse=True)
    return {'id': requested_id, 'url': formats[0]['url'], 'title': title,
            'entries': [{'kind': 'video', 'formats': formats}]}


def instagram_dash_formats(item: dict, media_type: str) -> list[dict]:
    """Read full-object representations from this item's DASH metadata only.

    SegmentBase byte ranges describe a complete object. SegmentTemplate/List
    need a segmented downloader and must never be mistaken for a complete file.
    """
    dash_info = item.get('dash_info') if isinstance(item.get('dash_info'), dict) else {}
    manifests = [item.get('video_dash_manifest'), dash_info.get('video_dash_manifest')]
    candidates = []
    for manifest in manifests:
        if (not isinstance(manifest, str) or len(manifest) > 2 * 1024 * 1024
                or '<!DOCTYPE' in manifest.upper() or '<!ENTITY' in manifest.upper()):
            continue
        try:
            root = ET.fromstring(manifest)
        except ET.ParseError:
            continue
        pending = [(root, {})]
        while pending:
            node, inherited = pending.pop()
            fields = {**inherited, **node.attrib}
            children = list(node)
            tags = [child.tag.rsplit('}', 1)[-1] for child in children]
            if {'SegmentTemplate', 'SegmentList', 'ContentProtection'}.intersection(tags):
                continue
            kind = fields.get('contentType') or fields.get('mimeType', '').split('/')[0]
            if node.tag.rsplit('}', 1)[-1] == 'Representation' and kind == media_type:
                try:
                    fps = float(Fraction(fields.get('frameRate') or '0'))
                except (ValueError, ZeroDivisionError):
                    fps = 0
                for child, tag in zip(children, tags):
                    url = (child.text or '').strip()
                    if tag == 'BaseURL' and url.startswith('https://'):
                        candidates.append({'url': url, 'width': fields.get('width'), 'height': fields.get('height'),
                                           'fps': fps, 'bitrate': fields.get('bandwidth')})
            pending.extend((child, fields) for child in reversed(children))
    unique = {}
    for row in sorted(candidates, key=media_quality, reverse=True):
        unique.setdefault(row['url'], row)
    return list(unique.values())


def instagram_dash_audio_urls(item: dict) -> list[str]:
    return [row['url'] for row in instagram_dash_formats(item, 'audio')]


def instagram_audio_urls(item: dict, *, include_children: bool = True, include_dash: bool = True) -> list[str]:
    urls = instagram_dash_audio_urls(item) if include_dash else []
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
    # Carousel children may own the music metadata even when the parent does
    # not. Do not walk the whole response: it may include recommended posts.
    for child in (item.get("carousel_media") or []) if include_children else []:
        if isinstance(child, dict):
            urls.extend(instagram_audio_urls(child))
    for edge in ((item.get("edge_sidecar_to_children") or {}).get("edges") or []) if include_children else []:
        child = edge.get("node") if isinstance(edge, dict) else None
        if isinstance(child, dict):
            urls.extend(instagram_audio_urls(child))
    return list(dict.fromkeys(urls))


def pinterest_post_audio_urls(pin: dict) -> list[str]:
    """Read only music attached to this pin, never related-pin metadata."""
    urls = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"audio_url", "audio_src", "progressive_download_url"}:
                    if isinstance(value, str) and value.startswith("https://"):
                        urls.append(value)
                elif key == "url" and isinstance(value, str) and value.startswith("https://"):
                    # Music objects often contain artwork URLs too.
                    path = value.split("?", 1)[0].lower()
                    if path.endswith((".m4a", ".mp3", ".aac", ".ogg", ".opus", ".wav")):
                        urls.append(value)
                elif isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    # Only a post-wide soundtrack can safely be applied to every silent slot.
    for key in ("audio", "music", "music_metadata", "soundtrack", "sound"):
        value = pin.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            urls.append(value)
        else:
            walk(value)
    return list(dict.fromkeys(urls))


def instagram_extractor(downloader):
    """Extend the pinned parth parser without changing its network behavior."""
    from parth_dl.extractors import MediaExtractor

    def with_api_video(child):
        if not child.get('video_versions'):
            if formats := instagram_dash_formats(child, 'video'):
                return {**child, 'video_versions': formats}
        return child

    def with_graphql_video(child):
        if not child.get('video_url'):
            if formats := instagram_dash_formats(child, 'video'):
                best = formats[0]
                return {**child, 'is_video': True, 'video_url': best['url'],
                        'dimensions': {'width': best['width'], 'height': best['height']}}
        return child

    class CompleteMediaExtractor(MediaExtractor):
        def _parse_media_item(self, item):
            children = item.get('carousel_media') or []
            prepared = ({**item, 'carousel_media': [with_api_video(child) for child in children]}
                        if children else with_api_video(item))
            result = super()._parse_media_item(prepared)
            expected = max(int(item.get("carousel_media_count") or 0), len(item.get("carousel_media") or [item]))
            if len(result.get("entries") or []) != expected:
                raise RuntimeError("Incomplete Instagram carousel metadata")
            result["audio_urls"] = instagram_audio_urls(item, include_children=False, include_dash=False)
            for entry, child in zip(result['entries'], children or [item]):
                if entry['kind'] == 'image' and child.get('media_type') == 2:
                    raise RuntimeError('Incomplete Instagram video metadata')
                entry['audio_urls'] = (instagram_audio_urls(child, include_children=False) if children
                                       else instagram_dash_audio_urls(child))
                if entry['kind'] == 'video':
                    entry['formats'].extend(instagram_dash_formats(child, 'video'))
            return result

        def _parse_graphql_media(self, item):
            sidecar = item.get("edge_sidecar_to_children") or {}
            edges = sidecar.get('edges') or []
            prepared = with_graphql_video(item)
            if edges:
                prepared = {**item, 'edge_sidecar_to_children': {
                    **sidecar, 'edges': [{**edge, 'node': with_graphql_video(edge.get('node') or {})}
                                         for edge in edges]}}
            result = super()._parse_graphql_media(prepared)
            expected = max(int(sidecar.get("count") or 0), len(sidecar.get("edges") or [item]))
            if len(result.get("entries") or []) != expected:
                raise RuntimeError("Incomplete Instagram carousel metadata")
            result["audio_urls"] = instagram_audio_urls(item, include_children=False, include_dash=False)
            for entry, child in zip(result['entries'], [edge.get('node') or {} for edge in edges] or [item]):
                entry['audio_urls'] = (instagram_audio_urls(child, include_children=False) if edges
                                       else instagram_dash_audio_urls(child))
                if entry['kind'] == 'video':
                    entry['formats'].extend(instagram_dash_formats(child, 'video'))
                elif entry['kind'] == 'image':
                    entry['formats'].extend({'url': row['src'], 'width': row.get('config_width'),
                                             'height': row.get('config_height')}
                                            for row in (child.get('display_resources') or [])
                                            if isinstance(row, dict) and row.get('src'))
            return result

    # Preserve the existing extractor's rate limiter and request configuration.
    downloader.media_extractor.__class__ = CompleteMediaExtractor
    return downloader


def pinterest_entries(pin: dict) -> list[dict]:
    """Build the complete ordered list from one pin, with video taking priority."""
    def media(node):
        video = node.get("videos") or node.get("video")
        if video:
            formats = [v for v in (video.get('video_list') or {}).values()
                       if isinstance(v, dict) and v.get("url")]
            if not formats:
                raise RuntimeError("Incomplete Pinterest video metadata; poster rejected")
            formats.sort(key=media_quality, reverse=True)
            return {'kind': 'video', 'formats': formats,
                    'audio_urls': pinterest_post_audio_urls(node) if node is not pin else []}
        if node.get("is_video") or node.get("type") == "story_pin_video_block":
            raise RuntimeError("Incomplete Pinterest video metadata; poster rejected")
        images = node.get("images") or (node.get("image") or {}).get("images") or {}
        if node.get("audio") and (node.get("type") == "story_pin_music_block" or
                                  not images and not node.get("image_signature")):
            audio = node["audio"]
            audio_url = audio.get('audio_url') or audio.get('url') if isinstance(audio, dict) else audio
            return {'kind': 'audio', 'formats': [{'url': audio_url}]}
        original = [v for key, v in images.items() if key in {'orig', 'original', 'originals'}
                    and isinstance(v, dict) and v.get('url')]
        formats = sorted([v for v in images.values() if isinstance(v, dict) and v.get('url')],
                         key=media_quality, reverse=True)
        if not original:
            sig = node.get("image_signature")
            if isinstance(sig, str) and re.fullmatch(r'[a-fA-F0-9]{6,128}', sig):
                base = f"https://i.pinimg.com/originals/{sig[:2]}/{sig[2:4]}/{sig[4:6]}/{sig}"
                original = [{'url': base + ext} for ext in ('.jpg', '.png', '.webp', '.gif')]
            elif formats:
                best = urlparse(formats[0]['url'])
                if best.hostname == 'i.pinimg.com' and re.match(r'^/\d+x(?:\d+)?/', best.path):
                    path = re.sub(r'^/\d+x(?:\d+)?/', '/originals/', best.path)
                    original = [{'url': urlunparse(best._replace(path=path))}]
        if not (formats or original):
            raise RuntimeError("Incomplete Pinterest image metadata")
        return {'kind': 'image', 'formats': list({v['url']: v for v in original + formats}.values())}

    post_music = [{"kind": "audio", "scope": "post", "formats": [{"url": url}]}
                  for url in pinterest_post_audio_urls(pin)]
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
        used = {fmt.get("url") for entry in result if entry["kind"] == "audio"
                for fmt in entry["formats"]}
        return result + [entry for entry in post_music if entry["formats"][0]["url"] not in used]
    if pin.get("carousel_data"):
        slots = pin["carousel_data"].get("carousel_slots") or []
        if not slots:
            raise RuntimeError("Incomplete Pinterest carousel metadata")
        return [media(slot) for slot in slots] + post_music
    item = media(pin)
    primary_urls = {fmt.get('url') for fmt in item['formats']}
    return [item] + [entry for entry in post_music if entry['formats'][0]['url'] not in primary_urls]


def find_pinterest_pin(document, pin_id: str) -> dict:
    candidates = [node for node in dictionaries(document) if str(node.get('id')) == pin_id and
                  any(node.get(k) for k in ('images', 'videos', 'carousel_data', 'story_pin_data', 'audio'))]
    return max(candidates, key=lambda p: (bool(p.get("story_pin_data") or p.get("carousel_data")),
                                         bool(p.get("videos")), len(p)), default={})


def pinterest_relay_pin(page: str, pin_id: str) -> dict:
    """Adapt the exact Pin query embedded in the current public web page."""
    def media(node):
        video = node.get('videos') or node.get('video')
        return {'images': {key.removeprefix('images_'): value for key, value in node.items()
                           if key.startswith('images_') and isinstance(value, dict)},
                'videos': {'video_list': video.get('videoList')} if video else None,
                'image_signature': node.get('imageSignature')}

    def story_detail(story):
        return sum(len(block) for page in (story.get('pages') or []) for block in (page.get('blocks') or []))

    block_types = {'StoryPinImageBlock': 'story_pin_image_block',
                   'StoryPinVideoBlock': 'story_pin_video_block',
                   'StoryPinMusicBlock': 'story_pin_music_block',
                   'StoryPinParagraphBlock': 'story_pin_paragraph_block',
                   'StoryPinProductStickerBlock': 'story_pin_product_sticker_block',
                   'StoryPinStaticStickerBlock': 'story_pin_static_sticker_block'}
    decoder = json.JSONDecoder()
    comma = re.compile(r'\s*,\s*')
    exact = {}
    for match in re.finditer(r'\bwindow\.__PWS_RELAY_REGISTER_COMPLETED_REQUEST__\(\s*', page):
        try:
            query, end = decoder.raw_decode(page, match.end())
            separator = comma.match(page, end)
            if not isinstance(query, str) or not separator:
                continue
            response, _ = decoder.raw_decode(page, separator.end())
            pin = response['data']['v3GetPinQueryv2']['data']
        except (ValueError, TypeError, KeyError):
            continue
        if (not isinstance(pin, dict) or str(pin.get('entityId')) != pin_id
                or pin.get('__typename') != 'Pin'):
            continue
        if pin.get('isViewerRestricted') or pin.get('carouselData'):
            # Relay carousel slots can contain covers without their media types.
            return {}
        for key, value in pin.items():
            if value is not None and (key != 'storyPinData' or story_detail(value) >= story_detail(exact.get(key) or {})):
                exact[key] = value

    if not exact:
        return {}
    result = {'id': pin_id, **media(exact), 'is_video': bool(exact.get('videoStatus'))}
    if story := exact.get('storyPinData'):
        pages = []
        for page_data in story.get('pages') or []:
            blocks = []
            for block in page_data.get('blocks') or []:
                kind = block_types.get(block.get('__typename'))
                if not kind:
                    raise RuntimeError('Incomplete Pinterest story block metadata')
                audio = block.get('audio') or block.get('audioUrl')
                if kind == 'story_pin_music_block' and not audio:
                    raise RuntimeError('Incomplete Pinterest story music metadata')
                blocks.append({'type': kind, **media(block), 'audio': audio})
            pages.append({'blocks': blocks})
        result['story_pin_data'] = {'pages': pages}
    return result
