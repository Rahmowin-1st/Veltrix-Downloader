"""Observe connectivity and fall back only before any request bytes are sent."""
import time
from urllib.parse import quote
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

import httpx
from telegram.request import HTTPXRequest

health = {'last_api_ok': 0.0, 'last_poll_ok': 0.0, 'last_error': None,
          'last_poll_start': 0.0, 'last_poll_error': None}


def environment_proxy() -> str | None:
    """Use an operator-configured HTTP tunnel only if this host is not excluded."""
    if proxy_bypass('api.telegram.org'):
        return None
    configured = getproxies()
    proxy = configured.get('https') or configured.get('all')
    if not proxy:
        return None
    try:
        parsed = urlsplit(proxy)
        parsed.port  # Reject invalid/non-numeric ports before constructing a transport.
    except ValueError:
        return None
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return None
    return proxy


class ConnectFallback(httpx.AsyncBaseTransport):
    def __init__(self, primary, fallback):
        self.primary, self.fallback = primary, fallback

    async def handle_async_request(self, request):
        first, second = self.primary, self.fallback
        try:
            return await first.handle_async_request(request)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # Read/write timeouts are deliberately not replayed.
            response = await second.handle_async_request(request)
            self.primary, self.fallback = second, first
            return response

    async def aclose(self):
        await self.primary.aclose()
        await self.fallback.aclose()


class ObservedRequest(HTTPXRequest):
    def __init__(self, *, relay_base: str = '', relay_token: str = '', **kwargs):
        super().__init__(**kwargs)
        self.relay_base = relay_base
        self.relay_token = relay_token

    async def do_request(self, url, method, **kwargs):
        if self.relay_base:
            api = f'{self.relay_base}/bot{self.relay_token}/'
            file = f'{self.relay_base}/file/bot{self.relay_token}/'
            encoded_file = f'{self.relay_base}/file/bot{quote(self.relay_token, safe="")}/'
            if url.startswith(api):
                url = self.relay_base + '/relay/api/' + url[len(api):]
            elif url.startswith(file):
                url = self.relay_base + '/relay/file/' + url[len(file):]
            elif url.startswith(encoded_file):
                url = self.relay_base + '/relay/file/' + url[len(encoded_file):]
            else:
                raise ValueError('Unexpected Telegram relay target')
        polling = url.rsplit('/', 1)[-1].lower() == 'getupdates'
        if polling:
            health['last_poll_start'] = time.time()
        try:
            result = await super().do_request(url, method, **kwargs)
        except Exception as exc:
            health['last_error'] = type(exc).__name__
            if polling:
                health['last_poll_error'] = type(exc).__name__
            raise
        if result[0] == 200:
            health['last_api_ok'] = time.time()
            health['last_error'] = None
            if polling:
                health['last_poll_ok'] = time.time()
                health['last_poll_error'] = None
        elif result[0] == 409:
            health['last_error'] = 'PollingConflict'
        if polling and result[0] != 200:
            health['last_poll_error'] = 'PollingConflict' if result[0] == 409 else f'HTTP {result[0]}'
        return result


def snapshot():
    result = dict(health)
    result['connected_recently'] = bool(result['last_error'] is None and result['last_api_ok'] and time.time() - result['last_api_ok'] < 180)
    result['polling_recently'] = bool(result['last_poll_error'] is None and result['last_poll_ok'] and time.time() - result['last_poll_ok'] < 180)
    return result
