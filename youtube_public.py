"""Bounded anonymous player fallback; downloads still use yt-dlp's pipeline.

The visitor handshake follows pytubefix's public VISION_OS client. If the WEB
player bootstrap is unavailable, the public watch page can supply visitorData.
No account, API key, age confirmation or third-party download service is used.
"""
from __future__ import annotations

import re
import json
from urllib.parse import urlparse

import httpx
from yt_dlp.utils import mimetype2ext, parse_codecs


PLAYER_URL = 'https://www.youtube.com/youtubei/v1/player?prettyPrint=false'
PLAYER_ENDPOINTS = (PLAYER_URL, 'https://youtubei.googleapis.com/youtubei/v1/player?prettyPrint=false')
SAFARI_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) '
             'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15')
MAX_RESPONSE = 4 * 1024 * 1024


def response_bytes(response) -> bytes:
    data = bytearray()
    for chunk in response.iter_bytes(65536):
        data.extend(chunk)
        if len(data) > MAX_RESPONSE:
            raise RuntimeError('YouTube response exceeds metadata limit')
    return bytes(data)


def player_request(client, video_id: str, context: dict, client_id: str, endpoint=PLAYER_URL) -> dict:
    headers = {'Content-Type': 'application/json', 'User-Agent': SAFARI_UA,
               'Accept-Language': 'en-US,en', 'X-Youtube-Client-Name': client_id,
               'X-Youtube-Client-Version': context['clientVersion']}
    if context.get('visitorData'):
        headers['X-Goog-Visitor-Id'] = context['visitorData']
    with client.stream('POST', endpoint, headers=headers,
                       json={'context': {'client': context}, 'videoId': video_id}) as response:
        data = response_bytes(response)
        if not response.is_success:
            try:
                detail = str(json.loads(data).get('error', {}).get('message') or '')
            except (ValueError, AttributeError):
                detail = ''
            detail = re.sub(r'https?://\S+', '[url]', detail)[:220]
            raise httpx.HTTPStatusError(f'YouTube {context["clientName"]} player HTTP{response.status_code}: {detail}',
                                        request=response.request, response=response)
        result = json.loads(data)
    if not isinstance(result, dict):
        raise RuntimeError('Invalid YouTube player response')
    return result


def watch_visitor(client, video_id: str) -> str:
    """Read only the guest context, never execute scripts or follow page links."""
    with client.stream('GET', f'https://www.youtube.com/watch?v={video_id}',
                       headers={'User-Agent': SAFARI_UA, 'Accept-Language': 'en-US,en'}) as response:
        response.raise_for_status()
        if 'text/html' not in response.headers.get('content-type', '').lower():
            raise RuntimeError('YouTube watch page returned invalid metadata')
        page = response_bytes(response).decode('utf-8', errors='replace')
    decoder = json.JSONDecoder()
    # ytcfg.set also has calls with non-JSON arguments. Only decode an object.
    for match in re.finditer(r'\bytcfg\.set\s*\(\s*(?=\{)', page):
        try:
            config, _ = decoder.raw_decode(page, match.end())
        except ValueError:
            continue
        context = config.get('INNERTUBE_CONTEXT') or {}
        value = config.get('VISITOR_DATA') or (context.get('client') or {}).get('visitorData')
        if isinstance(value, str) and 0 < len(value) <= 4096 and value.isascii() and value.isprintable():
            return value
    return ''


def visitor_data(document: dict) -> str:
    context = document.get('responseContext') or {}
    value = context.get('visitorData')
    if isinstance(value, str):
        return value
    for service in context.get('serviceTrackingParams') or []:
        for row in service.get('params') or []:
            if row.get('key') == 'visitor_data' and isinstance(row.get('value'), str):
                return row['value']
    return ''


def player_manifest(document: dict, video_id: str, *, safe_url) -> dict:
    status = document.get('playabilityStatus') or {}
    if status.get('status') != 'OK':
        # Keep platform errors recognizable by the existing failure classifier.
        raise RuntimeError('YouTube: ' + str(status.get('reason') or status.get('status') or 'unavailable'))
    details = document.get('videoDetails') or {}
    microformat = (document.get('microformat') or {}).get('playerMicroformatRenderer') or {}
    if details.get('videoId') != video_id:
        raise RuntimeError('YouTube returned a different video')
    if microformat.get('isFamilySafe') is False:
        raise RuntimeError('Age-restricted media is not supported')
    if details.get('isLive') or (microformat.get('liveBroadcastDetails') or {}).get('isLiveNow'):
        raise RuntimeError('YouTube live stream has not ended')
    formats = []
    streaming = document.get('streamingData') or {}
    for row in [*(streaming.get('formats') or []), *(streaming.get('adaptiveFormats') or [])]:
        url = row.get('url')
        parsed = urlparse(url) if isinstance(url, str) else None
        if (parsed is None or parsed.scheme != 'https' or parsed.username or parsed.password
                or not (parsed.hostname or '').endswith('.googlevideo.com') or not safe_url(url)
                or row.get('drmFamilies') or row.get('drmTrackType')
                or row.get('type') == 'FORMAT_STREAM_TYPE_OTF'):
            continue
        mime = row.get('mimeType') or ''
        codecs = re.search(r'codecs="([^"]+)"', mime)
        if not codecs:
            continue
        fmt = {'format_id': 'public-' + str(row.get('itag')), 'url': url,
               'ext': mimetype2ext(mime.split(';')[0]), **parse_codecs(codecs[1]),
               'width': row.get('width'), 'height': row.get('height'), 'fps': row.get('fps'),
               'http_headers': {'User-Agent': SAFARI_UA, 'Referer': 'https://www.youtube.com/'},
               'downloader_options': {'http_chunk_size': 9 * 1024 * 1024}}
        for source, target, scale in [('contentLength', 'filesize', 1), ('bitrate', 'tbr', 1000),
                                       ('audioSampleRate', 'asr', 1)]:
            try:
                fmt[target] = int(row[source]) / scale
            except (KeyError, ValueError, TypeError):
                pass
        track = row.get('audioTrack') or {}
        if track:
            fmt['language'] = str(track.get('id') or '').split('.')[0]
            fmt['language_preference'] = 10 if track.get('audioIsDefault') else -10
        formats.append(fmt)
    if not formats:
        raise RuntimeError('YouTube returned no accessible download formats')
    return {'id': video_id, 'title': str(details.get('title') or video_id),
            'duration': float(details.get('lengthSeconds') or 0), 'formats': formats,
            'webpage_url': f'https://www.youtube.com/watch?v={video_id}',
            'extractor': 'youtube_public', 'extractor_key': 'Youtube', 'age_limit': 0}


def request_manifest(video_id: str, *, safe_url, proxy=None, client_factory=httpx.Client) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('Invalid YouTube video ID')
    with client_factory(timeout=12, follow_redirects=False, proxy=proxy, trust_env=False) as client:
        visitor = ''
        watch_checked = False
        for index, endpoint in enumerate(PLAYER_ENDPOINTS):
            try:
                if not visitor:
                    try:
                        seed = player_request(client, video_id, {
                            'clientName': 'WEB', 'clientVersion': '2.20260708.00.00',
                            'osName': 'Windows', 'osVersion': '10.0', 'platform': 'DESKTOP'}, '1', endpoint)
                        visitor = visitor_data(seed)
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code not in {403, 404}:
                            raise
                        if watch_checked:
                            raise
                        watch_checked = True
                        visitor = watch_visitor(client, video_id)
                        if not visitor:
                            raise exc
                    if not visitor and not watch_checked:
                        watch_checked = True
                        visitor = watch_visitor(client, video_id)
                context = {'clientName': 'VISIONOS', 'clientVersion': '1.02', 'deviceMake': 'Apple',
                           'platform': 'MOBILE', 'osName': 'visionOS', 'osVersion': '26.5.23O471',
                           'deviceModel': 'RealityDevice17,1'}
                if visitor:
                    context['visitorData'] = visitor
                document = player_request(client, video_id, context, '101', endpoint)
                break
            except httpx.HTTPStatusError as exc:
                # The web front door can reject a request before it reaches the
                # player API. Try Google's fixed API origin once, never a proxy
                # or an arbitrary redirect, and never retry a rate limit.
                if exc.response.status_code not in {403, 404} or index == len(PLAYER_ENDPOINTS) - 1:
                    raise
    return player_manifest(document, video_id, safe_url=safe_url)
