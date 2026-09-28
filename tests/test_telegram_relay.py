import asyncio
import io
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import httpx

import bot
from telegram_relay import relay_handler
from telegram_network import health, snapshot


TOKEN = '123456:relay-test-token'


class RelayServerTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        def respond(req):
            self.received.append((req.method, str(req.url), req.headers, req.read()))
            data = (b'\x00video\xff' if '/file/bot' in req.url.path else
                    b'{"ok":true,"result":{"id":123456}}')
            return httpx.Response(200, headers={'Content-Length': str(len(data))}, stream=httpx.ByteStream(data))
        handler = relay_handler(TOKEN, upstream='https://api.telegram.org',
                                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_auth_url_has_no_token_and_forwards_multipart_intact(self):
        with httpx.Client(trust_env=False) as client:
            result = client.post(self.base + '/relay/api/sendMediaGroup',
                                 headers={'X-Veltrix-Token': TOKEN},
                                 files={'file': ('video.mp4', b'video-body', 'video/mp4')})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.received), 1)
        method, url, headers, content = self.received[0]
        self.assertEqual(method, 'POST')
        self.assertEqual(url, f'https://api.telegram.org/bot{TOKEN}/sendMediaGroup')
        self.assertIn(b'video-body', content)
        self.assertIn('multipart/form-data', headers['content-type'])
        self.assertNotIn('x-veltrix-token', headers)

    def test_chunked_upload_and_file_download_keep_bytes(self):
        with httpx.Client(trust_env=False) as client:
            headers = {'X-Veltrix-Token': TOKEN, 'Content-Type': 'application/octet-stream'}
            uploaded = client.post(self.base + '/relay/api/sendVideo', headers=headers,
                                   content=(block for block in (b'first-', b'second')))
            downloaded = client.get(self.base + '/relay/file/videos/file_0.mp4',
                                    headers={'X-Veltrix-Token': TOKEN})
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, b'\x00video\xff')
        self.assertEqual(self.received[0][3], b'first-second')
        self.assertEqual(self.received[1][1], f'https://api.telegram.org/file/bot{TOKEN}/videos/file_0.mp4')

    def test_rejects_bad_token_and_untrusted_paths(self):
        with httpx.Client(trust_env=False) as client:
            self.assertEqual(client.post(self.base + '/relay/api/getMe').status_code, 403)
            headers = {'X-Veltrix-Token': TOKEN}
            self.assertEqual(client.get(self.base + '/relay/api/deleteWebhook', headers=headers).status_code, 404)
            self.assertEqual(client.post(self.base + '/relay/api/getMe/evil', headers=headers).status_code, 404)
            self.assertEqual(client.get(self.base + '/relay/file/..%2Fsecret', headers=headers).status_code, 404)
        self.assertEqual(self.received, [])

    def test_health_is_public_and_contains_no_token(self):
        with httpx.Client(trust_env=False) as client:
            response = client.get(self.base + '/healthz')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(TOKEN, response.text)


class RelayClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_updates_marks_polling_ready_only_after_success(self):
        def respond(req):
            if req.url.path.endswith('/getUpdates'):
                return httpx.Response(200, json={'ok': True, 'result': []})
            return httpx.Response(200, json={'ok': True, 'result': {
                'id': 123456, 'is_bot': True, 'first_name': 'Veltrix'}})
        with patch('bot.TELEGRAM_RELAY_BASE', 'https://veltrix-downloader.onrender.com'), \
                patch('httpx.AsyncHTTPTransport', return_value=httpx.MockTransport(respond)):
            client = bot.application_builder(TOKEN).build().bot
            with patch.dict(health, {'last_api_ok': 0.0, 'last_poll_ok': 0.0, 'last_error': None}):
                async with client:
                    self.assertTrue(snapshot()['connected_recently'])
                    self.assertFalse(snapshot()['polling_recently'])
                    self.assertEqual(await client.get_updates(timeout=1), ())
                    self.assertTrue(snapshot()['polling_recently'])
                    health['last_poll_ok'] = time.time() - 181
                    self.assertFalse(snapshot()['polling_recently'])

    async def test_ptb_api_url_rewritten_and_token_sent_only_as_header(self):
        calls = []
        def respond(req):
            calls.append(req)
            return httpx.Response(200, json={'ok': True, 'result': {'id': 123456, 'is_bot': True, 'first_name': 'Veltrix'}})
        with patch('bot.TELEGRAM_RELAY_BASE', 'https://veltrix-downloader.onrender.com'), \
                patch('httpx.AsyncHTTPTransport', return_value=httpx.MockTransport(respond)):
            client = bot.application_builder(TOKEN).build().bot
            async with client:
                result = await client.get_me()
        self.assertEqual(result.id, 123456)
        self.assertTrue(calls)
        for req in calls:
            self.assertEqual(req.url.path, '/relay/api/getMe')
            self.assertNotIn(TOKEN, str(req.url))
            self.assertEqual(req.headers['x-veltrix-token'], TOKEN)

    async def test_ptb_file_download_uses_tokenless_file_path(self):
        calls = []
        def respond(req):
            calls.append(req)
            if req.url.path.endswith('/getMe'):
                return httpx.Response(200, json={'ok': True, 'result': {
                    'id': 123456, 'is_bot': True, 'first_name': 'Veltrix'}})
            if req.url.path.endswith('/getFile'):
                return httpx.Response(200, json={'ok': True, 'result': {
                    'file_id': 'file-id', 'file_unique_id': 'unique', 'file_path': 'videos/file_0.mp4'}})
            return httpx.Response(200, content=b'\x00clip\xff')
        with patch('bot.TELEGRAM_RELAY_BASE', 'https://veltrix-downloader.onrender.com'), \
                patch('httpx.AsyncHTTPTransport', return_value=httpx.MockTransport(respond)):
            client = bot.application_builder(TOKEN).build().bot
            async with client:
                file = await client.get_file('file-id')
                output = io.BytesIO()
                await file.download_to_memory(out=output)
        self.assertEqual(output.getvalue(), b'\x00clip\xff')
        self.assertIn('/relay/file/videos/file_0.mp4', [str(r.url.path) for r in calls])
        self.assertTrue(all(TOKEN not in str(req.url) and req.headers['x-veltrix-token'] == TOKEN
                            for req in calls))
