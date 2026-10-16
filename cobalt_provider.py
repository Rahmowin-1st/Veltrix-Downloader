"""Optional Cobalt API adapter for an owner-operated or authorized instance.

No public instance is assumed. API credentials are used only for the manifest
request; media transfers use the normal bounded, independently validated path.
"""
from __future__ import annotations

import json
import os
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx


class ProviderError(RuntimeError):
    pass


def _public_https(url, safe_url):
    if not isinstance(url, str) or len(url) > 16384:
        return False
    try:
        parsed = urlparse(url)
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and not parsed.fragment and safe_url(url))
    except ValueError:
        return False


def parse_manifest(payload, *, safe_url, max_items, audio_only=False):
    """Validate the whole response before downloading any item, in source order."""
    if not isinstance(payload, dict):
        raise ProviderError('Provider returned invalid metadata')
    status = payload.get('status')
    if status == 'error':
        # Neither provider context nor its arbitrary error text is safe to log.
        raise ProviderError('Provider could not retrieve this post')
    if status in {'tunnel', 'redirect'}:
        suffix = PurePosixPath(str(payload.get('filename') or '')).suffix.lower()
        kinds = {'.jpg': 'image', '.jpeg': 'image', '.png': 'image', '.webp': 'image',
                 '.avif': 'image', '.gif': 'animation', '.mp4': 'video', '.mkv': 'video',
                 '.mov': 'video', '.webm': 'video', '.m4a': 'audio', '.mp3': 'audio',
                 '.ogg': 'audio', '.opus': 'audio', '.wav': 'audio', '.flac': 'audio'}
        kind = ('audio' if audio_only and suffix in {'.mp4', '.mkv', '.webm'} else kinds.get(suffix))
        if not kind:
            raise ProviderError('Provider returned an unsupported media type')
        rows = [{'kind': kind, 'url': payload.get('url')}]
    elif status == 'picker':
        items = payload.get('picker')
        if not isinstance(items, list) or not items:
            raise ProviderError('Provider returned empty media metadata')
        if len(items) > max_items:
            raise ProviderError('Post exceeds configured item limit')
        rows = []
        for item in items:
            if not isinstance(item, dict):
                raise ProviderError('Provider returned incomplete post metadata')
            kind = {'photo': 'image', 'video': 'video', 'gif': 'animation'}.get(item.get('type'))
            if not kind:
                raise ProviderError('Provider returned incomplete post metadata')
            rows.append({'kind': kind, 'url': item.get('url')})
        if payload.get('audio'):
            rows.append({'kind': 'audio', 'scope': 'post', 'url': payload['audio']})
    else:
        # local-processing should not occur with localProcessing=disabled. Do
        # not mistake its separate audio/video tunnels for complete media.
        raise ProviderError('Provider returned an unsupported response')
    if len(rows) > max_items:
        raise ProviderError('Post exceeds configured item limit')
    entries = []
    for row in rows:
        if not _public_https(row['url'], safe_url):
            raise ProviderError('Provider returned an unsafe media destination')
        entries.append({'kind': row['kind'], 'scope': row.get('scope'),
                        'formats': [{'url': row['url']}]})
    return entries


def request_manifest(url, *, audio_only, quality, safe_url, max_items,
                     proxy=None, client_factory=httpx.Client):
    endpoint = os.getenv('COBALT_API_URL', '').strip()
    if not endpoint:
        return []
    if not _public_https(endpoint, safe_url) or urlparse(endpoint).query:
        raise ProviderError('Provider endpoint must be a public HTTPS URL without credentials or query')
    if not _public_https(url, safe_url):
        raise ProviderError('Unsafe source link')
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    key = os.getenv('COBALT_API_KEY', '').strip()
    if key:
        headers['Authorization'] = 'Api-Key ' + key
    body = {'url': url, 'downloadMode': 'audio' if audio_only else 'auto',
            'videoQuality': quality, 'audioFormat': 'best', 'audioBitrate': '320',
            'localProcessing': 'disabled', 'alwaysProxy': True}
    try:
        with client_factory(timeout=45, follow_redirects=False, trust_env=False,
                            proxy=proxy) as client:
            with client.stream('POST', endpoint, headers=headers, json=body) as response:
                if response.status_code == 429:
                    raise ProviderError('Provider rate limit; retry later')
                if response.status_code in {401, 403}:
                    raise ProviderError('Provider requires authorized API access')
                if response.status_code != 200:
                    raise ProviderError('Provider request failed')
                if 'application/json' not in response.headers.get('content-type', '').lower():
                    raise ProviderError('Provider returned invalid metadata')
                chunks = bytearray()
                for chunk in response.iter_bytes(65536):
                    chunks.extend(chunk)
                    if len(chunks) > 1024 * 1024:
                        raise ProviderError('Provider metadata size exceeded')
        payload = json.loads(chunks)
    except (httpx.HTTPError, ValueError):
        # URLs and API keys must not escape through upstream exception strings.
        raise ProviderError('Provider metadata request failed') from None
    return parse_manifest(payload, safe_url=safe_url, max_items=max_items, audio_only=audio_only)
