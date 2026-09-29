"""Short-lived, read-only source probes for investigating an actual Render failure.

Enabled by SOURCE_PROBE_UNTIL (UTC ISO date) and explicit source IDs in the
environment. Never writes URLs, cookies, page content or media to the log.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


log = logging.getLogger('veltrix.source_probe')


class QuietYDL:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def reason(exc: Exception) -> str:
    text = str(exc).lower()
    if 'confirm you’re not a bot' in text or "confirm you're not a bot" in text:
        return 'bot-check'
    if '403' in text or 'forbidden' in text:
        return 'HTTP403'
    if '404' in text or 'not found' in text:
        return 'HTTP404'
    if 'sign in' in text or 'login' in text:
        return 'login-required'
    if 'no formats' in text or 'not available' in text:
        return 'no-formats'
    return type(exc).__name__


def probe_snapchat(spotlight_id: str) -> None:
    import bot
    import httpx

    if not re.fullmatch(r'[A-Za-z0-9_-]{20,160}', spotlight_id):
        log.warning('Snapchat probe: invalid ID')
        return
    base = f'https://www.snapchat.com/spotlight/{spotlight_id}'
    for label, url in [('direct', base), ('locale', base + '?locale=en_US'),
                       ('embed', base + '/embed')]:
        try:
            page = bot.fetch_public_page(url)
            selected = bot._snap_info_from_exact_page(page.text, str(page.url))
            if not selected and bot._snap_page_owns_spotlight(page.text, str(page.url)):
                selected = bot.snapchat_preload(page.text)
            log.info('Snapchat probe %s: HTTP%s exact_video=%s bytes=%s', label,
                     page.status_code, bool(selected), len(page.content))
        except Exception as exc:
            log.info('Snapchat probe %s: %s', label, reason(exc))
            if isinstance(exc, httpx.DecodingError):
                # The exact embed route sometimes advertises compression that
                # httpx cannot decode. Inspect a bounded raw response without
                # writing the page, cookies, media URL or HTML to the log.
                try:
                    headers = dict(bot.request_headers(url))
                    headers['Accept-Encoding'] = 'identity'
                    with httpx.Client(headers=headers, follow_redirects=False, timeout=20,
                                      proxy=bot.PROXY or None, trust_env=False) as client:
                        with client.stream('GET', url) as raw:
                            data = b''
                            for chunk in raw.iter_raw():
                                data += chunk
                                if len(data) > 12 * 1024 * 1024:
                                    raise RuntimeError('Probe page size exceeded')
                            page = data.decode('utf-8', errors='replace')
                            exact = bool(bot._snap_info_from_exact_page(page, url))
                            log.info('Snapchat probe %s raw: HTTP%s content_type=%s encoding=%s bytes=%s exact_video=%s',
                                     label, raw.status_code,
                                     (raw.headers.get('content-type') or '').split(';')[0],
                                     raw.headers.get('content-encoding') or 'none', len(data), exact)
                            log.info('Snapchat probe %s markers: exact_id=%s next_data=%s content_url=%s og_video=%s preload_video=%s',
                                     label, spotlight_id in page, '__NEXT_DATA__' in page,
                                     'contentUrl' in page, 'og:video' in page,
                                     bool(re.search(r'<link[^>]+as=["\']video["\']', page, re.I)))
                except Exception as retry_exc:
                    log.info('Snapchat probe %s raw: %s', label, reason(retry_exc))


def probe_youtube(ids: str) -> None:
    import bot
    from yt_dlp import YoutubeDL

    for index, video_id in enumerate(ids.split(',')[:3], 1):
        video_id = video_id.strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            continue
        url = f'https://www.youtube.com/watch?v={video_id}'
        for client in ('default', 'web_safari', 'android_sdkless'):
            opts = {
                'quiet': True, 'no_warnings': True, 'skip_download': True,
                'noplaylist': True, 'socket_timeout': 10,
                'retries': 0, 'extractor_retries': 0,
                'http_headers': bot.request_headers(url),
                'js_runtimes': {'deno': {'path': bot.DENO_BIN}} if bot.DENO_BIN else {},
                'logger': QuietYDL(),
            }
            if client != 'default':
                opts['extractor_args'] = {'youtube': {'player_client': [client]}}
            cookie_file = os.getenv('YOUTUBE_COOKIE_FILE', '')
            if cookie_file and os.path.isfile(cookie_file):
                opts['cookiefile'] = cookie_file
            try:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                log.info('YouTube probe #%s %s: formats=%s', index,
                         client, len(info.get('formats') or []) if info else 0)
            except Exception as exc:
                log.info('YouTube probe #%s %s: %s', index, client, reason(exc))


def probe_pinterest(short_code: str) -> None:
    if not re.fullmatch(r'[A-Za-z0-9_-]{5,40}', short_code):
        log.warning('Pinterest probe: invalid short code')
        return
    try:
        with tempfile.TemporaryDirectory(prefix='vx_pin_probe_'):
            cmd = [sys.executable, '-m', 'gallery_dl', '--config-ignore', '--resolve-json',
                   f'https://pin.it/{short_code}']
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode:
            log.info('Pinterest probe: metadata unavailable (exit=%s)', proc.returncode)
            return
        rows = json.loads(proc.stdout)
        urls = [row for row in rows if isinstance(row, list) and row and row[0] == 3]
        kinds = {}
        for row in urls:
            ext = str(row[-1].get('extension', '')).lower() if isinstance(row[-1], dict) else ''
            kind = ('audio' if ext in {'m4a', 'mp3', 'aac', 'opus'} else
                    'video' if ext in {'mp4', 'm3u8', 'webm'} else 'image-or-other')
            kinds[kind] = kinds.get(kind, 0) + 1
        log.info('Pinterest probe: entries=%s kinds=%s', len(urls), kinds)
    except Exception as exc:
        log.info('Pinterest probe: %s', reason(exc))


def run() -> None:
    deadline = os.getenv('SOURCE_PROBE_UNTIL', '').strip()
    if not deadline:
        return
    try:
        end = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
        if end.tzinfo is None or not datetime.now(timezone.utc) < end.astimezone(timezone.utc):
            return
    except ValueError:
        return
    snap = os.getenv('SOURCE_PROBE_SPOTLIGHT_ID', '').strip()
    youtube = os.getenv('SOURCE_PROBE_YOUTUBE_IDS', '').strip()
    pinterest = os.getenv('SOURCE_PROBE_PIN_CODE', '').strip()
    if snap:
        probe_snapchat(snap)
    if youtube:
        probe_youtube(youtube)
    if pinterest:
        probe_pinterest(pinterest)
