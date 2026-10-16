import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import bot
from cobalt_provider import ProviderError, parse_manifest, request_manifest
from source_metadata import instagram_dash_audio_urls, instagram_extractor


def dash(url, *, segmented=False):
    segment = '<SegmentTemplate media="piece-$Number$.m4a" />' if segmented else '<SegmentBase indexRange="0-100" />'
    return ('<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>'
            '<AdaptationSet mimeType="audio/mp4"><Representation bandwidth="128000">'
            f'<BaseURL>{url}</BaseURL>{segment}'
            '</Representation></AdaptationSet></Period></MPD>')


class ProviderTests(unittest.TestCase):
    def test_picker_preserves_visual_order_and_separate_post_soundtrack(self):
        entries = parse_manifest({'status': 'picker', 'picker': [
            {'type': 'photo', 'url': 'https://cdn.test/a.jpg'},
            {'type': 'video', 'url': 'https://cdn.test/b.mp4'}],
            'audio': 'https://cdn.test/music.m4a'}, safe_url=lambda _: True, max_items=10)
        self.assertEqual([e['kind'] for e in entries], ['image', 'video', 'audio'])
        self.assertEqual(entries[-1]['scope'], 'post')

    def test_incomplete_picker_is_rejected_as_a_whole(self):
        for item in ({'type': 'video'}, {'type': 'unknown', 'url': 'https://cdn.test/a'}):
            with self.subTest(item=item), self.assertRaises(ProviderError):
                parse_manifest({'status': 'picker', 'picker': [item]},
                               safe_url=lambda url: isinstance(url, str), max_items=10)

    def test_unsafe_destinations_and_item_overflow_are_rejected(self):
        for url in ('http://cdn.test/a', 'https://user:pass@cdn.test/a',
                    'https://127.0.0.1/a', 'https://cdn.test/a#fragment'):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                parse_manifest({'status': 'tunnel', 'filename': 'a.mp4', 'url': url},
                               safe_url=lambda value: '127.0.0.1' not in value, max_items=10)
        with self.assertRaisesRegex(ProviderError, 'configured item limit'):
            parse_manifest({'status': 'picker', 'picker': [
                {'type': 'photo', 'url': 'https://cdn.test/a'}] * 2},
                safe_url=lambda _: True, max_items=1)

    def test_local_processing_is_not_treated_as_a_complete_video(self):
        with self.assertRaises(ProviderError):
            parse_manifest({'status': 'local-processing', 'type': 'merge',
                            'tunnel': ['https://cdn.test/silent', 'https://cdn.test/audio']},
                           safe_url=lambda _: True, max_items=10)

    def test_audio_only_webm_is_checked_as_audio(self):
        entries = parse_manifest({'status': 'tunnel', 'filename': 'sound.webm',
                                  'url': 'https://cdn.test/sound.webm'},
                                 safe_url=lambda _: True, max_items=10, audio_only=True)
        self.assertEqual(entries[0]['kind'], 'audio')

    def test_unconfigured_provider_makes_no_request(self):
        with patch.dict(os.environ, {'COBALT_API_URL': ''}):
            self.assertEqual(request_manifest('https://youtube.com/watch?v=BaW_jenozKc',
                audio_only=False, quality='max', safe_url=lambda _: True, max_items=10,
                client_factory=lambda **_: self.fail('Unexpected HTTP request')), [])

    def test_authorized_request_uses_max_quality_and_no_local_processing(self):
        observed = []
        def handle(request):
            observed.append(request)
            return httpx.Response(200, json={'status': 'tunnel',
                'url': 'https://cdn.test/video.mp4', 'filename': '../../ignored.mp4'})
        def client(**kwargs):
            return httpx.Client(transport=httpx.MockTransport(handle), **kwargs)
        with patch.dict(os.environ, {'COBALT_API_URL': 'https://provider.test/',
                                     'COBALT_API_KEY': 'test-secret'}):
            entries = request_manifest('https://youtube.com/watch?v=BaW_jenozKc',
                audio_only=False, quality='max', safe_url=lambda _: True, max_items=10,
                client_factory=client)
        self.assertEqual(len(observed), 1)
        body = json.loads(observed[0].content)
        self.assertEqual(body['videoQuality'], 'max')
        self.assertEqual(body['downloadMode'], 'auto')
        self.assertEqual(body['localProcessing'], 'disabled')
        self.assertEqual(observed[0].headers['authorization'], 'Api-Key test-secret')
        self.assertEqual(entries[0]['kind'], 'video')
        self.assertNotIn('filename', entries[0])

    def test_api_redirect_never_forwards_credentials(self):
        observed = []
        def handle(request):
            observed.append(request)
            return httpx.Response(307, headers={'location': 'https://other.test/'})
        with patch.dict(os.environ, {'COBALT_API_URL': 'https://provider.test/',
                                     'COBALT_API_KEY': 'test-secret'}), self.assertRaises(ProviderError):
            request_manifest('https://instagram.com/p/test/', audio_only=False, quality='max',
                safe_url=lambda _: True, max_items=10,
                client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), **kw))
        self.assertEqual(len(observed), 1)

    def test_bad_json_and_large_response_are_bounded_and_redacted(self):
        for data in (b'not-json', b'x' * (1024 * 1024 + 1)):
            def client(**kwargs):
                return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
                    200, content=data, headers={'content-type': 'application/json'})), **kwargs)
            with patch.dict(os.environ, {'COBALT_API_URL': 'https://provider.test/',
                                         'COBALT_API_KEY': 'test-secret'}):
                with self.assertRaises(ProviderError) as caught:
                    request_manifest('https://instagram.com/p/test/', audio_only=False, quality='max',
                                     safe_url=lambda _: True, max_items=10, client_factory=client)
                self.assertNotIn('test-secret', str(caught.exception))
                self.assertNotIn('https://', str(caught.exception))


class InstagramTrackTests(unittest.TestCase):
    def test_dash_reads_full_object_audio_and_ignores_segmented_manifests(self):
        url = 'https://cdn.test/audio.m4a'
        self.assertEqual(instagram_dash_audio_urls({'video_dash_manifest': dash(url)}), [url])
        self.assertEqual(instagram_dash_audio_urls({'dash_info': {'video_dash_manifest': dash(url)}}), [url])
        self.assertEqual(instagram_dash_audio_urls({'video_dash_manifest': dash(url, segmented=True)}), [])
        self.assertEqual(instagram_dash_audio_urls({'video_dash_manifest': '<!DOCTYPE bad><MPD />'}), [])

    def test_audio_stays_with_its_carousel_child(self):
        from parth_dl import InstagramDownloader
        extractor = instagram_extractor(InstagramDownloader(quiet=True)).media_extractor
        info = extractor._parse_media_item({'code': 'test', 'carousel_media': [
            {'video_versions': [{'url': 'https://cdn.test/one.mp4'}],
             'video_dash_manifest': dash('https://cdn.test/one.m4a')},
            {'video_versions': [{'url': 'https://cdn.test/two.mp4'}]}]})
        self.assertEqual(info['audio_urls'], [])
        self.assertEqual(info['entries'][0]['audio_urls'], ['https://cdn.test/one.m4a'])
        self.assertEqual(info['entries'][1]['audio_urls'], [])

    def test_graphql_child_audio_never_becomes_post_wide_music(self):
        from parth_dl import InstagramDownloader
        extractor = instagram_extractor(InstagramDownloader(quiet=True)).media_extractor
        info = extractor._parse_graphql_media({'shortcode': 'test', 'edge_sidecar_to_children': {'edges': [
            {'node': {'is_video': True, 'video_url': 'https://cdn.test/one.mp4',
                      'dash_info': {'video_dash_manifest': dash('https://cdn.test/one.m4a')}}},
            {'node': {'is_video': True, 'video_url': 'https://cdn.test/two.mp4'}}]}})
        self.assertEqual(info['audio_urls'], [])
        self.assertEqual(info['entries'][0]['audio_urls'], ['https://cdn.test/one.m4a'])
        self.assertEqual(info['entries'][1]['audio_urls'], [])

    def test_single_video_dash_alternatives_are_not_counted_as_multiple_post_tracks(self):
        from parth_dl import InstagramDownloader
        extractor = instagram_extractor(InstagramDownloader(quiet=True)).media_extractor
        xml = dash('https://cdn.test/best.m4a').replace('</AdaptationSet>',
            '<Representation bandwidth="64000"><BaseURL>https://cdn.test/low.m4a</BaseURL>'
            '</Representation></AdaptationSet>')
        info = extractor._parse_media_item({'code': 'test',
            'video_versions': [{'url': 'https://cdn.test/video.mp4'}], 'video_dash_manifest': xml})
        self.assertEqual(info['audio_urls'], [])
        self.assertEqual(info['entries'][0]['audio_urls'],
                         ['https://cdn.test/best.m4a', 'https://cdn.test/low.m4a'])


class RealMediaFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.silent, cls.audible = cls.root / 'silent.mp4', cls.root / 'audible.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=s=160x120:r=25',
                        '-t', '0.4', '-c:v', 'libx264', str(cls.silent)], check=True, timeout=20)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(cls.silent), '-f', 'lavfi',
                        '-i', 'sine=frequency=440', '-t', '0.4', '-c:v', 'copy',
                        '-c:a', 'aac', str(cls.audible)], check=True, timeout=20)
        cls.music = cls.root / 'music.m4a'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(cls.audible), '-vn',
                        '-c:a', 'copy', str(cls.music)], check=True, timeout=20)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_one_audible_video_does_not_stop_search_for_other_tracks(self):
        with tempfile.TemporaryDirectory() as job, patch.dict(os.environ, {'COBALT_API_URL': ''}), \
             patch('bot.safe_remote_url', return_value=True), \
             patch('bot.download_instagram_dedicated', return_value=([self.audible, self.silent], 'carousel')), \
             patch('bot.download_gallery', return_value=[self.audible, self.audible]) as fallback:
            self.assertEqual(bot.grab('https://instagram.com/p/test/', 'auto', job),
                             [self.audible, self.audible])
            fallback.assert_called_once()

    def test_audio_from_shorter_carousel_does_not_replace_complete_post(self):
        complete = [self.silent, self.silent]
        with tempfile.TemporaryDirectory() as job, patch.dict(os.environ, {'COBALT_API_URL': ''}), \
             patch('bot.safe_remote_url', return_value=True), \
             patch('bot.download_instagram_dedicated', return_value=(complete, 'carousel')), \
             patch('bot.download_gallery', return_value=[self.audible]), \
             patch('bot.download_ytdlp', side_effect=RuntimeError('No accessible source')):
            self.assertEqual(bot.grab('https://instagram.com/p/test/', 'auto', job), complete)

    def test_best_partial_audio_is_retained_and_resource_limits_stay_terminal(self):
        best = [self.audible, self.silent]
        with tempfile.TemporaryDirectory() as job, patch.dict(os.environ, {'COBALT_API_URL': ''}), \
             patch('bot.safe_remote_url', return_value=True), \
             patch('bot.download_instagram_dedicated', return_value=([self.silent, self.silent], 'carousel')), \
             patch('bot.download_gallery', return_value=best), \
             patch('bot.download_ytdlp', side_effect=RuntimeError('no formats')):
            self.assertEqual(bot.grab('https://instagram.com/p/test/', 'auto', job), best)
        with tempfile.TemporaryDirectory() as job, patch('bot.safe_remote_url', return_value=True), \
             patch('bot.download_instagram_dedicated', return_value=(best, 'carousel')), \
             patch('bot.download_gallery', side_effect=RuntimeError('Job storage limit exceeded')):
            with self.assertRaisesRegex(RuntimeError, 'storage limit'):
                bot.grab('https://instagram.com/p/test/', 'auto', job)

    def test_child_track_is_muxed_only_into_that_child(self):
        info = {'type': 'carousel', 'entries': [
            {'kind': 'video', 'formats': [{'url': 'https://cdn.test/one.mp4'}],
             'audio_urls': ['https://cdn.test/one.m4a']},
            {'kind': 'video', 'formats': [{'url': 'https://cdn.test/two.mp4'}]}]}
        def download(url, output, **kwargs):
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.music if url.endswith('.m4a') else self.silent, output)
            return True
        with tempfile.TemporaryDirectory() as job, patch('bot.safe_remote_url', return_value=True), \
             patch('bot._download_direct_file', side_effect=download):
            files, _ = bot.download_instagram_dedicated('https://instagram.com/p/test/', job, info)
            self.assertEqual([bot.has_audio(p) for p in files], [True, False])
            self.assertEqual(len(bot.audio_sources(files, Path(job) / 'soundtrack')), 1)

    def test_configured_provider_recovers_after_youtube_bot_check(self):
        with tempfile.TemporaryDirectory() as job, \
             patch.dict(os.environ, {'COBALT_API_URL': 'https://provider.test/'}), \
             patch('bot.safe_remote_url', return_value=True), \
             patch('bot.download_ytdlp', side_effect=RuntimeError("Sign in to confirm you're not a bot")), \
             patch('bot.download_cobalt', return_value=[self.audible]) as provider:
            files = bot.grab('https://youtube.com/watch?v=BaW_jenozKc', 'auto', job)
            self.assertEqual(files, [self.audible])
            provider.assert_called_once()

    def test_provider_is_never_used_to_truncate_an_explicit_playlist(self):
        with tempfile.TemporaryDirectory() as job, \
             patch.dict(os.environ, {'COBALT_API_URL': 'https://provider.test/'}), \
             patch('bot.safe_remote_url', return_value=True), \
             patch('bot.download_ytdlp', side_effect=RuntimeError('source unavailable')), \
             patch('bot.download_cobalt') as provider:
            with self.assertRaises(RuntimeError):
                bot.grab('https://youtube.com/playlist?list=PLtest', 'auto', job)
            provider.assert_not_called()

    def test_provider_outage_preserves_the_actual_platform_error(self):
        with tempfile.TemporaryDirectory() as job, \
             patch.dict(os.environ, {'COBALT_API_URL': 'https://provider.test/'}), \
             patch('bot.safe_remote_url', return_value=True), \
             patch('bot.download_ytdlp', side_effect=RuntimeError("Sign in to confirm you're not a bot")), \
             patch('bot.download_cobalt', side_effect=ProviderError('Provider metadata request failed')):
            with self.assertRaisesRegex(RuntimeError, 'confirm you'):
                bot.grab('https://youtube.com/watch?v=BaW_jenozKc', 'auto', job)
