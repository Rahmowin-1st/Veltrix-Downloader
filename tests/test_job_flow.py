import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import media_io
from telegram.error import TimedOut


class ExtractorFlowTests(unittest.TestCase):
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
                async def send(msg, files, caption, tmp, progress):
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
