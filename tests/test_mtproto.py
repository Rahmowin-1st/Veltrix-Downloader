import asyncio
import base64
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import telegram_mtproto
import vidfetch_provider
import newisty_provider


def zero_decode(data: bytes) -> bytes:
    out, index = bytearray(), 0
    while index < len(data):
        if data[index] == 0:
            out += b'\x00' * data[index + 1]
            index += 2
        else:
            out.append(data[index])
            index += 1
    return bytes(out)


def decode_file_id(value: str) -> dict:
    """Independent reader for the TDLib persistent file id layout."""
    raw = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    assert raw[-1] == 4, 'persistent id version'
    minor, data = raw[-2], zero_decode(raw[:-2])
    file_type, dc_id = struct.unpack_from('<ii', data)
    offset, reference = 8, b''
    if file_type & (1 << 25):
        length = data[offset]
        if length == 254:
            length = int.from_bytes(data[offset + 1:offset + 4], 'little')
            start = offset + 4
        else:
            start = offset + 1
        reference = data[start:start + length]
        offset = start + length + (-(start + length - offset) % 4)
    media_id, access_hash = struct.unpack_from('<qq', data, offset)
    return {'type': file_type & 0xFFFFFF, 'dc': dc_id, 'id': media_id, 'hash': access_hash,
            'reference': reference, 'minor': minor}


class FileIdTests(unittest.TestCase):
    def test_matches_the_bot_api_layout(self):
        # Vector cross-checked with Pyrogram's independent Bot API file_id codec.
        reference = bytes.fromhex('0200000a2b65f4d8c1a2f0e1d2c3b4a5968778695a4b3c2d1e')
        value = telegram_mtproto.bot_file_id('video', 2, 5418221711389917443, -6239893742395342126, reference)
        self.assertEqual(value, 'BAACAgIAAxkCAAIKK2X02MGi8OHSw7Sllod4aVpLPC0eAAIDgbSF8GMxS9LCV6CzcWepHgQ')

    def test_round_trip_for_every_kind_and_long_references(self):
        for kind, expected in telegram_mtproto.FILE_TYPES.items():
            for reference in (b'\x01\x00\x00\x02', bytes(range(1, 255)) + b'\x00' * 300):
                with self.subTest(kind=kind, size=len(reference)):
                    value = telegram_mtproto.bot_file_id(kind, 4, -42, 987654321, reference)
                    self.assertNotIn('=', value)
                    self.assertEqual(decode_file_id(value), {'type': expected, 'dc': 4, 'id': -42,
                                                             'hash': 987654321, 'reference': reference,
                                                             'minor': telegram_mtproto.PERSISTENT_ID_MINOR})

    def test_configuration_needs_numeric_id_and_hash(self):
        for env, expected in (({'TELEGRAM_API_ID': '12345', 'TELEGRAM_API_HASH': 'a' * 32}, True),
                              ({'TELEGRAM_API_ID': 'abc', 'TELEGRAM_API_HASH': 'a' * 32}, False),
                              ({'TELEGRAM_API_ID': '12345', 'TELEGRAM_API_HASH': 'short'}, False),
                              ({'TELEGRAM_API_ID': '', 'TELEGRAM_API_HASH': ''}, False)):
            with self.subTest(env=env), patch.dict('os.environ', env):
                self.assertEqual(telegram_mtproto.configured(), expected)

    def test_failure_pauses_large_uploads(self):
        with patch.dict('os.environ', {'TELEGRAM_API_ID': '12345', 'TELEGRAM_API_HASH': 'b' * 32}), \
                patch.object(telegram_mtproto, '_disabled_until', 0.0):
            self.assertTrue(telegram_mtproto.available())
            telegram_mtproto.mark_failed(SimpleNamespace(seconds=120, __cause__=None))
            self.assertFalse(telegram_mtproto.available())


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_stores_document_without_a_chat_and_returns_file_id(self):
        from telethon.tl import functions, types
        requests, uploads = [], []

        class Client:
            async def upload_file(self, file, **kwargs):
                uploads.append((file.read(), kwargs.get('file_size'), kwargs.get('file_name')))
                return types.InputFileBig(id=1, parts=1, name=kwargs.get('file_name') or 'thumb.jpg')

            async def __call__(self, request):
                requests.append(request)
                return types.MessageMediaDocument(document=types.Document(
                    id=77, access_hash=-5, file_reference=b'\x02\x03', date=None, mime_type='video/mp4',
                    size=4, dc_id=2, attributes=[]))

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'clip.mp4'
            path.write_bytes(b'0123456789')
            with patch('telegram_mtproto._get_client', return_value=Client()):
                value = await telegram_mtproto.upload(path, kind='video', bot_token='1:x', filename='clip.mp4',
                                                      byte_range=(2, 4), duration=12.5, width=1280, height=720)
        self.assertEqual(uploads, [(b'2345', 4, 'clip.mp4')])
        request = requests[0]
        self.assertIsInstance(request, functions.messages.UploadMediaRequest)
        self.assertIsInstance(request.peer, types.InputPeerEmpty)
        video = request.media.attributes[0]
        self.assertEqual((video.w, video.h, video.duration, video.supports_streaming), (1280, 720, 12.5, True))
        self.assertEqual(decode_file_id(value)['id'], 77)
        self.assertEqual(decode_file_id(value)['reference'], b'\x02\x03')


class ProviderQualityTests(unittest.TestCase):
    def test_vidfetch_sizes_in_any_listing_shape(self):
        info = {'qualities': [{'format': '1080p', 'size_bytes': 900}, {'quality': '720p', 'size': 400}],
                'sizes': {'480p': 200, '360p': {'filesize': 100}}}
        self.assertEqual(vidfetch_provider.quality_sizes(info), {'1080p': 900, '720p': 400, '480p': 200, '360p': 100})

    def test_vidfetch_quality_follows_the_budget(self):
        sizes = {'1080p': 900, '720p': 400}
        self.assertEqual(vidfetch_provider.choose_quality('1080p', 60, 1000, sizes), '1080p')
        self.assertEqual(vidfetch_provider.choose_quality('1080p', 60, 500, sizes), '720p')
        # Three hours at 49 MB per part: 360p is the floor, delivered in more parts.
        self.assertEqual(vidfetch_provider.choose_quality('1080p', 3 * 3600, 980_000_000, {}), '360p')
        self.assertEqual(vidfetch_provider.choose_quality('1080p', 3600, 980_000_000, {}), '480p')
        self.assertEqual(vidfetch_provider.choose_quality('mp3', 3 * 3600, 1, {}), 'mp3')
        self.assertEqual(vidfetch_provider.choose_quality('2160p', 60, None, {}), '2160p')

    def test_vidfetch_requests_the_budgeted_tier_and_waits_longer_for_long_videos(self):
        created = []
        now = [0]

        def handle(request):
            if request.url.path == '/v1/info':
                return httpx.Response(200, json={'id': 'aqz-KE-bpKQ', 'duration': 3 * 3600})
            if request.method == 'POST':
                created.append(request.read())
                return httpx.Response(202, json={'id': 'dl_x', 'url': 'https://www.youtube.com/watch?v=aqz-KE-bpKQ',
                                                 'status': 'processing'})
            if request.method == 'DELETE':
                return httpx.Response(204)
            return httpx.Response(200, json={'id': 'dl_x', 'url': 'https://www.youtube.com/watch?v=aqz-KE-bpKQ',
                                             'status': 'processing', 'progress': 40})

        statuses = []

        def sleep(seconds):
            now[0] += seconds

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(vidfetch_provider.ProviderError, 'timed out'):
                vidfetch_provider.fetch(
                    'aqz-KE-bpKQ', '1080p', Path(folder), api_key='k', safe_url=lambda _: True,
                    budget=lambda _: None, max_bytes=10 ** 10, budget_bytes=980_000_000,
                    client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), **kw),
                    clock=lambda: now[0], sleep=sleep, status=lambda state, value: statuses.append((state, value)))
        self.assertIn(b'"format":"360p"', created[0].replace(b' ', b''))
        # A three-hour job may take far longer than the four-minute default.
        self.assertGreater(now[0], 1800)
        self.assertEqual(statuses[0], ('processing', None))
        self.assertEqual(statuses[-1], ('processing', 40))

    def test_newisty_quality_follows_the_budget(self):
        self.assertEqual(newisty_provider.choose_quality('best-mp4', 600, 980_000_000), 'best-mp4')
        self.assertEqual(newisty_provider.choose_quality('best-mp4', 3 * 3600, 980_000_000), '480p')
        self.assertEqual(newisty_provider.choose_quality('audio-mp3', 3 * 3600, 1), 'audio-mp3')


if __name__ == '__main__':
    unittest.main()
