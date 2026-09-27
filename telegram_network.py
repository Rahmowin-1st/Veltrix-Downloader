"""Observe connectivity and fall back only before any request bytes are sent."""
import time

import httpx
from telegram.request import HTTPXRequest

health = {'last_api_ok': 0.0, 'last_poll_ok': 0.0, 'last_error': None}


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
    async def do_request(self, url, method, **kwargs):
        try:
            result = await super().do_request(url, method, **kwargs)
        except Exception as exc:
            health['last_error'] = type(exc).__name__
            raise
        if result[0] == 200:
            health['last_api_ok'] = time.time()
            health['last_error'] = None
            if url.rsplit('/', 1)[-1].lower() == 'getupdates':
                health['last_poll_ok'] = time.time()
        elif result[0] == 409:
            health['last_error'] = 'PollingConflict'
        return result


def snapshot():
    result = dict(health)
    result['connected_recently'] = bool(result['last_error'] is None and result['last_api_ok'] and time.time() - result['last_api_ok'] < 180)
    return result
