"""Resume a single public CDN object without joining different versions."""
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx


class TransferLimit(RuntimeError):
    pass


def download(url, out: Path, *, headers, safe_url, budget, max_bytes, proxy=None,
             timeout=45, attempts=4, client_factory=httpx.Client, sleep=time.sleep):
    partial = out.with_name(out.name + '.part')
    partial.unlink(missing_ok=True)
    validator = None
    total_size = None
    try:
        for attempt in range(attempts):
            offset = partial.stat().st_size if partial.exists() and validator else 0
            request_headers = dict(headers, **{'Accept-Encoding': 'identity'})
            if offset:
                request_headers.update({'Range': f'bytes={offset}-', 'If-Range': validator})
            try:
                with client_factory(headers=request_headers, follow_redirects=False, timeout=timeout,
                                    proxy=proxy, trust_env=False) as client:
                    current = url
                    for _ in range(6):
                        if not safe_url(current):
                            raise RuntimeError('Unsafe media destination')
                        with client.stream('GET', current) as response:
                            if response.status_code in {301, 302, 303, 307, 308}:
                                location = response.headers.get('location')
                                if not location:
                                    raise RuntimeError('Invalid media redirect')
                                current = urljoin(current, location)
                                continue
                            response.raise_for_status()
                            content_type = response.headers.get('content-type', '').lower()
                            if content_type.startswith('text/') or any(t in content_type for t in ('json', 'html', 'xml')):
                                raise RuntimeError('Non-media response')
                            if response.headers.get('content-encoding', 'identity') != 'identity':
                                raise RuntimeError('Unexpected encoded media response')
                            etag = response.headers.get('etag', '')
                            new_validator = etag if etag and not etag.startswith('W/') else response.headers.get('last-modified')
                            length = int(response.headers.get('content-length') or 0)
                            if response.status_code == 206:
                                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('content-range', ''))
                                if not match or not offset or int(match[1]) != offset or new_validator != validator:
                                    raise RuntimeError('Invalid media resume response')
                                start, end, size = map(int, match.groups())
                                if end < start or end >= size or (length and length != end - start + 1) or (total_size and size != total_size):
                                    raise RuntimeError('Inconsistent media range')
                                total_size = size
                            else:
                                # A server may ignore Range or replace an expired object.
                                offset = 0
                                total_size = length or None
                                validator = new_validator
                            if total_size and total_size > max_bytes:
                                raise TransferLimit('Source file exceeds configured storage limit')
                            out.parent.mkdir(parents=True, exist_ok=True)
                            written = offset
                            with partial.open('ab' if offset else 'wb') as stream:
                                for chunk in response.iter_bytes(64 * 1024):
                                    if not chunk:
                                        continue
                                    if written == 0 and chunk[:512].lstrip().lower().startswith((b'<!doctype html', b'<html', b'{"error')):
                                        raise RuntimeError('Error page returned as media')
                                    written += len(chunk)
                                    if written > max_bytes:
                                        raise TransferLimit('Source file exceeds configured storage limit')
                                    budget({})
                                    stream.write(chunk)
                            if not written or (total_size and written != total_size):
                                raise httpx.ReadError('Incomplete media response')
                            partial.replace(out)
                            return True
                    raise RuntimeError('Too many media redirects')
            except TransferLimit:
                raise
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {408, 429, 500, 502, 503, 504}:
                    return False
                retry = exc.response.headers.get('retry-after', '')
                # Long server rate limits are reported, not shortened.
                if retry.isdigit() and int(retry) > 30:
                    raise RuntimeError('Source rate limit; retry later') from exc
                delay = max(2 ** attempt, int(retry) if retry.isdigit() else 0)
            except httpx.TransportError:
                delay = min(2 ** attempt, 8)
            except RuntimeError:
                raise
            if attempt + 1 < attempts:
                sleep(delay)
        return False
    finally:
        partial.unlink(missing_ok=True)
