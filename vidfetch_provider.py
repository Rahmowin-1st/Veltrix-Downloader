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
from transfer import TransferLimit, download

API = 'https://api.vidfetch.dev/v1'
AGENT = 'VeltrixDownloader/1.0'
MAX_WAIT = 240
LOG = logging.getLogger('veltrix.provider')


def request_json(client, method, route, *, body=None):
    with client.stream(method, API + route, json=body) as response:
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


def await_file(client, job, route, job_id, url, video_id, *, clock, sleep):
    deadline = clock() + MAX_WAIT
    while True:
        if (job.get('id') != job_id or job.get('url') != url
                or job.get('video_id') not in (None, video_id) or job.get('trim') is not None):
            raise ProviderError('VidFetch returned a different video or job')
        state = job.get('status')
        if state == 'completed':
            return job
        if state not in ('queued', 'processing', 'downloading', 'uploading'):
            raise ProviderError('VidFetch could not finish the video')
        if clock() + 6 >= deadline:
            raise ProviderError('VidFetch job timed out')
        sleep(6)
        job = request_json(client, 'GET', route)


def fetch(video_id: str, quality: str, folder: Path, *, api_key, safe_url, budget, max_bytes,
          proxy=None, client_factory=httpx.Client, clock=time.monotonic, sleep=time.sleep) -> list[Path]:
    if not api_key or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id) or quality not in {
            '144p', '240p', '360p', '480p', '720p', '1080p', '1440p', '2160p', 'mp3'}:
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
            job = request_json(client, 'POST', '/downloads', body={
                'url': url, 'format': quality, 'destination': 'url',
            })
            job_id = job.get('id') or ''
            if not re.fullmatch(r'dl_[A-Za-z0-9_-]{1,96}', job_id) or job.get('url') != url:
                raise ProviderError('VidFetch returned an invalid download job')
            route = '/downloads/' + job_id
            try:
                job = await_file(client, job, route, job_id, url, video_id, clock=clock, sleep=sleep)
                size = int(job.get('size_bytes') or 0)
                if size > max_bytes:
                    raise TransferLimit('Source file exceeds configured storage limit')
                file_url = job.get('download_url')
                if not isinstance(file_url, str) or not allowed_file(file_url):
                    raise ProviderError('VidFetch returned an invalid delivery URL')
                if not download(file_url, out, headers=clean_headers, safe_url=allowed_file,
                                budget=budget, max_bytes=max_bytes, proxy=proxy,
                                client_factory=client_factory, timeout=45, attempts=2):
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
        LOG.info('VidFetch YouTube file verified: requested_format=%s bytes=%s', quality, out.stat().st_size)
        return [out]
    except (ProviderError, TransferLimit):
        out.unlink(missing_ok=True)
        raise
    except httpx.TimeoutException:
        out.unlink(missing_ok=True)
        raise ProviderError('VidFetch request timed out') from None
    except (ValueError, TypeError, AttributeError, KeyError, httpx.HTTPError, RuntimeError):
        out.unlink(missing_ok=True)
        raise ProviderError('VidFetch could not deliver this video') from None
