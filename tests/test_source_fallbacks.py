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
from media_io import probe, same_video_stream
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
    def test_dash_only_carousel_video_uses_highest_representation_instead_of_poster(self):
        from parth_dl import InstagramDownloader
        extractor = instagram_extractor(InstagramDownloader(quiet=True)).media_extractor
        manifest = """<MPD><Period>
        <AdaptationSet mimeType="video/mp4" frameRate="60000/1001">
          <Representation width="720" height="1280"><BaseURL>https://cdn.test/720.mp4</BaseURL></Representation>
          <Representation width="1080" height="1920"><BaseURL>https://cdn.test/1080.mp4</BaseURL></Representation>
          <Representation width="2160" height="3840"><ContentProtection/>
            <BaseURL>https://cdn.test/protected.mp4</BaseURL></Representation>
        </AdaptationSet></Period></MPD>"""
        child = {'media_type': 2, 'video_dash_manifest': manifest,
                 'image_versions2': {'candidates': [{'url': 'https://cdn.test/poster.jpg'}]}}
        data = extractor._parse_media_item({'code': 'test', 'carousel_media': [child]})
        entry, = data['entries']
        self.assertEqual(entry['kind'], 'video')
        self.assertEqual(set(row['url'] for row in entry['formats']),
                         {'https://cdn.test/720.mp4', 'https://cdn.test/1080.mp4'})
        with patch('bot.download_source_entries', return_value=[]) as download:
            bot.download_instagram_dedicated('https://instagram.com/p/test/', '.', data)
        self.assertEqual(download.call_args.args[0][0]['formats'][0]['url'], 'https://cdn.test/1080.mp4')

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
        cls.second_silent, cls.second_audible = cls.root / 'second_silent.mp4', cls.root / 'second_audible.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=s=160x120:r=25',
                        '-t', '0.4', '-c:v', 'libx264', str(cls.silent)], check=True, timeout=20)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(cls.silent), '-f', 'lavfi',
                        '-i', 'sine=frequency=440', '-t', '0.4', '-c:v', 'copy',
                        '-c:a', 'aac', str(cls.audible)], check=True, timeout=20)
        cls.music = cls.root / 'music.m4a'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(cls.audible), '-vn',
                        '-c:a', 'copy', str(cls.music)], check=True, timeout=20)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=red:s=160x120:r=25',
                        '-t', '1.6', '-c:v', 'libx264', str(cls.second_silent)], check=True, timeout=20)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(cls.second_silent), '-f', 'lavfi',
                        '-i', 'sine=frequency=660', '-t', '1.6', '-c:v', 'copy',
                        '-c:a', 'aac', str(cls.second_audible)], check=True, timeout=20)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_source_tries_next_rendition_after_invalid_file(self):
        with tempfile.TemporaryDirectory() as folder:
            entries = [{'kind': 'video', 'formats': [{'url': 'https://cdn.test/broken.mp4'},
                                                     {'url': 'https://cdn.test/valid.mp4'}]}]

            def receive(url, path, **kwargs):
                if 'broken.mp4' in url:
                    path.write_bytes(b'this is not a video')
                else:
                    shutil.copyfile(self.audible, path)
                return True

            with patch('bot.safe_remote_url', return_value=True), patch('bot._download_direct_file', side_effect=receive):
                output, = bot.download_source_entries(entries, Path(folder) / 'media',
                                                      'https://instagram.com/reel/example/')
            self.assertTrue(bot.has_audio(output))
            self.assertEqual(output.read_bytes(), self.audible.read_bytes())

    def test_matching_item_audio_keeps_both_encoded_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            output = bot.attach_post_music(self.silent, self.music, Path(directory), loop_audio=False)

            def audio_hash(path):
                return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0',
                                                '-c', 'copy', '-f', 'hash', '-'], timeout=10)

            self.assertTrue(same_video_stream(self.silent, output))
            self.assertEqual(audio_hash(output), audio_hash(self.music))

    def test_one_audible_video_does_not_stop_search_for_other_tracks(self):
        with tempfile.TemporaryDirectory() as job, patch.dict(os.environ, {'COBALT_API_URL': ''}), \
             patch('bot.safe_remote_url', return_value=True), \
             patch('bot.download_instagram_dedicated', return_value=([self.audible, self.silent], 'carousel')), \
             patch('bot.download_gallery', return_value=[self.audible, self.audible]) as fallback:
            self.assertEqual(bot.grab('https://instagram.com/p/test/', 'auto', job),
                             [self.audible, self.audible])
            fallback.assert_called_once()

    def test_complementary_fallbacks_keep_both_matching_videos_with_audio(self):
        original = [self.audible, self.second_silent]
        for mode in ('auto', 'mp3_320'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as job, \
                 patch.dict(os.environ, {'COBALT_API_URL': ''}), \
                 patch('bot.safe_remote_url', return_value=True), \
                 patch('bot.download_instagram_dedicated', return_value=(original, 'carousel')), \
                 patch('bot.download_gallery', return_value=[self.silent, self.second_audible]):
                self.assertEqual(bot.grab('https://instagram.com/p/test/', mode, job),
                                 [self.audible, self.second_audible])

    def test_silent_instagram_embed_can_use_exact_public_graphql_rendition(self):
        url = 'https://instagram.com/reel/test/'
        with tempfile.TemporaryDirectory() as job, patch.dict(os.environ, {'COBALT_API_URL': ''}), \
             patch('bot.safe_remote_url', return_value=True), patch('bot.download_gallery', return_value=[]), \
             patch('bot.download_instagram_dedicated', side_effect=[([self.silent], 'video'),
                                                                   ([self.audible], 'video')]) as dedicated, \
             patch('bot.download_ytdlp') as fallback:
            self.assertEqual(bot.grab(url, 'auto', job), [self.audible])
            self.assertTrue(dedicated.call_args.kwargs['graphql'])
            fallback.assert_not_called()

    def test_reel_accepts_a_verified_mixed_carousel_but_rejects_its_standalone_poster(self):
        from PIL import Image

        url = 'https://www.instagram.com/reel/album/'
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'photo.jpg'
            Image.new('RGB', (64, 64)).save(image)
            with patch.dict(os.environ, {'COBALT_API_URL': '', 'INSTAGRAM_COOKIE_FILE': ''}), \
                 patch('bot.safe_remote_url', return_value=True), patch('bot.download_gallery', return_value=[]), \
                 patch('bot.download_ytdlp', return_value=[]), patch('bot.download_instagram_dedicated') as dedicated:
                dedicated.return_value = ([image, self.audible], 'carousel')
                self.assertEqual(bot.grab(url, 'auto', folder), [image, self.audible])
                dedicated.return_value = ([image], 'image')
                with self.assertRaisesRegex(RuntimeError, 'poster instead of video'):
                    bot.grab(url, 'auto', folder)

    def test_unmatched_video_does_not_replace_another_carousel_child(self):
        original = [self.audible, self.second_silent]
        with tempfile.TemporaryDirectory() as job, patch.dict(os.environ, {'COBALT_API_URL': ''}), \
             patch('bot.safe_remote_url', return_value=True), \
             patch('bot.download_instagram_dedicated', return_value=(original, 'carousel')), \
             patch('bot.download_gallery', return_value=[self.second_silent, self.audible]), \
             patch('bot.download_ytdlp', return_value=[]):
            self.assertEqual(bot.grab('https://instagram.com/p/test/', 'auto', job), original)

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

    def test_best_instagram_video_can_use_separate_audio_before_smaller_rendition(self):
        info = {'type': 'video', 'entries': [{
            'kind': 'video', 'audio_urls': ['https://cdn.test/source.m4a'], 'formats': [
                {'url': 'https://cdn.test/lower.mp4', 'width': 640, 'height': 480, 'has_audio': True},
                {'url': 'https://cdn.test/best.mp4', 'width': 1920, 'height': 1080, 'has_audio': False},
            ],
        }]}
        sources = {'best.mp4': self.second_silent, 'lower.mp4': self.audible, 'source.m4a': self.music}
        requested = []

        def download(url, output, **kwargs):
            name = url.rsplit('/', 1)[-1]
            requested.append(name)
            shutil.copyfile(sources[name], output)
            return True

        with tempfile.TemporaryDirectory() as job, patch('bot.safe_remote_url', return_value=True), \
             patch('bot._download_direct_file', side_effect=download):
            files, _ = bot.download_instagram_dedicated('https://instagram.com/reel/test/', job, info)
            self.assertEqual(requested, ['best.mp4', 'source.m4a'])
            self.assertTrue(same_video_stream(files[0], self.second_silent))
            self.assertTrue(bot.has_audio(files[0]))

    def test_partial_download_can_recover_only_the_complete_carousel(self):
        from source_metadata import IncompletePost

        layout = ('video', 'video')
        for replacement in ([self.audible], [self.audible, self.second_audible]):
            with self.subTest(size=len(replacement)), tempfile.TemporaryDirectory() as job, \
                 patch.dict(os.environ, {'COBALT_API_URL': ''}), patch('bot.safe_remote_url', return_value=True), \
                 patch('bot.download_instagram_dedicated', side_effect=IncompletePost('Incomplete carousel', layout)), \
                 patch('bot.download_gallery', return_value=replacement), patch('bot.download_ytdlp', return_value=[]):
                if len(replacement) < 2:
                    with self.assertRaises(IncompletePost):
                        bot.grab('https://instagram.com/p/test/', 'auto', job)
                else:
                    self.assertEqual(bot.grab('https://instagram.com/p/test/', 'auto', job), replacement)

    def test_snapchat_hls_uses_video_download_and_checks_its_audio(self):
        media = {'entries': [{'kind': 'video', 'url': 'https://cdn.test/spotlight/master.m3u8?sig=fake'}]}
        options = []

        class PlaylistDownloader:
            def __init__(self, opts):
                options.append(opts)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, *, download):
                self.assertion = (url, download)
                path = Path(options[-1]['outtmpl'].replace('%(ext)s', 'mp4'))
                shutil.copyfile(RealMediaFallbackTests.audible, path)
                return {'requested_downloads': [{'filepath': str(path)}]}

        with tempfile.TemporaryDirectory() as job, patch('bot.safe_remote_url', return_value=True), \
             patch('bot.YoutubeDL', PlaylistDownloader), patch('bot._download_direct_file') as direct:
            files = bot.download_snapchat_dedicated('https://snapchat.com/spotlight/test', job, media)
            direct.assert_not_called()
            self.assertTrue(bot.has_audio(files[0]))
            self.assertEqual(options[0]['http_headers']['Referer'], 'https://www.snapchat.com/')

    def test_instagram_gallery_uses_embedded_dash_and_keeps_original_track_for_mp3(self):
        manifest = [[3, 'ytdl:https://www.instagram.com/p/test/1.mp4', {
            '_ytdl_manifest': 'dash', '_ytdl_manifest_data': dash('https://cdn.test/music.m4a'),
            '_fallback': ['https://cdn.test/video.mp4']}]]
        observed = []

        def download(url, output, **kwargs):
            observed.append(url)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.music if url.endswith('.m4a') else self.second_silent, output)
            return True

        with tempfile.TemporaryDirectory() as job, patch('bot.safe_remote_url', return_value=True), \
             patch('bot.subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(manifest))) as cli, \
             patch('bot._download_direct_file', side_effect=download):
            files = bot.download_gallery('https://instagram.com/p/test/', job)
            self.assertIn('output.private=true', cli.call_args.args[0])
            self.assertEqual(observed, ['https://cdn.test/video.mp4', 'https://cdn.test/music.m4a'])
            self.assertTrue(bot.has_audio(files[0]))
            self.assertTrue(same_video_stream(self.second_silent, files[0]))
            self.assertAlmostEqual(float(probe(files[0])['format']['duration']), 1.6, delta=0.1)
            sources = bot.audio_sources(files, Path(job) / 'soundtrack')
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].read_bytes(), self.music.read_bytes())

    def test_pinterest_music_does_not_replace_distinct_videos_or_surface_invalid_tracks(self):
        entries = [{'kind': 'video', 'formats': [{'url': f'https://cdn.test/video_{index}.mp4'}]}
                   for index in range(2)]
        entries.extend({'kind': 'audio', 'scope': 'post', 'formats': [{'url': f'https://cdn.test/{name}.m4a'}]}
                       for name in ('expired', 'music'))

        def download(url, output, **kwargs):
            if 'expired' in url:
                shutil.copy2(self.silent, output)
            else:
                source = self.music if 'music' in url else self.silent if '_0.' in url else self.second_silent
                shutil.copy2(source, output)
            return True

        with tempfile.TemporaryDirectory() as job, patch('bot.safe_remote_url', return_value=True), \
             patch('bot._download_direct_file', side_effect=download):
            files = bot.download_source_entries(entries, Path(job) / 'pinterest', 'https://pinterest.com/pin/42/')
            self.assertEqual(len(set(files)), 2)
            self.assertTrue(all(bot.has_audio(path) for path in files))
            self.assertTrue(same_video_stream(files[0], self.silent))
            self.assertTrue(same_video_stream(files[1], self.second_silent))
            self.assertEqual(len(bot.audio_sources(files, Path(job) / 'soundtrack')), 1)

    def test_configured_provider_recovers_after_youtube_bot_check(self):
        with tempfile.TemporaryDirectory() as job, \
             patch.dict(os.environ, {'COBALT_API_URL': 'https://provider.test/'}), \
             patch('bot.safe_remote_url', return_value=True), \
             patch('bot.download_ytdlp', side_effect=RuntimeError("Sign in to confirm you're not a bot")), \
             patch('bot.download_cobalt', return_value=[self.audible]) as provider:
            with patch('bot.download_youtube_public', return_value=[]):
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
            with self.assertRaisesRegex(RuntimeError, 'confirm you'), patch('bot.download_youtube_public', return_value=[]):
                bot.grab('https://youtube.com/watch?v=BaW_jenozKc', 'auto', job)

    def test_best_video_keeps_its_frames_and_original_audio_from_smaller_variant(self):
        with tempfile.TemporaryDirectory() as job:
            larger = Path(job) / 'larger.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(self.silent),
                            '-vf', 'scale=320:240', '-c:v', 'libx264', str(larger)], check=True, timeout=20)
            sources = {'best.mp4': larger, 'small.mp4': self.audible}
            def download(url, output, **kwargs):
                shutil.copy2(sources[url.rsplit('/', 1)[-1]], output)
                return True
            entries = [{'kind': 'video', 'formats': [{'url': 'https://cdn.test/best.mp4'},
                                                    {'url': 'https://cdn.test/small.mp4'}]}]
            with patch('bot.safe_remote_url', return_value=True), patch('bot._download_direct_file', side_effect=download):
                files = bot.download_source_entries(entries, Path(job) / 'source', 'https://instagram.com/p/test/')
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].is_file())
            self.assertTrue(bot.has_audio(files[0]))
            self.assertTrue(same_video_stream(files[0], larger))
            self.assertEqual(len(bot.audio_sources(files, Path(job) / 'soundtrack')), 1)
            def audio_hash(path):
                return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0',
                                                '-c', 'copy', '-f', 'hash', '-'], timeout=20)
            self.assertEqual(audio_hash(files[0]), audio_hash(self.audible))

    def test_different_variant_timeline_is_not_given_another_audio_track(self):
        from media_io import restore_variant_audio
        with tempfile.TemporaryDirectory() as job:
            larger = Path(job) / 'larger.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(self.second_silent),
                            '-vf', 'scale=320:240', '-c:v', 'libx264', str(larger)], check=True, timeout=20)
            self.assertEqual(restore_variant_audio(larger, self.audible, Path(job) / 'merged'), self.audible)

    def test_public_youtube_recovers_after_default_bot_check(self):
        with tempfile.TemporaryDirectory() as job, patch.dict(os.environ, {'COBALT_API_URL': ''}), \
             patch('bot.safe_remote_url', return_value=True), \
             patch('bot.download_ytdlp', side_effect=RuntimeError("Sign in to confirm you're not a bot")), \
             patch('bot.download_youtube_public', return_value=[self.audible]) as fallback:
            self.assertEqual(bot.grab('https://youtube.com/watch?v=u443WxCOAlY', 'auto', job), [self.audible])
            fallback.assert_called_once()

    def test_public_player_outage_preserves_original_bot_check(self):
        with tempfile.TemporaryDirectory() as job, patch.dict(os.environ, {'COBALT_API_URL': ''}), \
             patch('bot.safe_remote_url', return_value=True), \
             patch('bot.download_ytdlp', side_effect=RuntimeError("Sign in to confirm you're not a bot")), \
             patch('bot.download_youtube_public', side_effect=ProviderError('Public player HTTP403')):
            with self.assertRaisesRegex(RuntimeError, 'confirm you'):
                bot.grab('https://youtube.com/watch?v=u443WxCOAlY', 'auto', job)
