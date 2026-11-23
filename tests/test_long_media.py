import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import bot
import media_io
import render_free
from cobalt_provider import ProviderError
from source_metadata import MediaTooLong


def make_video(path: Path, seconds: float, *, size='320x240', rate=25, bitrate='600k', gop=25, extra=()):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate={rate}',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', str(seconds),
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-b:v', bitrate, '-g', str(gop),
                    '-c:a', 'aac', *extra, str(path)], check=True, timeout=120)
    return path


async def bot_module_send(chat, source: Path, folder: Path) -> int:
    return await bot.send_album(chat, [source], '', folder, {})


def moov_before_mdat(path: Path) -> bool:
    head = path.read_bytes()[:1 << 20]
    return 0 <= head.find(b'moov') < head.find(b'mdat')


class MediaFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.video = make_video(cls.root / 'clip.mp4', 12, bitrate='900k')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_cdn_suffix_is_replaced_by_the_container_extension(self):
        image = self.root / 'still.png'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(self.video), '-frames:v', '1', '-c:v', 'mjpeg',
                        str(self.root / 'still.jpg')], check=True, timeout=20)
        cases = {'.mp4': self.video, '.jpg': self.root / 'still.jpg'}
        for expected, source in cases.items():
            with self.subTest(expected=expected):
                odd = self.root / f'media{expected}.IRZXSOY'
                odd.write_bytes(source.read_bytes())
                renamed = media_io.with_canonical_suffix(odd)
                self.assertEqual(renamed.suffix, expected)
                self.assertTrue(renamed.is_file())
                self.assertFalse(odd.exists())
        known = self.root / 'known.mp4'
        known.write_bytes(self.video.read_bytes())
        self.assertEqual(media_io.with_canonical_suffix(known), known)
        audio = self.root / 'track.bin'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(self.video), '-vn', '-c:a', 'copy', '-f', 'mp4',
                        str(audio)], check=True, timeout=20)
        self.assertEqual(media_io.with_canonical_suffix(audio).suffix, '.m4a')
        del image

    def test_planned_parts_are_few_full_playable_and_fast_start(self):
        limit = int(self.video.stat().st_size * .4)
        parts = media_io.lossless_parts(self.video, self.root / 'split', limit)
        # 40 % parts: three are enough, the old 70 % interval split used four or more.
        self.assertEqual(len(parts), 3)
        total = 0
        for part in parts:
            self.assertLessEqual(part.stat().st_size, limit)
            self.assertTrue(moov_before_mdat(part))
            self.assertTrue(media_io.streamable(part))
            info = media_io.probe(part)
            self.assertTrue(any(s['codec_type'] == 'audio' for s in info['streams']))
            total += float(info['format']['duration'])
        self.assertAlmostEqual(total, 12, delta=.3)

    def test_cut_plan_uses_keyframes(self):
        cuts = media_io.cut_plan(self.video, int(self.video.stat().st_size * .3))
        self.assertGreaterEqual(len(cuts), 3)
        # testsrc2 at 25 fps with -g 25 has a keyframe on every whole second.
        for cut in cuts:
            self.assertAlmostEqual(cut, round(cut), delta=.05)

    def test_mp3_bitrate_steps_down_only_when_one_upload_cannot_hold_it(self):
        self.assertEqual(media_io.mp3_bitrate(600, 49_000_000), 320)
        self.assertEqual(media_io.mp3_bitrate(1800, 49_000_000), 192)
        self.assertEqual(media_io.mp3_bitrate(2900, 49_000_000), 128)
        # Three hours cannot fit 49 MB at any rate: keep good quality in parts.
        self.assertEqual(media_io.mp3_bitrate(3 * 3600, 49_000_000), 192)
        self.assertEqual(media_io.mp3_bitrate(3 * 3600, bot.LARGE_FILE_BYTES), 320)

    def test_rotation_swaps_display_size(self):
        rotated = self.root / 'rotated.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-display_rotation', '90', '-i', str(self.video),
                        '-c', 'copy', str(rotated)], check=True, timeout=20)
        info = media_io.video_info(rotated)
        self.assertEqual((info['width'], info['height']), (240, 320))
        self.assertAlmostEqual(info['duration'], 12, delta=.3)

    def test_expensive_vp9_conversion_is_remuxed_without_losing_frames(self):
        vp9 = self.root / 'vp9.webm'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(self.video), '-t', '2', '-c:v', 'libvpx-vp9',
                        '-deadline', 'realtime', '-cpu-used', '8', '-b:v', '300k', '-c:a', 'libopus', str(vp9)],
                       check=True, timeout=60)
        with patch('media_io.MAX_ENCODE_SECONDS', 0):
            final = media_io.native_video(vp9, self.root / 'vp9-remux')
        info = media_io.probe(final)
        self.assertEqual(final.suffix, '.mp4')
        self.assertEqual(next(s for s in info['streams'] if s['codec_type'] == 'video')['codec_name'], 'vp9')
        self.assertEqual(next(s for s in info['streams'] if s['codec_type'] == 'audio')['codec_name'], 'aac')
        self.assertTrue(moov_before_mdat(final))

    def test_thumbnail_fits_telegram_limits(self):
        thumb = media_io.video_thumbnail(self.video, self.root / 'thumb')
        self.assertIsNotNone(thumb)
        self.assertLessEqual(thumb.stat().st_size, 200_000)
        info = media_io.probe(thumb)
        self.assertLessEqual(max(info['streams'][0]['width'], info['streams'][0]['height']), 320)

    def test_counting_reader_reports_bytes_and_keeps_multipart_length(self):
        counted = []
        with self.video.open('rb') as handle:
            reader = media_io.CountingReader(handle, counted.append, 'clip.mp4')
            request = httpx.Request('POST', 'https://api.telegram.test', files={'video': ('clip.mp4', reader)})
            body = request.read()
        self.assertEqual(sum(counted), self.video.stat().st_size)
        self.assertEqual(int(request.headers['Content-Length']), len(body))
        with self.video.open('rb') as handle:
            part = media_io.FileSlice(handle, 10, 100)
            reader = media_io.CountingReader(part, counted.append)
            self.assertFalse(hasattr(reader, 'fileno'))
            self.assertEqual(len(reader.read()), 100)


class LongVideoDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sent = []

        async def video(**kwargs):
            upload = kwargs['video']
            size = (len(upload.input_file_content.read()) if not isinstance(upload, str) else 0)
            self.sent.append({'video': upload if isinstance(upload, str) else size, **{
                key: kwargs.get(key) for key in ('caption', 'width', 'height', 'duration', 'supports_streaming')}})
            return SimpleNamespace(edit_reply_markup=AsyncMock())

        self.msg = SimpleNamespace(reply_video=AsyncMock(side_effect=video), reply_media_group=AsyncMock(),
                                   reply_document=AsyncMock(), reply_photo=AsyncMock(), reply_text=AsyncMock())

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_long_video_arrives_as_numbered_playable_parts(self):
        source = make_video(self.root / 'long.mp4', 16, bitrate='900k')
        limit = int(source.stat().st_size * .35)
        progress = {}
        with patch('bot.MAX_BYTES', limit):
            sent = await bot.send_album(self.msg, [source], 'caption', self.root / 'job', progress)
        self.assertEqual(sent, len(self.sent))
        self.assertGreaterEqual(sent, 3)
        self.msg.reply_media_group.assert_not_called()
        for number, item in enumerate(self.sent, 1):
            self.assertLessEqual(item['video'], limit)
            self.assertIn(f'Part {number}/{sent}', item['caption'])
            self.assertEqual((item['width'], item['height']), (320, 240))
            self.assertTrue(item['supports_streaming'])
        self.assertTrue(self.sent[0]['caption'].startswith('caption'))
        self.assertAlmostEqual(sum(item['duration'] for item in self.sent), 16, delta=sent)
        self.assertEqual(progress['sent'], sent)

    async def test_single_video_request_matches_the_bot_api_layout(self):
        from telegram import Bot
        from telegram.request import BaseRequest

        source = make_video(self.root / 'single.mp4', 4, bitrate='900k')
        captured = {}

        class Recorder(BaseRequest):
            async def initialize(self):
                pass

            async def shutdown(self):
                pass

            async def do_request(self, url, method, request_data=None, **kwargs):
                captured['method'] = url.rsplit('/', 1)[-1]
                captured['json'] = request_data.json_parameters
                captured['files'] = {name: len(field[1].read()) if hasattr(field[1], 'read') else len(field[1])
                                     for name, field in (request_data.multipart_data or {}).items()}
                body = ('{"ok":true,"result":{"message_id":1,"date":0,"chat":{"id":42,"type":"private"}}}')
                return 200, body.encode()

        bot = Bot('123456:test-token', request=Recorder(), get_updates_request=Recorder())
        bot._initialized = True
        chat = SimpleNamespace(reply_video=lambda **kw: bot.send_video(42, **kw))
        self.assertEqual(await bot_module_send(chat, source, self.root / 'job'), 1)
        self.assertEqual(captured['method'], 'sendVideo')
        params = captured['json']
        self.assertEqual((params['width'], params['height'], params['duration']), ('320', '240', '4'))
        self.assertEqual(params['supports_streaming'], 'true')
        self.assertTrue(params['thumbnail'].startswith('attach://'))
        attach = params['thumbnail'].removeprefix('attach://')
        self.assertGreater(captured['files'][attach], 0)
        self.assertEqual(captured['files']['video'], source.stat().st_size)

    async def test_large_route_sends_big_parts_by_file_id(self):
        source = make_video(self.root / 'big.mp4', 6, bitrate='900k')
        uploads = []

        async def upload(path, **kwargs):
            uploads.append((path.stat().st_size, kwargs['kind'], kwargs['width'], kwargs['height']))
            return 'BAACAgIAAxkCAAIKK2X02MGi8OHS'

        with patch('bot.MAX_BYTES', 1000), patch('bot.LARGE_FILE_BYTES', 50_000_000), \
                patch('telegram_mtproto.available', return_value=True), \
                patch('telegram_mtproto.upload', side_effect=upload):
            sent = await bot.send_album(self.msg, [source], '', self.root / 'job')
        self.assertEqual(sent, 1)
        self.assertEqual(uploads, [(source.stat().st_size, 'video', 320, 240)])
        self.assertEqual(self.sent[0]['video'], 'BAACAgIAAxkCAAIKK2X02MGi8OHS')

    async def test_failed_large_upload_falls_back_to_bot_api_parts(self):
        source = make_video(self.root / 'fallback.mp4', 12, bitrate='900k')
        limit = int(source.stat().st_size * .4)
        with patch('bot.MAX_BYTES', limit), patch('bot.LARGE_FILE_BYTES', 50_000_000), \
                patch('telegram_mtproto.available', return_value=True), \
                patch('telegram_mtproto.upload', side_effect=ConnectionError('mtproto down')), \
                patch('telegram_mtproto.mark_failed') as paused:
            sent = await bot.send_album(self.msg, [source], '', self.root / 'job')
        paused.assert_called_once()
        self.assertGreaterEqual(sent, 3)
        self.assertTrue(all(isinstance(item['video'], int) and item['video'] <= limit for item in self.sent))

    async def test_long_audio_uses_a_bitrate_that_fits_one_upload(self):
        audio = self.root / 'podcast.m4a'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=300:sample_rate=44100',
                        '-t', '30', '-c:a', 'aac', str(audio)], check=True, timeout=30)
        captured = []

        async def receive(msg, outputs, caption, tmp, progress=None):
            captured.extend(outputs)
            return len(outputs)

        # 30 s at 320 kbps is 1.2 MB; a 1 MB limit must select 256 kbps or lower.
        with patch('bot.delivery_limit', return_value=1_000_000), patch('bot.send_album', side_effect=receive):
            await bot.send_mp3_album(None, [audio], self.root, meta={'title': 'Long talk', 'uploader': 'Host'})
        info = media_io.probe(captured[0])
        self.assertLessEqual(int(info['format']['bit_rate']), 270_000)
        self.assertEqual(info['format']['tags']['title'], 'Long talk')
        self.assertEqual(info['format']['tags']['artist'], 'Host')
        self.assertEqual(captured[0].name, 'Long talk.mp3')
        self.assertEqual(bot.audio_tags(captured[0])['performer'], 'Host')


class SelectionTests(unittest.TestCase):
    def raw(self, duration):
        def fmt(height, codec, tbr, width=None):
            return {'format_id': f'{codec}{height}', 'height': height, 'width': width or height * 16 // 9,
                    'vcodec': codec, 'acodec': 'none', 'tbr': tbr}
        return {'duration': duration, 'formats': [
            fmt(2160, 'vp09', 18000), fmt(1080, 'avc1.640028', 4500), fmt(720, 'avc1.4d401f', 2500),
            fmt(480, 'avc1.4d401e', 1200), fmt(360, 'avc1.4d401e', 650),
            {'format_id': '140', 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'tbr': 130},
        ]}

    def test_short_video_keeps_the_best_playable_and_original_rendition(self):
        # Playable delivery stays on H.264 (1080p); /original may take 4K VP9.
        self.assertEqual(bot.youtube_height_cap(self.raw(300), 'auto', 980_000_000), 1080)
        self.assertEqual(bot.youtube_height_cap(self.raw(300), 'original', 4_000_000_000), 2160)

    def test_three_hour_video_is_capped_to_fit_delivery(self):
        hosted = 20 * bot.MAX_BYTES
        large = 4 * bot.LARGE_FILE_BYTES
        # Even 360p exceeds twenty 49 MB parts: the smallest rendition is used.
        self.assertEqual(bot.youtube_height_cap(self.raw(3 * 3600), 'auto', hosted), 360)
        self.assertEqual(bot.youtube_height_cap(self.raw(3600), 'auto', hosted), 480)
        self.assertEqual(bot.youtube_height_cap(self.raw(3 * 3600), 'auto', large), 1080)
        self.assertEqual(bot.youtube_height_cap(self.raw(3 * 3600), 'original', 4 * 1024 ** 3), 720)

    def test_vertical_shorts_rank_by_their_short_side(self):
        raw = {'duration': 30, 'formats': [
            {'height': 1920, 'width': 1080, 'vcodec': 'avc1', 'acodec': 'none', 'tbr': 9000},
            {'height': 1280, 'width': 720, 'vcodec': 'avc1', 'acodec': 'none', 'tbr': 3000}]}
        self.assertEqual(bot.youtube_height_cap(raw, 'auto', 40_000_000), 1080)
        self.assertEqual(bot.youtube_height_cap(raw, 'auto', 20_000_000), 720)
        # Without published formats the conservative bitrate table decides.
        self.assertEqual(bot.youtube_height_cap({'duration': 30, 'formats': []}, 'auto', 20_000_000), 1080)
        self.assertEqual(bot.youtube_height_cap({'formats': []}, 'auto', 20_000_000), 4320)

    def test_format_sort_prefers_h264_for_playable_delivery(self):
        self.assertEqual(bot.youtube_format_sort('auto', 720)[:2], ['vcodec:h264', 'res:720'])
        self.assertEqual(bot.youtube_format_sort('original', 2160)[0], 'res:2160')

    def test_live_and_overlong_media_are_rejected_before_download(self):
        ydl = SimpleNamespace(params={})
        with self.assertRaisesRegex(RuntimeError, 'live stream has not ended'):
            bot.prepare_youtube_selection(ydl, {'live_status': 'is_live'}, 'auto', tempfile.gettempdir())
        with self.assertRaises(MediaTooLong):
            bot.prepare_youtube_selection(ydl, {'duration': bot.MAX_MEDIA_SECONDS + 1}, 'auto', tempfile.gettempdir())
        bot.prepare_youtube_selection(ydl, self.raw(3 * 3600), 'auto', tempfile.gettempdir())
        self.assertEqual(ydl.params['format_sort'][0], 'vcodec:h264')
        self.assertTrue(bot.fatal_download_error(MediaTooLong('x')))
        self.assertIn('4 hours', bot.friendly_error('youtube', MediaTooLong('x')))

    def test_budget_reflects_the_upload_route(self):
        with patch('bot.mtproto_configured', return_value=False), patch('shutil.disk_usage',
                                                                         return_value=SimpleNamespace(free=10 ** 12)):
            self.assertEqual(bot.delivery_budget('auto'), 20 * bot.MAX_BYTES)
            self.assertEqual(bot.delivery_budget('original'), bot.MAX_SOURCE_BYTES)
        with patch('bot.mtproto_configured', return_value=True), patch('shutil.disk_usage',
                                                                        return_value=SimpleNamespace(free=10 ** 12)):
            self.assertEqual(bot.delivery_budget('auto'), bot.MAX_SOURCE_BYTES)
        with patch('bot.mtproto_configured', return_value=True), patch('shutil.disk_usage',
                                                                        return_value=SimpleNamespace(free=1_000_000_000)):
            self.assertEqual(bot.delivery_budget('auto'), 420_000_000)

    def test_public_player_media_refusal_keeps_the_platform_error(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('bot.DATA_DIR', Path(folder)), \
                patch('youtube_public.request_manifest', return_value={'id': 'BaW_jenozKc', 'formats': []}), \
                patch('bot.download_ytdlp', side_effect=RuntimeError('unable to download video data: HTTP Error 403: Forbidden')):
            with self.assertRaises(ProviderError):
                bot.download_youtube_public('https://youtu.be/BaW_jenozKc', 'auto', folder)
            self.assertTrue((Path(folder) / 'youtube_public_cooldown').is_file())


class ProgressTests(unittest.TestCase):
    def test_worker_progress_is_shared_through_the_job_folder(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'VELTRIX_JOB_ROOT': folder}):
            bot._progress_state.update(last=0.0, phase='')
            bot.ytdlp_progress({'status': 'downloading', 'downloaded_bytes': 50, 'total_bytes': 200,
                                'speed': 10, 'info_dict': {'vcodec': 'avc1'}})
            data = bot.read_worker_progress(Path(folder))
        self.assertEqual((data['phase'], data['done'], data['total'], data['label']), ('download', 50, 200, 'video'))

    def test_status_text_shows_percent_speed_and_eta(self):
        worker = {'phase': 'download', 'done': 25 * 1024 ** 2, 'total': 100 * 1024 ** 2, 'speed': 5 * 1024 ** 2,
                  'label': 'video', 'at': 1}
        text = bot.progress_text('YouTube', {'stage': 'download'}, worker, 12, [])
        self.assertIn('25%', text)
        self.assertIn('25.0 MB / 100.0 MB', text)
        self.assertIn('5.0 MB/s', text)
        self.assertIn('~0:15 left', text)
        upload = bot.progress_text('YouTube', {'stage': 'upload', 'phase': 'Sending part 1/3', 'up_done': 50,
                                               'up_total': 100, 'up_started': 0}, {}, 5, [])
        self.assertIn('50%', upload)
        self.assertIn('Sending part 1/3', upload)
        self.assertIn('the download service is preparing',
                      bot.progress_text('YouTube', {'stage': 'download'}, {'phase': 'provider'}, 3, []))

    def test_titles_drop_hashtags_and_generic_names(self):
        self.assertEqual(bot.clean_title('And thank god #creative #viral\nsecond line'), 'And thank god')
        self.assertEqual(bot.clean_title('Spotlight Snap'), '')
        self.assertEqual(bot.clean_title('Media by someone'), '')
        self.assertEqual(bot.clean_title('Lofi beats · 3 hours'), 'Lofi beats · 3 hours')

    def test_caption_and_summary(self):
        self.assertEqual(bot.delivery_caption('https://www.instagram.com/p/x/', {'title': 'Media'}),
                         '⚡ Veltrix Downloader · Instagram')
        caption = bot.delivery_caption('https://youtu.be/x', {'title': 'A' * 150}, 1)
        self.assertTrue(caption.startswith('🎬 ' + 'A' * 97 + '…'))
        self.assertIn('🔇', caption)
        self.assertEqual(bot.delivered_summary(['image', 'video', 'video'], 3), '✅ Delivered · 1 photo, 2 videos')
        self.assertEqual(bot.delivered_summary(['video'], 4), '✅ Delivered · 1 video in 4 messages')


class KeepAwakeTests(unittest.TestCase):
    def test_pings_only_while_work_is_active(self):
        requests = []

        def handle(request):
            requests.append(str(request.url))
            return httpx.Response(404)

        real_client = httpx.Client
        clock = iter(range(0, 100000, 60))
        with patch('httpx.Client', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handle))), \
                patch.dict('os.environ', {'KEEP_AWAKE': ''}):
            with patch('bot.metric_snapshot', return_value={'active_jobs': 0, 'queued_jobs': 0}):
                render_free.keep_awake('https://bot.test/', sleep=lambda _: None, clock=lambda: next(clock), rounds=3)
            self.assertEqual(requests, [])
            with patch('bot.metric_snapshot', return_value={'active_jobs': 1, 'queued_jobs': 0}):
                render_free.keep_awake('https://bot.test/', sleep=lambda _: None, clock=lambda: next(clock), rounds=6)
        # First round pings, then every four minutes (60 s per round).
        self.assertEqual(requests, ['https://bot.test/', 'https://bot.test/'])


if __name__ == '__main__':
    unittest.main()


class DeliveryFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_mp3_button_keeps_post_title_and_creator(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = make_video(root / 'clip.mp4', 1)
            with patch('bot.DATA_DIR', root), patch('bot.CACHE_DIR', root / 'cache'), \
                    patch('bot.ACTIONS_FILE', root / 'actions.json'), patch('bot.BOT_TOKEN', '123:test'):
                markup = bot.post_mp3_button(42, 'https://www.instagram.com/reel/abc/', [source], 'Robert and mom',
                                             uploader='family.page')
                token = markup.inline_keyboard[0][0].callback_data.split('|')[2]
                action = bot.resolve_action_data(42, token)
                self.assertEqual((action['title'], action['uploader']), ('Robert and mom', 'family.page'))
                manager = bot.JobManager(root / 'jobs.sqlite3')
                msg = SimpleNamespace(chat_id=42, reply_text=AsyncMock())
                with patch('bot._jobs', manager), patch('bot.run_job', new_callable=AsyncMock) as job:
                    await bot.dispatch_job(msg, None, 42, action['url'], 'mp3_320', action=action)
                    await asyncio.gather(*list(manager.tasks.values()))
        self.assertEqual(job.call_args.kwargs['known_meta'], {'title': 'Robert and mom', 'uploader': 'family.page'})
        self.assertEqual(len(job.call_args.kwargs['cached_sources']), 1)

    def test_mp3_names_never_fall_back_to_unknown_artist(self):
        self.assertEqual(bot.mp3_meta({'title': 'Media'}, 'Instagram'),
                         {'title': 'Instagram audio', 'uploader': 'Instagram'})
        self.assertEqual(bot.mp3_meta({'title': 'Surah Al-Hashr', 'uploader': 'Ahmed'}, 'YouTube'),
                         {'title': 'Surah Al-Hashr', 'uploader': 'Ahmed'})
        self.assertEqual(bot.mp3_meta({}, 'Unsupported')['uploader'], 'Veltrix')

    def test_messages_explain_silent_sources_and_unavailable_media(self):
        from source_metadata import PublicMediaUnavailable
        self.assertIn('Pinterest provides 3 of these videos without sound',
                      bot.delivery_caption('https://pin.it/x', {}, 3))
        self.assertIn('Instagram provides this video without sound',
                      bot.delivery_caption('https://www.instagram.com/reel/x/', {}, 1))
        snap = bot.friendly_error('snapchat', PublicMediaUnavailable('x'))
        self.assertIn('only in the app', snap)
        quota = bot.friendly_error('youtube', bot.VidFetchQuotaExceeded('VidFetch create HTTP 402'))
        self.assertIn('allowance is used up', quota)
        self.assertIn('Instagram, Snapchat and Pinterest links still work', quota)

    def test_youtube_providers_back_up_an_expired_cookie(self):
        with patch.dict('os.environ', {'YOUTUBE_COOKIES_B64': 'x', 'YOUTUBE_COOKIE_FILE': '/tmp/cookies',
                                       'VIDFETCH_API_KEY': 'key', 'NEWISTY_YOUTUBE': '1'}):
            self.assertTrue(bot.vidfetch_enabled())
            self.assertTrue(bot.newisty_enabled())
        with patch.dict('os.environ', {'VIDFETCH_API_KEY': '', 'NEWISTY_YOUTUBE': '0'}):
            self.assertFalse(bot.vidfetch_enabled())
            self.assertFalse(bot.newisty_enabled())
