"""A video or audio sent to the bot (e.g. a screen recording) becomes MP3."""
import asyncio
import datetime
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Chat, Document, Message, Update, User, Video
from telegram.ext import MessageHandler

import bot


def sample(path: Path, *, audio: bool = True) -> Path:
    inputs = ['-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10']
    if audio:
        inputs += ['-f', 'lavfi', '-i', 'sine=frequency=440']
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *inputs, '-t', '2', '-c:v', 'libx264',
                    *(['-c:a', 'aac'] if audio else []), '-shortest', str(path)], check=True, timeout=30)
    return path


class FakeStatus:
    def __init__(self):
        self.texts = []

    async def edit_text(self, text, **kwargs):
        self.texts.append(text)


class FakeMessage:
    def __init__(self, *, video=None, document=None, caption=None):
        self.video, self.document, self.caption = video, document, caption
        self.audio = self.voice = self.video_note = None
        self.chat_id = 7
        self.status = FakeStatus()
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        self.status.texts.append(text)
        return self.status


class MediaFileTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)

    def run_handler(self, msg, source: Path | None):
        downloaded = []

        class TelegramFile:
            async def download_to_drive(self, custom_path):
                downloaded.append(Path(custom_path))
                shutil.copy(source, custom_path)

        context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=TelegramFile())))
        update = SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=42))
        sent = []

        async def send_album(message, files, caption, tmp, progress=None):
            for path in files:
                probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                        'stream=codec_name:format_tags=title', '-of', 'csv=p=0', str(path)],
                                       capture_output=True, text=True, check=True)
                sent.append((path.name, probe.stdout.split()))
            return len(files)

        async def handle():
            task = await bot.on_media_file(update, context)
            if task is not None:
                await task

        with patch('bot.send_album', side_effect=send_album):
            asyncio.run(handle())
        return context, downloaded, sent

    def test_a_screen_recording_becomes_an_mp3_named_after_its_caption(self):
        source = sample(self.folder / 'Screen_Recording_20261124.mp4')
        video = SimpleNamespace(file_id='f1', file_size=source.stat().st_size, file_name=source.name)
        msg = FakeMessage(video=video, caption='Snap from Akbar')
        context, downloaded, sent = self.run_handler(msg, source)
        context.bot.get_file.assert_awaited_once_with('f1')
        self.assertEqual(downloaded[0].suffix, '.mp4')
        self.assertFalse(downloaded[0].exists())  # temporary files are removed
        self.assertEqual(len(sent), 1)
        name, details = sent[0]
        self.assertEqual(name, 'Snap from Akbar.mp3')
        self.assertIn('mp3', details)
        self.assertIn('File to MP3', msg.status.texts[0])
        self.assertIn('elapsed', msg.status.texts[0])
        self.assertIn('MP3 delivered', msg.status.texts[-1])
        self.assertEqual(len(msg.replies), 1)

    def test_title_comes_from_the_file_name_without_a_caption(self):
        self.assertEqual(bot.media_file_title(FakeMessage(), 'Screen_Recording_2026.mp4'), 'Screen Recording 2026')
        self.assertEqual(bot.media_file_title(FakeMessage(), ''), 'Audio')

    def test_a_silent_file_is_explained(self):
        source = sample(self.folder / 'silent.mp4', audio=False)
        video = SimpleNamespace(file_id='f2', file_size=source.stat().st_size, file_name=source.name)
        msg = FakeMessage(video=video)
        _, _, sent = self.run_handler(msg, source)
        self.assertEqual(sent, [])
        self.assertIn('no sound', msg.status.texts[-1])

    def test_a_file_over_the_bot_limit_is_refused_before_download(self):
        video = SimpleNamespace(file_id='f3', file_size=bot.BOT_FILE_LIMIT + 1, file_name='long.mp4')
        msg = FakeMessage(video=video)
        with patch('bot.TELEGRAM_API_BASE', ''):
            context, downloaded, _ = self.run_handler(msg, None)
        context.bot.get_file.assert_not_awaited()
        self.assertEqual(downloaded, [])
        self.assertIn('20 MB', msg.replies[0])

    def test_conversion_runs_in_the_background_with_per_user_limits(self):
        release = asyncio.Event()
        started = []

        async def convert(msg, context, uid, item, name):
            started.append(name)
            await release.wait()

        async def scenario():
            update = SimpleNamespace(effective_user=SimpleNamespace(id=9))
            messages, tasks = [], []
            for index in range(bot.FILE_JOBS_PER_USER + 1):
                msg = FakeMessage(video=SimpleNamespace(file_id=str(index), file_size=10, file_name=f'{index}.mp4'))
                update.effective_message = msg
                messages.append(msg)
                # The handler returns at once instead of holding an update slot.
                tasks.append(await asyncio.wait_for(bot.on_media_file(update, SimpleNamespace()), 1))
            await asyncio.sleep(0)
            self.assertEqual(len(started), bot.FILE_JOBS_PER_USER)
            self.assertIsNone(tasks[-1])
            self.assertIn('Please wait', messages[-1].replies[0])
            release.set()
            await asyncio.gather(*filter(None, tasks))
            self.assertEqual(bot._file_jobs, {})
            self.assertEqual(bot._file_tasks, set())

        with patch('bot.convert_media_file', side_effect=convert):
            asyncio.run(scenario())

    def test_a_lost_upload_reply_is_not_reported_as_a_missing_file(self):
        source = sample(self.folder / 'clip.mp4')
        video = SimpleNamespace(file_id='f4', file_size=source.stat().st_size, file_name=source.name)

        async def lost(msg, sources, tmp, progress, *, meta):
            progress['attempted'] = True
            raise bot.TimedOut('reply lost')

        msg = FakeMessage(video=video)
        with patch('bot.send_mp3_album', side_effect=lost):
            self.run_handler(msg, source)
        self.assertIn('Check this chat before sending the file again', msg.status.texts[-1])

        class Broken:
            async def download_to_drive(self, custom_path):
                raise bot.NetworkError('download failed')

        msg = FakeMessage(video=video)
        context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=Broken())))
        update = SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=43))

        async def handle():
            await (await bot.on_media_file(update, context))

        asyncio.run(handle())
        self.assertIn('did not deliver this file to the bot', msg.status.texts[-1])

    def test_the_configured_length_limit_applies_to_files(self):
        source = sample(self.folder / 'long.mp4')
        video = SimpleNamespace(file_id='f5', file_size=source.stat().st_size, file_name=source.name)
        msg = FakeMessage(video=video)
        with patch('bot.MAX_MEDIA_SECONDS', 1):
            _, _, sent = self.run_handler(msg, source)
        self.assertEqual(sent, [])
        self.assertIn('longer than', msg.status.texts[-1])

    def test_shutdown_cancels_conversions_and_removes_their_files(self):
        folders = []
        entered = asyncio.Event()

        async def slow_download(custom_path):
            folders.append(Path(custom_path).parent)
            entered.set()
            await asyncio.sleep(3600)

        async def scenario():
            msg = FakeMessage(video=SimpleNamespace(file_id='f6', file_size=10, file_name='a.mp4'))
            context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(
                return_value=SimpleNamespace(download_to_drive=slow_download))))
            update = SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=44))
            task = await bot.on_media_file(update, context)
            await asyncio.wait_for(entered.wait(), 5)
            # stop_all() would also block every later subprocess in this test run.
            with patch('media_process.stop_all'):
                await bot.stop_jobs(SimpleNamespace())
            self.assertTrue(task.cancelled())
            self.assertEqual(bot._file_tasks, set())

        asyncio.run(scenario())
        self.assertFalse(folders[0].exists())

    def test_only_video_and_audio_documents_count(self):
        pdf = SimpleNamespace(mime_type='application/pdf', file_name='a.pdf')
        movie = SimpleNamespace(mime_type='video/mp4', file_name='a.mp4')
        self.assertIsNone(bot.incoming_media(FakeMessage(document=pdf)))
        self.assertEqual(bot.incoming_media(FakeMessage(document=movie)), (movie, 'a.mp4'))

    def test_handler_takes_private_media_but_leaves_captioned_links_to_downloads(self):
        handlers = []
        app = SimpleNamespace(add_handler=handlers.append, add_error_handler=lambda _: None)
        bot.register_handlers(app)
        media = next(h for h in handlers if isinstance(h, MessageHandler) and h.callback is bot.on_media_file)
        when = datetime.datetime.now(datetime.timezone.utc)
        user = User(1, 'u', False)
        video = Video('id', 'uid', 64, 64, 2)

        def update(chat_type, **fields):
            message = Message(1, when, Chat(1, chat_type), from_user=user, **fields)
            return Update(1, message=message)

        self.assertTrue(media.check_update(update('private', video=video)))
        self.assertTrue(media.check_update(update('private', document=Document('d', 'du', mime_type='audio/mpeg'))))
        self.assertFalse(media.check_update(update('group', video=video)))
        self.assertFalse(media.check_update(update('private', video=video,
                                                   caption='https://www.snapchat.com/spotlight/abc')))


if __name__ == '__main__':
    unittest.main()
