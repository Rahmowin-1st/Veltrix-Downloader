"""Download one public YouTube video through Newisty's documented developer API."""
import json
import logging
import math
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from cobalt_provider import ProviderError
from media_io import media_kind, probe
from transfer import TransferLimit, download

API = 'https://newisty.com/api/video-downloader/'
AGENT = 'VeltrixDownloader/1.0'
LOG = logging.getLogger('veltrix.provider')


class ProviderRateLimit(ProviderError):
    """The public service has used its current allowance."""


def metadata(client, method, route, body=None):
    with client.stream(method, API + route, json=body) as response:
        if response.status_code == 429:
            raise ProviderRateLimit('Public download service rate limit; retry later')
        response.raise_for_status()
        if 'application/json' not in response.headers.get('content-type', '').lower():
            raise ProviderError('Public download service returned invalid metadata')
        raw = bytearray()
        for chunk in response.iter_bytes(65536):
            raw.extend(chunk)
            if len(raw) > 1024 * 1024:
                raise ProviderError('Public download service metadata too large')
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('status') is not True or not isinstance(data.get('data'), dict):
            raise ProviderError('Public download service could not retrieve this video')
        return data['data']


def completed_file(client, job_id, max_bytes, *, clock, sleep):
    # Ten light requests/minute are shared with the final file request.
    route = 'progress/' + job_id
    started = clock()
    while clock() - started < 240:
        sleep(7)
        try:
            status = metadata(client, 'GET', route)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404 and clock() - started < 60:
                continue  # The new job may not be visible to the polling endpoint yet.
            raise
        state = status.get('status')
        if state == 'done':
            if int(status.get('size') or 0) > max_bytes:
                raise TransferLimit('Source file exceeds configured storage limit')
            return API + 'download/' + job_id
        if state not in {'queued', 'starting', 'downloading', 'processing', 'merging'}:
            raise ProviderError('Public download service could not finish the video')
    raise ProviderError('Public download service timed out')


def fetch(video_id: str, quality: str, folder: Path, *, safe_url, budget, max_bytes,
          proxy=None, client_factory=httpx.Client, clock=time.monotonic, sleep=time.sleep) -> list[Path]:
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id) or quality not in {
            'best-mp4', '1080p', '720p', '480p', 'audio-mp3', 'audio-m4a'}:
        return []
    url = 'https://www.youtube.com/watch?v=' + video_id
    audio = quality.startswith('audio-')
    suffix = {'audio-mp3': '.mp3', 'audio-m4a': '.m4a'}.get(quality, '.mp4')
    out = folder / (video_id + suffix)
    headers = {'User-Agent': AGENT, 'Accept': 'application/json'}

    def allowed_file(target):
        parsed = urlsplit(target)
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and not parsed.fragment
                and (parsed.hostname == 'newisty.com' or (parsed.hostname or '').endswith('.googlevideo.com'))
                and safe_url(target))

    try:
        with client_factory(headers=headers, follow_redirects=False, trust_env=False,
                            proxy=proxy, timeout=30) as client:
            info = metadata(client, 'POST', 'info', {'url': url})
            duration = float(info.get('duration') or 0)
            if (info.get('id') != video_id or not math.isfinite(duration) or duration <= 0
                    or info.get('is_live') or info.get('is_private') or info.get('is_unlisted')
                    or int(info.get('age_limit') or 0) >= 18
                    or info.get('availability') not in {None, 'public'}):
                raise ProviderError('Public download service could not verify this video')
            start = metadata(client, 'POST', 'start', {'url': url, 'format': quality})
            mode = start.get('mode', 'queued')
            if mode == 'direct':
                file_url = start.get('download_url') or ''
                if not isinstance(file_url, str) or not re.fullmatch(
                        re.escape(API) + r'go/[A-Za-z0-9_-]{1,256}', file_url):
                    raise ProviderError('Public download service returned an invalid file link')
                if int(start.get('size') or 0) > max_bytes:
                    raise TransferLimit('Source file exceeds configured storage limit')
            elif mode == 'queued':
                job = str(UUID(start.get('job_id') or ''))
                try:
                    file_url = completed_file(client, job, max_bytes, clock=clock, sleep=sleep)
                except ProviderRateLimit:
                    raise
                except Exception:
                    # Release the single active job slot if a job is still pending.
                    try:
                        with client.stream('DELETE', API + 'cancel/' + job, timeout=5):
                            pass
                    except httpx.HTTPError:
                        pass
                    raise
            else:
                raise ProviderError('Public download service returned an unknown delivery mode')
        if not download(file_url, out, headers={'User-Agent': AGENT, 'Accept': '*/*'},
                        safe_url=allowed_file, budget=budget, max_bytes=max_bytes, proxy=proxy,
                        client_factory=client_factory, timeout=45, attempts=1):
            raise ProviderError('Public download service file unavailable')
        actual = probe(out)
        length = float(actual['format'].get('duration') or 0)
        if (not math.isfinite(length) or abs(duration - length) > max(1.5, min(3, duration * .01))
                or media_kind(out) != ('audio' if audio else 'video')):
            raise ProviderError('Public download service returned mismatched media')
        LOG.info('Public YouTube download verified: kind=%s bytes=%s', 'audio' if audio else 'video', out.stat().st_size)
        return [out]
    except (ProviderError, TransferLimit):
        out.unlink(missing_ok=True)
        raise
    except (ValueError, TypeError, AttributeError, KeyError, httpx.HTTPError, RuntimeError):
        out.unlink(missing_ok=True)
        # Do not expose signed links, job IDs or content returned by the provider.
        raise ProviderError('Public download service could not deliver this video') from None
