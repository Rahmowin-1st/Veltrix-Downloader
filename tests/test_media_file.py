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

        with patch('bot.send_album', side_effect=send_album):
            asyncio.run(bot.on_media_file(update, context))
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
        self.assertEqual(msg.status.texts[0], '🎵 Making the MP3…')
        self.assertEqual(msg.status.texts[-1], '✅ MP3 sent.')

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
