import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import bot
from vidfetch_provider import ProviderError, VidFetchQuotaExceeded, create_job, fetch


VIDEO_ID = 'aqz-KE-bpKQ'
URL = 'https://www.youtube.com/watch?v=' + VIDEO_ID
JOB = 'dl_testjob123'
FILE_URL = 'https://storage.vidfetch.dev/test-media.mp4'


class VidFetchTests(unittest.TestCase):
    def fetch(self, handler, folder, quality='2160p'):
        now = [0]

        def sleep(seconds):
            now[0] += seconds

        return fetch(VIDEO_ID, quality, Path(folder), api_key='test-key', safe_url=lambda _: True,
                     budget=lambda _: None, max_bytes=1024 * 1024,
                     client_factory=lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handler), **kwargs),
                     clock=lambda: now[0], sleep=sleep)

    def test_checks_and_transfers_actual_media_without_leaking_the_key(self):
        with tempfile.TemporaryDirectory() as folder:
            samples = {'2160p': Path(folder) / 'reference.mp4', 'mp3': Path(folder) / 'reference.mp3'}
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=32x32',
                            '-f', 'lavfi', '-i', 'sine=frequency=400', '-t', '2',
                            '-c:v', 'libx264', '-c:a', 'aac', str(samples['2160p'])], check=True, timeout=12)
            subprocess.run(['ffmpeg', '-v', 'error', '-i', str(samples['2160p']), '-vn', '-c:a', 'libmp3lame',
                            str(samples['mp3'])], check=True, timeout=12)
            for quality, sample in samples.items():
                with self.subTest(quality=quality):
                    calls = []

                    def handle(request):
                        calls.append((request.method, request.url.path))
                        if request.url.host == 'storage.vidfetch.dev':
                            self.assertNotIn('Authorization', request.headers)
                            self.assertNotIn('Cookie', request.headers)
                            return httpx.Response(200, content=sample.read_bytes(),
                                                  headers={'Content-Type': 'application/octet-stream'})
                        self.assertEqual(request.url.host, 'api.vidfetch.dev')
                        self.assertEqual(request.headers['Authorization'], 'Bearer test-key')
                        job = {'id': JOB, 'url': URL, 'video_id': VIDEO_ID}
                        if request.url.path == '/v1/info':
                            self.assertEqual(json.loads(request.content), {'url': URL})
                            return httpx.Response(200, json={'id': VIDEO_ID, 'duration': 2})
                        if request.method == 'POST':
                            self.assertEqual(json.loads(request.content), {'url': URL, 'format': quality, 'destination': 'url'})
                            return httpx.Response(202, json={**job, 'status': 'queued'})
                        if request.method == 'DELETE':
                            return httpx.Response(204)
                        return httpx.Response(200, json={**job, 'status': 'completed', 'download_url': FILE_URL,
                                                         'size_bytes': sample.stat().st_size})

                    output, = self.fetch(handle, folder, quality=quality)
                    self.assertEqual(output.read_bytes(), sample.read_bytes())
                    self.assertTrue(bot.has_audio(output))
                    job_path = '/v1/downloads/' + JOB
                    self.assertEqual(calls, [('POST', '/v1/info'), ('POST', '/v1/downloads'),
                                             ('GET', job_path), ('GET', '/test-media.mp4'), ('DELETE', job_path)])

    def test_wrong_identity_or_restriction_stops_before_creating_a_job(self):
        for info in ({'id': 'abcdefghijk', 'duration': 2},
                     {'id': VIDEO_ID, 'duration': 2, 'availability': 'private'},
                     {'id': VIDEO_ID, 'duration': 2, 'age_limit': 18}):
            with self.subTest(info=info), tempfile.TemporaryDirectory() as folder:
                def handle(request):
                    self.assertEqual(request.url.path, '/v1/info')
                    return httpx.Response(200, json=info)

                with self.assertRaisesRegex(ProviderError, 'verify this public video'):
                    self.fetch(handle, folder)

    def test_create_descends_only_for_rejected_quota_requests(self):
        for responses, expected_error in (([402, 402, 202], None), ([402, 429], ProviderError),
                                          ([402] * 8, VidFetchQuotaExceeded)):
            with self.subTest(responses=responses):
                formats = []

                def handle(request):
                    status = responses[len(formats)]
                    formats.append(json.loads(request.content)['format'])
                    self.assertEqual(request.url.path, '/v1/downloads')
                    return httpx.Response(status, json={'id': JOB})

                with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                    if expected_error:
                        with self.assertRaises(expected_error):
                            create_job(client, URL, '2160p')
                    else:
                        self.assertEqual(create_job(client, URL, '2160p'), ({'id': JOB}, '1080p'))
                self.assertEqual(formats, ['2160p', '1440p', '1080p', '720p', '480p', '360p', '240p', '144p'][:len(responses)])

    def test_rejects_untrusted_media_redirect(self):
        with tempfile.TemporaryDirectory() as folder:
            def handle(request):
                if request.url.path == '/v1/info':
                    return httpx.Response(200, json={'id': VIDEO_ID, 'duration': 2})
                if request.method == 'POST':
                    return httpx.Response(202, json={'id': JOB, 'url': URL, 'status': 'completed', 'download_url': FILE_URL})
                if request.method == 'DELETE':
                    return httpx.Response(204)
                self.assertEqual(request.url.host, 'storage.vidfetch.dev')
                return httpx.Response(302, headers={'Location': 'https://unrelated.example/file.mp4'})

            with self.assertRaisesRegex(ProviderError, 'could not deliver'):
                self.fetch(handle, folder)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_worker_falls_back_for_an_individual_video_after_bot_check(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {
                'VIDFETCH_API_KEY': 'test-key', 'NEWISTY_YOUTUBE': '0', 'COBALT_API_URL': '',
                'YOUTUBE_COOKIE_FILE': '', 'YOUTUBE_COOKIES_B64': ''}), \
                patch('bot.safe_remote_url', return_value=True), patch('bot.download_youtube_public', return_value=[]), \
                patch('vidfetch_provider.fetch', return_value=[]) as provider, \
                patch('bot.download_ytdlp', side_effect=RuntimeError("Sign in to confirm you're not a bot")) as primary:
            with self.assertRaisesRegex(RuntimeError, 'confirm you'):
                bot.grab('https://youtu.be/' + VIDEO_ID + '?si=tracking', 'auto', folder)
            self.assertEqual(provider.call_args.args[:2], (VIDEO_ID, '2160p'))
            provider.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'confirm you'):
                bot.grab('https://www.youtube.com/playlist?list=PLdemo', 'auto', folder)
            primary.side_effect = RuntimeError('Private video')
            with self.assertRaisesRegex(RuntimeError, 'Private video'):
                bot.grab(URL, 'auto', folder)
            provider.assert_not_called()
