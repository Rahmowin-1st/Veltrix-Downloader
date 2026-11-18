import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import bot
from newisty_provider import ProviderError, ProviderRateLimit, fetch


VIDEO_ID = 'kOMHkvxfmd4'
URL = 'https://www.youtube.com/watch?v=' + VIDEO_ID
JOB = '34326ec8-c670-4408-a191-37e343a3609d'
BOT_CHECK = RuntimeError("Sign in to confirm you’re not a bot")


class NewistyTests(unittest.TestCase):
    def fetch(self, handle, folder, mode='best-mp4'):
        now = [0]

        def advance(seconds):
            now[0] += seconds

        return fetch(VIDEO_ID, mode, Path(folder), safe_url=lambda _: True,
                     budget=lambda _: None, max_bytes=1024 * 1024,
                     client_factory=lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handle), **kwargs),
                     clock=lambda: now[0], sleep=advance)

    def test_downloads_verified_video_after_job_is_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'reference.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=32x32',
                            '-f', 'lavfi', '-i', 'sine=frequency=400', '-t', '2',
                            '-c:v', 'libx264', '-c:a', 'aac', str(source)], check=True, timeout=12)
            calls = []

            def handle(req):
                self.assertEqual(req.url.host, 'newisty.com')
                self.assertNotIn('authorization', req.headers)
                self.assertNotIn('cookie', req.headers)
                key = req.url.path.split('/')[3]
                calls.append(key)
                if key == 'info':
                    self.assertEqual(json.loads(req.content), {'url': URL})
                    data = {'id': VIDEO_ID, 'duration': 2}
                elif key == 'start':
                    self.assertEqual(json.loads(req.content), {'url': URL, 'format': 'best-mp4'})
                    data = {'mode': 'queued', 'job_id': JOB}
                elif key == 'progress' and calls.count('progress') == 1:
                    return httpx.Response(404)
                elif key == 'progress':
                    data = {'status': 'done', 'size': source.stat().st_size}
                else:
                    self.assertEqual(req.url.path, '/api/video-downloader/download/' + JOB)
                    return httpx.Response(200, content=source.read_bytes(), headers={'content-type': 'video/mp4'})
                return httpx.Response(200, json={'status': True, 'data': data})

            output, = self.fetch(handle, folder)
            self.assertEqual(calls, ['info', 'start', 'progress', 'progress', 'download'])
            self.assertEqual(output.read_bytes(), source.read_bytes())
            self.assertTrue(bot.has_audio(output))

    def test_wrong_identity_or_access_requirement_cannot_start_a_job(self):
        for data in ({'id': 'a' * 11, 'duration': 2},
                     {'id': VIDEO_ID, 'duration': 2, 'age_limit': 18},
                     {'id': VIDEO_ID, 'duration': 2, 'availability': 'private'}):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as folder:
                calls = []

                def handle(req):
                    calls.append(req.url.path)
                    return httpx.Response(200, json={'status': True, 'data': data})

                with self.assertRaises(ProviderError):
                    self.fetch(handle, folder)
                self.assertEqual(calls, ['/api/video-downloader/info'])

    def test_rate_limit_stops_without_requeue(self):
        paths = []

        def handle(req):
            paths.append(req.url.path)
            return httpx.Response(429, headers={'Retry-After': '60'})

        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ProviderRateLimit):
            self.fetch(handle, folder)
        self.assertEqual(paths, ['/api/video-downloader/info'])

    def test_grab_uses_provider_only_after_bot_check_and_canonicalizes_url(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'NEWISTY_YOUTUBE': '1', 'YOUTUBE_COOKIE_FILE': '', 'YOUTUBE_COOKIES_B64': '', 'COBALT_API_URL': ''}), \
                patch('bot.safe_remote_url', return_value=True), patch('bot.download_youtube_public', return_value=[]), \
                patch('newisty_provider.fetch', return_value=[]) as fetch_from_provider, \
                patch('bot.download_ytdlp', side_effect=BOT_CHECK) as primary:
            with self.assertRaisesRegex(RuntimeError, 'confirm you'):
                bot.grab('https://youtu.be/' + VIDEO_ID + '?si=tracking', 'auto', folder)
            self.assertEqual(fetch_from_provider.call_args.args[:2], (VIDEO_ID, 'best-mp4'))
            fetch_from_provider.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'confirm you'):
                bot.grab('https://www.youtube.com/playlist?list=PLdemo', 'auto', folder)
            primary.side_effect = RuntimeError('Private video')
            with self.assertRaisesRegex(RuntimeError, 'Private video'):
                bot.grab(URL, 'auto', folder)
            fetch_from_provider.assert_not_called()
