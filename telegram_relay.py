"""Authenticated, streaming Bot API bridge for a phone without Telegram egress.

Only the owner's bot token in a request header authorizes a call. Public URLs
never contain the credential; the upstream host is fixed and TLS is verified.
"""
from __future__ import annotations

import hmac
import logging
import os
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore, Lock
from time import monotonic
from urllib.parse import unquote, urlsplit

import httpx


log = logging.getLogger('veltrix.relay')
MAX_BODY = 550_000_000  # Up to ten cloud-Bot-API-sized items in one album.
API_METHOD = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}\Z')


def relay_handler(token: str, upstream: str = 'https://api.telegram.org', client_factory=None):
    """Build an isolated handler; upstream override is solely for local tests."""
    slots = BoundedSemaphore(16)
    poll_lock = Lock()
    poll_log = {'seen': False, 'last': 0.0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            # Request headers and URLs are never copied to application logs.
            return

        def _answer(self, code: int, data: bytes, mime: str = 'application/json'):
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            if code >= 400:
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.end_headers()
            self.wfile.write(data)

        def _destination(self):
            parsed = urlsplit(self.path)
            if parsed.path.startswith('/relay/api/'):
                method = parsed.path[len('/relay/api/'):]
                if not API_METHOD.fullmatch(method):
                    return None
                return f'{upstream}/bot{token}/{method}' + (f'?{parsed.query}' if parsed.query else '')
            if parsed.path.startswith('/relay/file/'):
                path = parsed.path[len('/relay/file/'):]
                components = unquote(path).split('/')
                if (not path or any(part in {'', '.', '..'} or '\\' in part for part in components)
                        or '?' in path or '#' in path):
                    return None
                return f'{upstream}/file/bot{token}/{path}'
            return None

        def _body(self):
            size = self.headers.get('Content-Length')
            chunked = self.headers.get('Transfer-Encoding', '').lower() == 'chunked'
            if size is not None:
                remaining = int(size)
                if remaining < 0 or remaining > MAX_BODY:
                    raise ValueError('request too large')
                while remaining:
                    data = self.rfile.read(min(65536, remaining))
                    if not data:
                        raise OSError('incomplete upload')
                    remaining -= len(data)
                    yield data
            elif chunked:
                received = 0
                while True:
                    line = self.rfile.readline(128)
                    count = int(line.split(b';', 1)[0].strip(), 16)
                    received += count
                    if received > MAX_BODY:
                        raise ValueError('request too large')
                    if not count:
                        for _ in range(20):
                            if self.rfile.readline(8192) in (b'\r\n', b'\n', b''):
                                return
                        raise ValueError('too many upload trailers')
                    remaining = count
                    while remaining:
                        data = self.rfile.read(min(65536, remaining))
                        if not data:
                            raise OSError('incomplete upload')
                        remaining -= len(data)
                        yield data
                    if self.rfile.read(2) != b'\r\n':
                        raise ValueError('invalid chunk framing')

        def _request(self):
            if self.path in {'/', '/health', '/healthz', '/readyz'} and self.command == 'GET':
                return self._answer(200, b'{"ok":true,"service":"veltrix-relay"}')
            if not hmac.compare_digest(self.headers.get('X-Veltrix-Token', ''), token):
                return self._answer(403, b'{"ok":false}')
            target = self._destination()
            if not target or (self.command == 'GET' and '/relay/file/' not in self.path):
                return self._answer(404, b'{"ok":false}')
            polling = self.command == 'POST' and urlsplit(self.path).path == '/relay/api/getUpdates'
            if not slots.acquire(blocking=False):
                return self._answer(503, b'{"ok":false,"description":"Relay busy"}')
            response_started = False
            started = monotonic()
            try:
                if polling:
                    with poll_lock:
                        if not poll_log['seen']:
                            log.info('Relay received first getUpdates request')
                            poll_log['seen'] = True
                self.connection.settimeout(120)
                length = self.headers.get('Content-Length')
                transfer_encoding = self.headers.get('Transfer-Encoding', '').lower()
                if transfer_encoding not in {'', 'chunked'} or (length is not None and transfer_encoding):
                    return self._answer(400, b'{"ok":false,"description":"Invalid upload framing"}')
                if length is not None and (not length.isdecimal() or int(length) > MAX_BODY):
                    return self._answer(413, b'{"ok":false,"description":"Relay size limit"}')
                headers = {}
                if self.headers.get('Content-Type'):
                    headers['Content-Type'] = self.headers['Content-Type']
                if length is not None:
                    headers['Content-Length'] = length
                factory = client_factory or (lambda: httpx.Client(trust_env=False, timeout=httpx.Timeout(
                    connect=12, read=150, write=1800, pool=12), follow_redirects=False))
                with factory() as client:
                    body = (self._body() if length is not None or self.headers.get('Transfer-Encoding')
                            else b'') if self.command == 'POST' else None
                    with client.stream(self.command, target, headers=headers, content=body) as response:
                        if polling:
                            now = monotonic()
                            with poll_lock:
                                if response.status_code != 200 or now - poll_log['last'] >= 300:
                                    log.info('Relay getUpdates: HTTP %s after %.1fs', response.status_code, now - started)
                                    poll_log['last'] = now
                        self.send_response(response.status_code)
                        for name in ('Content-Type', 'Content-Length', 'Content-Encoding',
                                     'Content-Disposition', 'Retry-After'):
                            if name in response.headers:
                                self.send_header(name, response.headers[name])
                        if 'Content-Length' not in response.headers:
                            self.send_header('Connection', 'close')
                            self.close_connection = True
                        self.end_headers()
                        response_started = True
                        for chunk in response.iter_raw(chunk_size=65536):
                            self.wfile.write(chunk)
            except (httpx.HTTPError, OSError, ValueError, socket.timeout) as exc:
                log.warning('Telegram relay %s: %s', 'getUpdates' if polling else 'transfer', type(exc).__name__)
                if not response_started and not self.wfile.closed:
                    try:
                        self._answer(502, b'{"ok":false,"description":"Relay transfer failed"}')
                    except (BrokenPipeError, OSError):
                        pass
                self.close_connection = True
            finally:
                slots.release()

        def do_GET(self):
            self._request()

        def do_POST(self):
            self._request()

    return Handler


def serve_relay(token: str) -> None:
    port = int(os.getenv('PORT', '10000'))
    ThreadingHTTPServer(('0.0.0.0', port), relay_handler(token)).serve_forever()
