import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import bot
from youtube_public import player_manifest, request_manifest, watch_visitor

ID = 'u443WxCOAlY'


def document():
    return {'playabilityStatus': {'status': 'OK'}, 'videoDetails': {
        'videoId': ID, 'title': 'Example', 'lengthSeconds': '4'}, 'streamingData': {
        'adaptiveFormats': [
            {'itag': 137, 'url': 'https://rr1.googlevideo.com/video', 'mimeType': 'video/mp4; codecs="avc1.640028"',
             'width': 1920, 'height': 1080, 'fps': 60, 'contentLength': '100000', 'bitrate': 900000},
            {'itag': 140, 'url': 'https://rr1.googlevideo.com/audio', 'mimeType': 'audio/mp4; codecs="mp4a.40.2"',
             'bitrate': 128000, 'audioTrack': {'id': 'uz.0', 'audioIsDefault': True}}]}}


class YoutubePublicTests(unittest.TestCase):
    def test_blocked_fallback_cools_down_across_calls_and_then_recovers(self):
        from cobalt_provider import ProviderError
        error = httpx.HTTPStatusError('blocked', request=httpx.Request('POST', 'https://www.youtube.com/'),
                                      response=httpx.Response(403))
        with tempfile.TemporaryDirectory() as folder, patch('bot.DATA_DIR', Path(folder)), \
             patch('youtube_public.request_manifest', side_effect=error) as request, \
             patch('bot.download_ytdlp', return_value=[]) as download:
            with self.assertRaises(ProviderError):
                bot.download_youtube_public('https://youtu.be/' + ID, 'auto', folder)
            self.assertEqual(bot.download_youtube_public('https://youtu.be/' + ID, 'auto', folder), [])
            self.assertEqual(request.call_count, 1)
            download.assert_not_called()
            (Path(folder) / 'youtube_public_cooldown').write_text('0')
            request.side_effect = None
            request.return_value = {'id': ID}
            bot.download_youtube_public('https://youtu.be/' + ID, 'auto', folder)
            self.assertEqual(request.call_count, 2)
            download.assert_called_once()

    def test_real_ytdlp_selects_both_best_video_and_original_audio(self):
        from yt_dlp import YoutubeDL
        manifest = player_manifest(document(), ID, safe_url=lambda _: True)
        with YoutubeDL({'quiet': True, 'skip_download': True, 'format': 'bv*+ba/b/ba',
                        'cachedir': False, 'check_formats': False}) as downloader:
            selected = downloader.process_ie_result(manifest, download=False)
        self.assertEqual([f['format_id'] for f in selected['requested_formats']], ['public-137', 'public-140'])

    def test_formats_keep_quality_separate_audio_and_default_language(self):
        video, audio = player_manifest(document(), ID, safe_url=lambda _: True)['formats']
        self.assertEqual((video['width'], video['height'], video['fps'], video['filesize']), (1920, 1080, 60, 100000))
        self.assertEqual(video['acodec'], 'none')
        self.assertEqual(audio['vcodec'], 'none')
        self.assertEqual((audio['language'], audio['language_preference']), ('uz', 10))

    def test_identity_live_and_age_restrictions_are_checked(self):
        for mutation in [lambda d: d['videoDetails'].update(videoId='kOMHkvxfmd4'),
                         lambda d: d['videoDetails'].update(isLive=True),
                         lambda d: d.update(microformat={'playerMicroformatRenderer': {'isFamilySafe': False}}),
                         lambda d: d.update(playabilityStatus={'status': 'LOGIN_REQUIRED', 'reason': 'Sign in to confirm your age'})]:
            data = document()
            mutation(data)
            with self.assertRaises(RuntimeError):
                player_manifest(data, ID, safe_url=lambda _: True)

    def test_unsafe_drm_ciphered_or_incomplete_streams_are_excluded(self):
        for change in [{'url': 'https://googlevideo.com.evil.test/a'}, {'url': 'http://rr1.googlevideo.com/a'},
                       {'url': 'https://user:pass@rr1.googlevideo.com/a'}, {'drmFamilies': ['WIDEVINE']},
                       {'url': None, 'signatureCipher': 'not-decoded'}, {'type': 'FORMAT_STREAM_TYPE_OTF'}]:
            data = document()
            data['streamingData']['adaptiveFormats'] = [{**data['streamingData']['adaptiveFormats'][0], **change}]
            with self.assertRaises(RuntimeError):
                player_manifest(data, ID, safe_url=lambda _: True)
        with self.assertRaises(RuntimeError):
            player_manifest(document(), ID, safe_url=lambda _: False)

    def test_handshake_reuses_guest_context_without_credentials(self):
        requests = []
        def handle(req):
            requests.append(req)
            return httpx.Response(200, json={'responseContext': {'visitorData': 'guest-only'}} if len(requests) == 1 else document())
        result = request_manifest(ID, safe_url=lambda _: True,
            client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), **kw))
        self.assertEqual(result['id'], ID)
        self.assertEqual(len(requests), 2)
        bodies = [json.loads(req.content) for req in requests]
        self.assertEqual(bodies[1]['context']['client']['visitorData'], 'guest-only')
        self.assertEqual(requests[1].headers['X-Goog-Visitor-Id'], 'guest-only')
        self.assertEqual(requests[1].headers['X-Youtube-Client-Version'], '1.02')
        self.assertTrue(all(body['videoId'] == ID for body in bodies))
        self.assertTrue(all('contentCheckOk' not in body and 'racyCheckOk' not in body for body in bodies))
        self.assertTrue(all('authorization' not in req.headers and 'cookie' not in req.headers for req in requests))

    def test_redirects_and_oversized_metadata_are_rejected(self):
        for response in [httpx.Response(307, headers={'location': 'https://elsewhere.test/'}),
                         httpx.Response(200, content=b'x' * (4 * 1024 * 1024 + 1))]:
            with self.assertRaises((RuntimeError, httpx.HTTPError)):
                request_manifest(ID, safe_url=lambda _: True,
                    client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(lambda _: response), **kw))

    def test_blocked_web_front_door_uses_fixed_api_origin_once(self):
        requests = []
        def handle(req):
            requests.append(req)
            if req.url.host == 'www.youtube.com':
                return httpx.Response(403)
            body = json.loads(req.content)
            return httpx.Response(200, json=document() if body['context']['client']['clientName'] == 'VISIONOS'
                                  else {'responseContext': {'visitorData': 'guest'}})
        data = request_manifest(ID, safe_url=lambda _: True,
            client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), **kw))
        self.assertEqual(data['id'], ID)
        self.assertEqual([(r.method, r.url.host) for r in requests], [
            ('POST', 'www.youtube.com'), ('GET', 'www.youtube.com'),
            ('POST', 'youtubei.googleapis.com'), ('POST', 'youtubei.googleapis.com')])

    def test_watch_context_recovers_blocked_bootstrap_and_survives_origin_switch(self):
        requests = []
        def handle(req):
            requests.append(req)
            if req.method == 'GET':
                return httpx.Response(200, headers={'content-type': 'text/html'}, text=
                    '<script>ytcfg.set("ignored", false);ytcfg.set({"VISITOR_DATA":"watch-guest"});</script>')
            if req.url.host == 'www.youtube.com':
                return httpx.Response(403)
            return httpx.Response(200, json=document())
        result = request_manifest(ID, safe_url=lambda _: True,
            client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), **kw))
        self.assertEqual(result['id'], ID)
        self.assertEqual(len(requests), 4)
        for req in requests[2:]:
            context = json.loads(req.content)['context']['client']
            self.assertEqual((context['clientName'], context['visitorData']), ('VISIONOS', 'watch-guest'))
        self.assertEqual(str(requests[1].url), 'https://www.youtube.com/watch?v=' + ID)

    def test_missing_seed_visitor_uses_watch_context(self):
        requests = []
        def handle(req):
            requests.append(req)
            if req.method == 'GET':
                return httpx.Response(200, headers={'content-type': 'text/html'}, text=
                    'ytcfg.set({"INNERTUBE_CONTEXT":{"client":{"visitorData":"nested-guest"}}});')
            return httpx.Response(200, json={} if len(requests) == 1 else document())
        request_manifest(ID, safe_url=lambda _: True,
            client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), **kw))
        self.assertEqual(json.loads(requests[-1].content)['context']['client']['visitorData'], 'nested-guest')

    def test_watch_page_remains_bounded_and_does_not_follow_redirects(self):
        for response in [httpx.Response(307, headers={'location': 'https://elsewhere.test/'}),
                         httpx.Response(200, headers={'content-type': 'text/html'}, content=b'x' * (4 * 1024 * 1024 + 1)),
                         httpx.Response(200, headers={'content-type': 'application/json'}, json={'VISITOR_DATA': 'guest'})]:
            calls = []
            def handle(req):
                calls.append(req)
                return response
            with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
                with self.assertRaises((RuntimeError, httpx.HTTPError)):
                    watch_visitor(client, ID)
            self.assertEqual(len(calls), 1)

    def test_watch_bootstrap_cannot_override_video_access_restrictions(self):
        def handle(req):
            if req.method == 'GET':
                return httpx.Response(200, headers={'content-type': 'text/html'}, text='ytcfg.set({"VISITOR_DATA":"guest"});')
            if json.loads(req.content)['context']['client']['clientName'] == 'WEB':
                return httpx.Response(403)
            return httpx.Response(200, json={'playabilityStatus': {'status': 'LOGIN_REQUIRED', 'reason': 'Sign in to confirm your age'}})
        with self.assertRaisesRegex(RuntimeError, 'Sign in to confirm your age'):
            request_manifest(ID, safe_url=lambda _: True,
                client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), **kw))

    def test_watch_rate_limit_stops_without_another_origin(self):
        requests = []
        def handle(req):
            requests.append(req)
            return httpx.Response(429 if req.method == 'GET' else 403)
        with self.assertRaises(httpx.HTTPStatusError):
            request_manifest(ID, safe_url=lambda _: True,
                client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), **kw))
        self.assertEqual(len(requests), 2)

    def test_rate_limit_is_not_replayed_on_another_origin(self):
        requests = []
        def handle(req):
            requests.append(req)
            return httpx.Response(429)
        with self.assertRaises(httpx.HTTPStatusError):
            request_manifest(ID, safe_url=lambda _: True,
                client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), **kw))
        self.assertEqual(len(requests), 1)

    def test_short_link_passes_manifest_to_downloader_and_playlists_are_skipped(self):
        manifest = {'id': ID}
        with tempfile.TemporaryDirectory() as folder, patch('youtube_public.request_manifest', return_value=manifest) as request, \
             patch('bot.download_ytdlp', return_value=[]) as download:
            bot.download_youtube_public(f'https://youtu.be/{ID}?si=tracking', 'mp3_320', folder)
            self.assertEqual(request.call_args.args[0], ID)
            self.assertEqual(download.call_args.kwargs['manifest'], manifest)
            self.assertEqual(download.call_args.args[1], 'mp3_320')
            request.reset_mock()
            self.assertEqual(bot.download_youtube_public('https://youtube.com/playlist?list=PLtest', 'auto', folder), [])
            request.assert_not_called()
