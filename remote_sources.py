"""Opt-in hosted extractors. Credentials stay on the API request, never the CDN.

No shared public instances or third-party cookies are used. Setting an API key
enables that provider's billable fallback; normal downloads stay local first.
"""
from __future__ import annotations

import json
import os
import re
import time

import httpx


class RemoteSourceError(RuntimeError):
    pass


def configured(platform: str) -> bool:
    return bool(os.getenv({'youtube': 'TUNELIO_API_KEY',
                           'snapchat': 'APIFY_TOKEN'}.get(platform, '_NO_PROVIDER'), '').strip())


def _json(client, method, url, **kwargs):
    """Bound JSON and sanitize failures, including URLs containing signed tokens."""
    try:
        with client.stream(method, url, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                raise RemoteSourceError(f'Remote provider HTTP {response.status_code}')
            payload = bytearray()
            for chunk in response.iter_bytes():
                payload.extend(chunk)
                if len(payload) > 1024 * 1024:
                    raise RemoteSourceError('Remote provider metadata limit exceeded')
        return json.loads(payload)
    except RemoteSourceError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        # Do not include provider payloads/exception URLs in logs or Telegram.
        raise RemoteSourceError('Remote provider request failed') from None


def _tunelio(client, url, mode):
    headers = {'Authorization': 'Bearer ' + os.environ['TUNELIO_API_KEY'].strip()}
    if mode.startswith('mp3') or mode in {'m4a', 'opus'}:
        quality = 'm4a'  # delivery converts to the requested MP3 once
    else:
        info = _json(client, 'GET', 'https://tunelio.dev/info',
                     params={'url': url}, headers=headers)
        formats = info.get('formats') if isinstance(info, dict) else None
        heights = []
        for fmt in formats or []:
            match = re.fullmatch(r'(\d+)p', str(fmt.get('quality', '')))
            if match:
                heights.append(int(match[1]))
        ceiling = int(mode) if mode.isdigit() else 10000
        supported = [h for h in heights if h <= ceiling and h in {144, 240, 360, 480, 720, 1080, 2160}]
        if not supported:
            raise RemoteSourceError('Remote provider has no requested video format')
        quality = f'{max(supported)}p'
    data = _json(client, 'GET', 'https://tunelio.dev/create',
                 params={'url': url, 'quality': quality}, headers=headers)
    if not isinstance(data, dict) or data.get('status') != 'ok' or not data.get('url'):
        raise RemoteSourceError('Remote provider did not resolve media')
    return [{'url': data['url'], 'kind': 'audio' if quality == 'm4a' else 'video',
             'requires_audio': True}]


def _apify(client, url, mode):
    headers = {'Authorization': 'Bearer ' + os.environ['APIFY_TOKEN'].strip()}
    base = 'https://api.apify.com/v2'
    # Start once. Poll the same run instead of duplicating charged jobs.
    response = _json(client, 'POST', base + '/acts/convertfleetdotonline~snapchat-downloader/runs',
                     params={'timeout': 120, 'memory': 256, 'maxTotalChargeUsd': '0.02'},
                     json={'videoUrls': [url]}, headers=headers)
    run = response.get('data', {}) if isinstance(response, dict) else {}
    run_id = run.get('id', '')
    if not re.fullmatch(r'[A-Za-z0-9]+', run_id):
        raise RemoteSourceError('Remote provider did not create a run')
    deadline = time.monotonic() + 135
    while run.get('status') in {'READY', 'RUNNING'}:
        if time.monotonic() >= deadline:
            raise RemoteSourceError('Remote provider timeout')
        response = _json(client, 'GET', base + f'/actor-runs/{run_id}',
                         params={'waitForFinish': 10}, headers=headers)
        run = response.get('data', {}) if isinstance(response, dict) else {}
    dataset = run.get('defaultDatasetId', '')
    if run.get('status') != 'SUCCEEDED' or not re.fullmatch(r'[A-Za-z0-9]+', dataset):
        raise RemoteSourceError('Remote provider run failed')
    records = _json(client, 'GET', base + f'/datasets/{dataset}/items',
                    params={'clean': 'true'}, headers=headers)
    if not isinstance(records, list) or not records:
        raise RemoteSourceError('Remote provider returned no media')
    entries = []
    for record in records:
        if not isinstance(record, dict) or record.get('error'):
            raise RemoteSourceError('Incomplete remote collection')
        audio = mode.startswith('mp3') or mode in {'m4a', 'opus'}
        formats = [f for f in record.get('formats', []) if isinstance(f, dict) and f.get('url')]
        if audio and record.get('audio_url'):
            entries.append({'url': record['audio_url'], 'kind': 'audio', 'requires_audio': True})
            continue
        videos = [f for f in formats if f.get('type') == 'video' and f.get('ext') == 'mp4']
        ceiling = int(mode) if mode.isdigit() else 10000
        videos = [f for f in videos if int(f.get('height') or 0) <= ceiling]
        if not videos:
            raise RemoteSourceError('Incomplete remote collection: no video format')
        # Prefer a combined stream, then the highest resolution within the cap.
        selected = max(videos, key=lambda f: (f.get('has_audio') is True, int(f.get('height') or 0)))
        entries.append({'url': selected['url'], 'kind': 'video',
                        'audio_url': record.get('audio_url'),
                        'requires_audio': audio or selected.get('has_audio') is True or bool(record.get('audio_url'))})
    return entries


def resolve(platform: str, url: str, mode: str, *, proxy=None):
    if not configured(platform):
        return []
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False, proxy=proxy) as client:
        return _tunelio(client, url, mode) if platform == 'youtube' else _apify(client, url, mode)
