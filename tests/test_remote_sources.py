import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import bot
import remote_sources as remote


class RemoteTests(unittest.TestCase):
    def client(self, handle):
        return httpx.Client(transport=httpx.MockTransport(handle))

    def test_disabled_without_credentials(self):
        with patch.dict(os.environ, {}, clear=True), patch('remote_sources.httpx.Client') as client:
            self.assertEqual(remote.resolve('youtube', 'https://youtu.be/abcdefghijk', 'auto'), [])
            client.assert_not_called()

    def test_tunelio_respects_quality_and_uses_header_auth(self):
        requests = []
        def handle(request):
            requests.append(request)
            data = {'formats': [{'quality': '2160p'}, {'quality': '1080p'}, {'quality': '720p'}]}
            if request.url.path == '/create':
                self.assertEqual(request.url.params['quality'], '720p')
                data = {'status': 'ok', 'url': 'https://media.test/signed'}
            self.assertEqual(request.headers['Authorization'], 'Bearer secret')
            self.assertNotIn('secret', str(request.url))
            return httpx.Response(200, json=data)
        with patch.dict(os.environ, TUNELIO_API_KEY='secret'), self.client(handle) as client:
            result = remote._tunelio(client, 'https://youtu.be/abcdefghijk', '720')
        self.assertTrue(result[0]['requires_audio'])
        self.assertEqual(len(requests), 2)

    def test_mp3_request_uses_audio_only_without_info_call(self):
        def handle(request):
            self.assertEqual(request.url.path, '/create')
            self.assertEqual(request.url.params['quality'], 'm4a')
            return httpx.Response(200, json={'status': 'ok', 'url': 'https://media.test/audio'})
        with patch.dict(os.environ, TUNELIO_API_KEY='secret'), self.client(handle) as client:
            self.assertEqual(remote._tunelio(client, 'source', 'mp3_320')[0]['kind'], 'audio')

    def test_api_redirect_never_forwards_credentials(self):
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(302, headers={'location': 'https://evil.test/'})
        with self.client(handle) as client, self.assertRaisesRegex(remote.RemoteSourceError, 'HTTP 302'):
            remote._json(client, 'GET', 'https://provider.test/', headers={'Authorization': 'secret'})
        self.assertEqual(len(requests), 1)

    def test_api_failure_does_not_leak_payload(self):
        with self.client(lambda r: httpx.Response(401, text='private-key-here')) as client:
            with self.assertRaises(remote.RemoteSourceError) as ctx:
                remote._json(client, 'GET', 'https://provider.test/')
        self.assertNotIn('private-key-here', str(ctx.exception))

    def test_apify_starts_once_and_polls_same_run(self):
        methods = []
        def handle(request):
            methods.append(request.method)
            if request.method == 'POST':
                self.assertEqual(json.loads(request.content), {'videoUrls': ['source']})
                self.assertEqual(request.url.params['maxTotalChargeUsd'], '0.02')
                return httpx.Response(200, json={'data': {'id': 'run1', 'status': 'RUNNING'}})
            if '/actor-runs/' in request.url.path:
                return httpx.Response(200, json={'data': {'id': 'run1', 'status': 'SUCCEEDED', 'defaultDatasetId': 'set1'}})
            return httpx.Response(200, json=[{'formats': [
                {'type': 'video', 'ext': 'mp4', 'height': 1080, 'has_audio': False, 'url': 'silent'},
                {'type': 'video', 'ext': 'mp4', 'height': 720, 'has_audio': True, 'url': 'combined'}]}])
        with patch.dict(os.environ, APIFY_TOKEN='secret'), self.client(handle) as client:
            self.assertEqual(remote._apify(client, 'source', 'auto')[0]['url'], 'combined')
        self.assertEqual(methods, ['POST', 'GET', 'GET'])

    def test_apify_rejects_partial_collection(self):
        with patch.dict(os.environ, APIFY_TOKEN='secret'), patch('remote_sources._json', side_effect=[
            {'data': {'id': 'run1', 'status': 'SUCCEEDED', 'defaultDatasetId': 'set1'}},
            [{'error': 'unavailable'}],
        ]), self.assertRaisesRegex(remote.RemoteSourceError, 'Incomplete'):
            remote._apify(None, 'source', 'auto')

    def test_playlist_is_not_replaced_by_single_remote_video(self):
        with patch('remote_sources.resolve') as resolve:
            self.assertEqual(bot.download_remote_source('https://youtube.com/playlist?list=PL123', 'auto', '/tmp'), [])
            resolve.assert_not_called()

    def test_real_media_fallback_after_bot_check(self):
        # Exercise grab -> remote download -> actual ffprobe/audio inspection.
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'fixture.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=64x64:d=0.3',
                            '-f', 'lavfi', '-i', 'sine=duration=0.3', '-c:v', 'libx264',
                            '-c:a', 'aac', '-shortest', str(fixture)], check=True)
            calls = []
            def download(url, path, **kwargs):
                calls.append(kwargs)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(fixture.read_bytes())
                return True
            with patch.dict(os.environ, TUNELIO_API_KEY='secret'), \
                 patch('bot.safe_remote_url', return_value=True), \
                 patch('bot.download_ytdlp', side_effect=RuntimeError("Sign in to confirm you're not a bot")), \
                 patch('remote_sources.resolve', return_value=[{'url': 'https://cdn.test/video', 'kind': 'video', 'requires_audio': True}]), \
                 patch('bot._download_direct_file', side_effect=download):
                files = bot.grab('https://youtube.com/watch?v=abcdefghijk', 'auto', directory)
            self.assertEqual(len(files), 1)
            self.assertTrue(bot.has_audio(files[0]))
            self.assertEqual(calls, [{'referer': ''}])

    def test_silent_provider_video_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('remote_sources.resolve', return_value=[{'url': 'https://cdn.test/video', 'kind': 'video', 'requires_audio': True}]), \
             patch('bot.safe_remote_url', return_value=True), \
             patch('bot._download_direct_file', return_value=True), \
             patch('bot.validate_media_files'), patch('bot.classify', return_value='video'), \
             patch('bot.has_audio', return_value=False), self.assertRaisesRegex(remote.RemoteSourceError, 'no audio'):
            bot.download_remote_source('https://youtube.com/watch?v=abcdefghijk', 'auto', directory)


if __name__ == '__main__':
    unittest.main()
