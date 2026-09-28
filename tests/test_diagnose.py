import unittest
from unittest.mock import patch

import httpx

from diagnose import probe_telegram_route


class RouteDiagnosisTests(unittest.IsolatedAsyncioTestCase):
    async def test_getme_probe_reports_only_status_and_does_not_leak_token(self):
        token = '12345:private-test-secret'
        def respond(request):
            self.assertEqual(request.method, 'POST')
            self.assertTrue(str(request.url).endswith('/getMe'))
            return httpx.Response(401, json={'description': token})
        with patch('diagnose.httpx.AsyncHTTPTransport', return_value=httpx.MockTransport(respond)) as transport:
            result = await probe_telegram_route(token, 'Telegram IPv4 route', '0.0.0.0')
        self.assertEqual(transport.call_args.kwargs['local_address'], '0.0.0.0')
        self.assertEqual(result, 'Telegram IPv4 route: invalid bot token (HTTP 401)')
        self.assertNotIn(token, result)

    async def test_connect_timeout_does_not_print_request_url(self):
        token = '12345:private-test-secret'
        def fail(request):
            raise httpx.ConnectTimeout(str(request.url))
        with patch('diagnose.httpx.AsyncHTTPTransport', return_value=httpx.MockTransport(fail)):
            result = await probe_telegram_route(token, 'Telegram automatic route', None)
        self.assertEqual(result, 'Telegram automatic route: ConnectTimeout')
        self.assertNotIn(token, result)
