"""Download one public YouTube video through the configured VidFetch account."""
import json
import logging
import math
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from cobalt_provider import ProviderError
from media_io import media_kind, probe
from source_metadata import MediaTooLong
from transfer import TransferLimit, download

API = 'https://api.vidfetch.dev/v1'
AGENT = 'VeltrixDownloader/1.0'
MAX_WAIT = 240
# Long videos are processed on the provider before delivery starts.
LONG_WAIT_LIMIT = 2400
LOG = logging.getLogger('veltrix.provider')
VIDEO_QUALITIES = ('2160p', '1440p', '1080p', '720p', '480p', '360p', '240p', '144p')
# Conservative MP4 sizes when /info does not report one for a tier (kbit/s).
QUALITY_KBPS = {'2160p': 20000, '1440p': 10000, '1080p': 5000, '720p': 2700,
                '480p': 1300, '360p': 800, '240p': 450, '144p': 250}
SIZE_KEYS = ('size_bytes', 'filesize', 'file_size', 'size', 'bytes', 'approx_size_bytes')


class VidFetchQuotaExceeded(ProviderError):
    """VidFetch rejected the request before creating a download job."""


def request_json(client, method, route, *, body=None):
    with client.stream(method, API + route, json=body) as response:
        if method == 'POST' and route == '/downloads' and response.status_code == 402:
            raise VidFetchQuotaExceeded('VidFetch create HTTP 402 (download quota)')
        if not response.is_success:
            stage = 'info' if route == '/info' else 'create' if method == 'POST' else 'status'
            raise ProviderError(f'VidFetch {stage} HTTP {response.status_code}')
        if 'application/json' not in response.headers.get('content-type', '').lower():
            raise ProviderError('VidFetch returned invalid metadata')
        raw = bytearray()
        for chunk in response.iter_bytes(65536):
            raw.extend(chunk)
            if len(raw) > 1024 * 1024:
                raise ProviderError('VidFetch metadata exceeds the response limit')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ProviderError('VidFetch returned invalid metadata')
    return data


def _number(value) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return int(number) if math.isfinite(number) and number > 0 else 0


def quality_sizes(info) -> dict:
    """Per-quality byte sizes from /info, whatever shape the listing takes."""
    found = {}

    def size_of(node):
        if isinstance(node, dict):
            return next((size for size in (_number(node.get(key)) for key in SIZE_KEYS) if size), 0)
        return _number(node)

    def visit(node, depth=0):
        if depth > 5:
            return
        if isinstance(node, dict):
            label = str(node.get('format') or node.get('quality') or node.get('label') or '').lower()
            if label in QUALITY_KBPS and size_of(node):
                found.setdefault(label, size_of(node))
            for key, value in node.items():
                key = str(key).lower()
                if key in QUALITY_KBPS and size_of(value):
                    found.setdefault(key, size_of(value))
                elif isinstance(value, (dict, list)):
                    visit(value, depth + 1)
        elif isinstance(node, list):
            for value in node[:64]:
                visit(value, depth + 1)

    visit(info)
    return found


def choose_quality(quality, duration, budget_bytes, sizes):
    """Highest tier at or below ``quality`` whose size fits ``budget_bytes``."""
    if quality == 'mp3' or not budget_bytes:
        return quality
    order = VIDEO_QUALITIES[VIDEO_QUALITIES.index(quality):]
    # Below 360p a long video is no longer worth watching: accept more parts.
    order = [tier for tier in order if int(tier[:-1]) >= 360] or order
    for tier in order:
        size = sizes.get(tier) or QUALITY_KBPS[tier] * 125 * max(0.0, duration)
        if size <= budget_bytes:
            return tier
    return order[-1]


def create_job(client, url, quality):
    qualities = ('mp3',) if quality == 'mp3' else VIDEO_QUALITIES[VIDEO_QUALITIES.index(quality):]
    for requested in qualities:
        try:
            job = request_json(client, 'POST', '/downloads', body={
                'url': url, 'format': requested, 'destination': 'url',
            })
            return job, requested
        except VidFetchQuotaExceeded:
            if requested == qualities[-1]:
                raise


def await_file(client, job, route, job_id, url, video_id, *, clock, sleep, wait=MAX_WAIT, status=None):
    deadline = clock() + wait
    while True:
        if (job.get('id') != job_id or job.get('url') != url
                or job.get('video_id') not in (None, video_id) or job.get('trim') is not None):
            raise ProviderError('VidFetch returned a different video or job')
        state = job.get('status')
        if state == 'completed':
            return job
        if state not in ('queued', 'processing', 'downloading', 'uploading'):
            raise ProviderError('VidFetch could not finish the video')
        if status is not None:
            status(state, job.get('progress'))
        if clock() + 6 >= deadline:
            raise ProviderError('VidFetch job timed out')
        sleep(6)
        job = request_json(client, 'GET', route)


def fetch(video_id: str, quality: str, folder: Path, *, api_key, safe_url, budget, max_bytes,
          proxy=None, client_factory=httpx.Client, clock=time.monotonic, sleep=time.sleep,
          budget_bytes=None, on_info=None, progress=None, status=None) -> list[Path]:
    if (not api_key or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id)
            or quality not in (*VIDEO_QUALITIES, 'mp3')):
        return []
    url = 'https://www.youtube.com/watch?v=' + video_id
    audio = quality == 'mp3'
    out = folder / (video_id + ('.mp3' if audio else '.mp4'))
    clean_headers = {'User-Agent': AGENT, 'Accept': '*/*'}
    api_headers = {**clean_headers, 'Accept': 'application/json', 'Authorization': 'Bearer ' + api_key}

    def allowed_file(target):
        parts = urlsplit(target)
        return (parts.scheme == 'https' and not parts.username and not parts.password
                and parts.port in (None, 443) and not parts.fragment
                and (parts.hostname or '').endswith('.vidfetch.dev') and safe_url(target))

    try:
        with client_factory(headers=api_headers, follow_redirects=False, trust_env=False,
                            proxy=proxy, timeout=20) as client:
            info = request_json(client, 'POST', '/info', body={'url': url})
            duration = float(info.get('duration') or 0)
            if (info.get('id') != video_id or not math.isfinite(duration) or duration <= 0
                    or info.get('is_live') or info.get('is_private') or info.get('is_unlisted')
                    or int(info.get('age_limit') or 0) >= 18 or info.get('availability') not in (None, 'public')):
                raise ProviderError('VidFetch could not verify this public video')
            if on_info is not None:
                on_info(info)
            # /info is free: use its sizes so delivery and quota stay bounded.
            quality = choose_quality(quality, duration, budget_bytes, quality_sizes(info))
            job, accepted_quality = create_job(client, url, quality)
            job_id = job.get('id') or ''
            if not re.fullmatch(r'dl_[A-Za-z0-9_-]{1,96}', job_id) or job.get('url') != url:
                raise ProviderError('VidFetch returned an invalid download job')
            route = '/downloads/' + job_id
            try:
                wait = min(LONG_WAIT_LIMIT, MAX_WAIT + duration * .3)
                job = await_file(client, job, route, job_id, url, video_id, clock=clock, sleep=sleep,
                                 wait=wait, status=status)
                size = int(job.get('size_bytes') or 0)
                if size > max_bytes:
                    raise TransferLimit('Source file exceeds configured storage limit')
                file_url = job.get('download_url')
                if not isinstance(file_url, str) or not allowed_file(file_url):
                    raise ProviderError('VidFetch returned an invalid delivery URL')
                if not download(file_url, out, headers=clean_headers, safe_url=allowed_file,
                                budget=budget, max_bytes=max_bytes, proxy=proxy,
                                client_factory=client_factory, timeout=45, attempts=3,
                                progress=progress):
                    raise ProviderError('VidFetch delivery file unavailable')
                document = probe(out)
                length = float(document['format'].get('duration') or 0)
                if (not math.isfinite(length) or abs(duration - length) > max(1.5, min(3, duration * .01))
                        or (size > 0 and out.stat().st_size != size)
                        or media_kind(out) != ('audio' if audio else 'video')
                        or not any(s.get('codec_type') == 'audio' for s in document['streams'])):
                    raise ProviderError('VidFetch returned mismatched media')
            finally:
                # Discard our temporary provider copy once the transfer attempt ends.
                try:
                    with client.stream('DELETE', API + route, timeout=5):
                        pass
                except httpx.HTTPError:
                    pass
        LOG.info('VidFetch YouTube file verified: requested_format=%s accepted_format=%s bytes=%s',
                 quality, accepted_quality, out.stat().st_size)
        return [out]
    except (ProviderError, TransferLimit, MediaTooLong):
        out.unlink(missing_ok=True)
        raise
    except httpx.TimeoutException:
        out.unlink(missing_ok=True)
        raise ProviderError('VidFetch request timed out') from None
    except (ValueError, TypeError, AttributeError, KeyError, httpx.HTTPError, RuntimeError):
        out.unlink(missing_ok=True)
        raise ProviderError('VidFetch could not deliver this video') from None
