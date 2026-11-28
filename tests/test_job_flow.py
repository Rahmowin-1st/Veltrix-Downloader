import asyncio
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import media_io
from telegram.error import TimedOut, BadRequest, RetryAfter


class StatusUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_card_changes_every_second_then_stops_on_receipt(self):
        edits = []
        two_edits = asyncio.Event()

        async def edit(text, **kwargs):
            edits.append((time.monotonic(), text))
            if len(edits) == 2:
                two_edits.set()

        msg = SimpleNamespace(chat_id=42, reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=edit)))
        status = bot.JobStatus(msg, 'YouTube')
        status.start({'stage': 'download'}, lambda: {'phase': 'download', 'done': 50, 'total': 100})
        try:
            await asyncio.wait_for(two_edits.wait(), 3.5)
            await status.complete('✅', 'Media delivered', '1 video')
        finally:
            await status.stop()
        msg.reply_text.assert_awaited_once()
        self.assertIn('50%', edits[0][1])
        self.assertNotEqual(edits[0][1], edits[1][1])
        self.assertGreaterEqual(edits[1][0] - edits[0][0], 0.98)
        self.assertIn('Media delivered', edits[-1][1])
        self.assertIsNone(status.task)

    async def test_inflight_edit_finishes_before_the_receipt(self):
        editing, release = asyncio.Event(), asyncio.Event()
        edits = []

        async def edit(text, **kwargs):
            if 'Working' in text:
                editing.set()
                await release.wait()
            edits.append(text)

        status = bot.JobStatus(SimpleNamespace(chat_id=42), 'YouTube', SimpleNamespace(edit_text=edit))
        status.rate.interval = 0.01
        status.start({})
        await asyncio.wait_for(editing.wait(), 1)
        receipt = asyncio.create_task(status.complete('✅', 'Media delivered'))
        await asyncio.sleep(0)
        self.assertFalse(receipt.done())
        release.set()
        await asyncio.wait_for(receipt, 1)
        self.assertIn('Working', edits[0])
        self.assertIn('Media delivered', edits[1])

    async def test_telegram_flood_wait_is_shared_with_other_jobs_in_the_chat(self):
        msg = SimpleNamespace(chat_id=-42)
        first = bot.JobStatus(msg, 'YouTube', SimpleNamespace(edit_text=AsyncMock(side_effect=RetryAfter(30))))
        second = bot.JobStatus(msg, 'Pinterest', SimpleNamespace(edit_text=AsyncMock()))
        await first._update(lambda: 'Working')
        self.assertIs(first.rate, second.rate)
        self.assertEqual(second.rate.interval, 4.0)
        self.assertGreater(second.rate.next_edit - time.monotonic(), 29)
        await second._update(lambda: 'Working')
        second.message.edit_text.assert_not_awaited()

    async def test_premium_rejection_retries_the_same_status_with_standard_emoji(self):
        msg = SimpleNamespace(chat_id=42)
        edit = AsyncMock(side_effect=[BadRequest('Custom emoji forbidden'), None])
        status = bot.JobStatus(msg, 'YouTube', SimpleNamespace(edit_text=edit))
        with patch('bot._status_emojis', {'✅': ('✅', '1234')}), patch('bot._status_emoji_enabled', True):
            await status.complete('✅', 'Media delivered', 'Video <1>')
            self.assertFalse(bot._status_emoji_enabled)
        first, second = (call.args[0] for call in edit.await_args_list)
        self.assertIn('<tg-emoji emoji-id="1234">✅</tg-emoji>', first)
        self.assertIn('✅ <b>Media delivered</b>', second)
        self.assertIn('Video &lt;1&gt;', second)


class ExtractorFlowTests(unittest.TestCase):
    def test_authorized_instagram_route_uses_its_session_first(self):
        with tempfile.TemporaryDirectory() as folder:
            cookie = Path(folder) / 'cookies.txt'
            cookie.write_text('# Netscape HTTP Cookie File\n')
            photo = Path(folder) / 'photo.jpg'
            photo.write_bytes(b'fixture')
            with patch.dict(os.environ, {'INSTAGRAM_COOKIE_FILE': str(cookie)}), \
                 patch('bot.safe_remote_url', return_value=True), \
                 patch('bot.download_gallery', return_value=[photo]) as gallery, \
                 patch('bot.download_instagram_dedicated') as public, \
                 patch('bot.validate_media_files', side_effect=lambda paths: paths):
                self.assertEqual(bot.grab('https://www.instagram.com/p/test/', 'auto', folder), [photo])
            gallery.assert_called_once()
            public.assert_not_called()

    def test_age_restriction_stops_fallback(self):
        self.assertTrue(bot.fatal_download_error(RuntimeError('age-restricted content')))
        self.assertFalse(bot.fatal_download_error(RuntimeError('rate limit exceeded')))
        self.assertFalse(bot.fatal_download_error(RuntimeError('page rate limit exceeded')))

    def test_reel_audio_mode_accepts_audio_from_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / 'track.m4a'
            audio.write_bytes(b'fixture')
            with patch('bot.safe_remote_url', return_value=True), \
                    patch('bot.download_instagram_dedicated', side_effect=RuntimeError('429 rate limit')), \
                    patch('bot.download_gallery', side_effect=RuntimeError('unavailable')), \
                    patch('bot.download_ytdlp', return_value=[audio]) as fallback, \
                    patch('bot.validate_media_files', side_effect=lambda files: files), \
                    patch('bot.classify', return_value='audio'):
                self.assertEqual(bot.grab('https://www.instagram.com/reel/test/', 'mp3_320', folder), [audio])
                fallback.assert_called_once()

    def test_partial_carousel_does_not_fall_back_to_one_item(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('bot.safe_remote_url', return_value=True), \
                patch('bot.download_instagram_dedicated', side_effect=RuntimeError('Incomplete carousel')), \
                patch('bot.download_gallery') as fallback:
            with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
                bot.grab('https://www.instagram.com/p/test/', 'auto', folder)
            fallback.assert_not_called()

    def test_error_messages_preserve_category_without_source_secrets(self):
        for detail, expected in [('Incomplete https://secret.test/token', 'incomplete'),
                                 ('429 rate limit https://secret.test/token', 'rate-limited'),
                                 ('age limit', 'age-restricted')]:
            message = bot.friendly_error('instagram', RuntimeError(detail))
            self.assertIn(expected, message)
            self.assertNotIn('secret', message)
            self.assertEqual(bot.friendly_error('instagram', bot.WorkerFailure(message)), message)


class JobFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = bot.JobManager(Path(self.tmp.name) / 'jobs.sqlite3')
        self.manager_patch = patch('bot._jobs', self.manager)
        self.manager_patch.start()

    async def asyncTearDown(self):
        await self.manager.shutdown()
        self.manager_patch.stop()
        self.tmp.cleanup()

    async def test_duplicate_link_runs_once_and_cleans_up(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def run(*args, **kwargs):
            started.set()
            await release.wait()
            bot.mark_state('done')
        msg = SimpleNamespace(chat_id=1, reply_text=AsyncMock())
        with patch('bot.run_job', side_effect=run) as job:
            first = asyncio.create_task(bot.dispatch_job(msg, None, 1, 'https://youtu.be/test', 'auto'))
            await asyncio.wait_for(started.wait(), 2)
            await bot.dispatch_job(msg, None, 1, 'https://youtu.be/test', 'auto')
            self.assertIn('already being processed', msg.reply_text.call_args.args[0])
            release.set()
            await first
            await asyncio.gather(*list(self.manager.tasks.values()))
            job.assert_awaited_once()
            self.assertEqual(self.manager.recent(1, 1)[0]['state'], 'done')

    async def test_failed_ack_does_not_lose_the_download(self):
        msg = SimpleNamespace(chat_id=1, reply_text=AsyncMock(side_effect=TimedOut()))
        with patch('bot.run_job', new_callable=AsyncMock) as run:
            await bot.dispatch_job(msg, None, 1, 'https://youtu.be/test', 'auto')
            await asyncio.gather(*list(self.manager.tasks.values()))
            run.assert_awaited_once()

    async def test_plain_text_receives_guidance(self):
        msg = SimpleNamespace(text='hello', caption=None, reply_text=AsyncMock())
        await bot.on_text(SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=1)), None)
        self.assertIn('link', msg.reply_text.call_args.args[0])

    async def test_retry_button_only_before_upload_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'clip.mp4'
            path.write_bytes(b'fixture')
            for attempted in (False, True):
                status = SimpleNamespace(photo=[], video=None, audio=None, document=None, animation=None,
                                         edit_text=AsyncMock(), delete=AsyncMock())
                msg = SimpleNamespace(chat_id=1)
                context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
                async def send(msg, files, caption, tmp, progress, **kwargs):
                    progress['attempted'] = True
                    raise TimedOut()
                with patch('bot.download_in_worker', new_callable=AsyncMock,
                           side_effect=None if attempted else RuntimeError('unavailable'), return_value=[path]), \
                        patch('bot.send_album', side_effect=send), patch('bot.classify', return_value='video'), \
                        patch('bot.create_action', return_value='retry-token'), \
                        patch('bot._job_locks', {}), patch('bot._global_sem', None):
                    await bot.run_job(msg, context, 1, 'https://youtu.be/test', 'auto', status)
                markup = status.edit_text.call_args.kwargs['reply_markup']
                self.assertEqual(markup is None, attempted)

    async def test_known_platform_blocks_do_not_offer_futile_retry(self):
        status = SimpleNamespace(photo=None, video=None, audio=None, document=None, animation=None,
                                 edit_text=AsyncMock(), delete=AsyncMock())
        msg = SimpleNamespace(chat_id=1)
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        with patch('bot.download_in_worker', new_callable=AsyncMock,
                   side_effect=bot.WorkerFailure('YouTube: automated traffic check', 'PlatformBotCheck')), \
                patch('bot.create_action') as action, \
                patch('bot._job_locks', {}), patch('bot._global_sem', None):
            await bot.run_job(msg, context, 1, 'https://youtu.be/test', 'auto', status)
        self.assertIsNone(status.edit_text.call_args.kwargs['reply_markup'])
        action.assert_not_called()

    async def test_rejected_album_can_be_retried_without_duplicate_warning(self):
        status = SimpleNamespace(photo=None, video=None, audio=None, document=None, animation=None,
                                 edit_text=AsyncMock(), delete=AsyncMock())
        msg = SimpleNamespace(chat_id=1)
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / 'clip.mp4'
            clip.write_bytes(b'fixture')
            async def reject(msg, files, caption, tmp, progress, **kwargs):
                progress['attempted'] = True
                raise BadRequest("can't parse media JSON object")
            with patch('bot.download_in_worker', new_callable=AsyncMock, return_value=[clip]), \
                    patch('bot.send_album', side_effect=reject), patch('bot.create_action', return_value='retry-token'), \
                    patch('bot._job_locks', {}), patch('bot._global_sem', None):
                await bot.run_job(msg, context, 1, 'https://www.instagram.com/p/test/', 'auto', status)
        self.assertIsNotNone(status.edit_text.call_args.kwargs['reply_markup'])
        self.assertIn('Telegram rejected', status.edit_text.call_args.args[0])

    async def test_mp3_source_is_copied_without_reencoding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.mp3'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440',
                            '-t', '1', '-c:a', 'libmp3lame', str(source)], check=True, timeout=20)
            def audio_hash(path):
                return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0',
                                                '-c', 'copy', '-f', 'hash', '-'], timeout=20)
            async def receive(msg, files, caption, tmp):
                self.assertEqual(audio_hash(source), audio_hash(files[0]))
                return 1
            with patch('bot.send_album', side_effect=receive):
                self.assertEqual(await bot.send_mp3_album(None, [source], root), 1)

    async def test_partial_mp3_cache_is_refreshed_without_losing_existing_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cached, first, second = [root / name for name in ('cached.m4a', 'first.m4a', 'second.m4a')]
            for path in (cached, first, second):
                path.write_bytes(path.name.encode())
            for available in (1, 2):
                with self.subTest(available=available):
                    status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
                    msg = SimpleNamespace(chat_id=1)
                    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

                    async def send(msg, sources, tmp, progress, **kwargs):
                        expected = [first, second] if available == 2 else [cached]
                        self.assertEqual([p.read_bytes() for p in sources], [p.read_bytes() for p in expected])
                        return len(sources)

                    with patch('bot.download_in_worker', new_callable=AsyncMock, return_value=[first, second][:available]) as worker, \
                         patch('bot.send_mp3_album', side_effect=send) as upload, \
                         patch('bot.has_audio', return_value=True):
                        await bot.run_job(msg, context, 1, 'https://instagram.com/p/test/', 'mp3_320', status,
                                          cached_sources=[cached], refresh_audio=True)
                    worker.assert_awaited_once()
                    upload.assert_awaited_once()
                    self.assertIn('MP3 delivered', status.edit_text.call_args.args[0])
                    status.delete.assert_not_called()


class VideoTrackTests(unittest.TestCase):
    def test_h264_is_preserved_when_only_audio_needs_conversion(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'h264_opus.mkv'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=25',
                            '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '1', '-c:v', 'libx264',
                            '-c:a', 'libopus', str(source)], check=True, timeout=30)
            result = media_io.native_video(source, root / 'output')
            def video_hash(path):
                return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:v:0',
                                                '-c', 'copy', '-bsf:v', 'h264_mp4toannexb', '-f', 'hash', '-'], timeout=20)
            self.assertEqual(video_hash(source), video_hash(result))
            self.assertTrue(media_io.streamable(result))
            self.assertEqual(next(s['codec_name'] for s in media_io.probe(result)['streams']
                                  if s['codec_type'] == 'audio'), 'aac')
