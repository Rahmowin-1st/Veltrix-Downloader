import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot
import media_io
from telegram.error import RetryAfter, TimedOut


class MediaFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.video = cls.root / "sample.mp4"
        subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "6",
            "-c:v", "libx264", "-g", "25", "-c:a", "aac", str(cls.video),
        ], check=True, timeout=30)
        cls.audio = cls.root / "audio.m4a"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(cls.video), "-vn", "-c:a", "copy", str(cls.audio)], check=True)
        cls.image = cls.root / "photo.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(cls.video), "-frames:v", "1", str(cls.image)], check=True)
        cls.gif = cls.root / "animation.gif"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(cls.video), "-t", "1", "-vf", "fps=5,scale=100:-1", str(cls.gif)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_bytes_override_misleading_extension(self):
        for source, expected in [(self.video, "video"), (self.audio, "audio"), (self.image, "image"), (self.gif, "animation")]:
            target = self.root / (expected + ".wrong")
            shutil.copyfile(source, target)
            self.assertEqual(bot.classify(target), expected)
        fake_jpg = self.root / "actually_video.jpg"
        shutil.copyfile(self.video, fake_jpg)
        self.assertEqual(bot.classify(fake_jpg), "video")

    def test_error_payload_rejected(self):
        bad = self.root / "error.mp4"
        bad.write_text('{"error":"blocked"}')
        with self.assertRaises(RuntimeError):
            bot.validate_media_files([bad])

    def test_lossless_split_preserves_resolution_audio_duration(self):
        limit = int(self.video.stat().st_size * .45)
        parts = media_io.lossless_parts(self.video, self.root / "split", limit)
        self.assertGreater(len(parts), 1)
        total = 0
        for part in parts:
            self.assertLessEqual(part.stat().st_size, limit)
            info = media_io.probe(part)
            video = next(s for s in info["streams"] if s["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"], video["codec_name"]), (320, 240, "h264"))
            self.assertTrue(any(s["codec_type"] == "audio" for s in info["streams"]))
            total += float(info["format"]["duration"])
        self.assertAlmostEqual(total, 6, delta=.3)

    def test_instagram_selects_largest_and_preserves_carousel_order(self):
        requested = []
        info = {"type": "carousel", "entries": [
            {"kind": "video", "formats": [{"url": "https://cdn.test/low.mp4", "width": 1280, "height": 720},
                                             {"url": "https://cdn.test/high.mp4", "width": 1920, "height": 1080}]},
            {"kind": "image", "formats": [{"url": "https://cdn.test/photo.png", "width": 300, "height": 200}]},
        ]}
        def download(url, out, **kwargs):
            requested.append(url)
            shutil.copyfile(self.image if "photo" in url else self.video, out)
            return True
        with patch("bot.safe_remote_url", return_value=True), patch("bot._download_direct_file", side_effect=download):
            files, kind = bot.download_instagram_dedicated("https://www.instagram.com/p/example/", str(self.root), info)
        self.assertEqual(requested, ["https://cdn.test/high.mp4", "https://cdn.test/photo.png"])
        self.assertEqual([bot.classify(p) for p in files], ["video", "image"])

    def test_partial_carousel_is_not_success(self):
        info = {"entries": [{"kind": "image", "formats": [{"url": "https://cdn.test/x.jpg"}]}]}
        with patch("bot.safe_remote_url", return_value=True), patch("bot._download_direct_file", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Incomplete carousel"):
                bot.download_instagram_dedicated("https://www.instagram.com/p/example/", str(self.root), info)

    def test_pinterest_mixed_manifest_keeps_page_order(self):
        manifest = [[2, {"count": 3}], [3, "https://cdn.test/a.mp4", {"extension": "mp4"}],
                    [3, "https://cdn.test/b.png", {"extension": "png"}], [3, "https://cdn.test/c.m4a", {"extension": "m4a"}]]
        sources = iter([self.video, self.image, self.audio])
        def download(url, out, **kwargs):
            shutil.copyfile(next(sources), out)
            return True
        # Probe before mocking subprocess.run (ffprobe also uses subprocess).
        with patch("bot.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(manifest))), \
                patch("bot.safe_remote_url", return_value=True), patch("bot._download_direct_file", side_effect=download), \
                patch("bot.validate_media_files", side_effect=lambda files: files):
            files = bot.download_gallery("https://www.pinterest.com/pin/123/", str(self.root))
        self.assertEqual([bot.classify(p) for p in files], ["video", "image", "audio"])

    def test_gallery_error_record_even_with_exit_zero(self):
        response = SimpleNamespace(returncode=0, stdout=json.dumps([[3, "https://cdn.test/a.jpg", {}], [-1, {"error": "HTTPError"}]]))
        with patch("bot.subprocess.run", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "Incomplete"):
                bot.download_gallery("https://www.pinterest.com/pin/123/", str(self.root))

    def test_duplicate_source_items_are_preserved(self):
        self.assertEqual(bot.validate_media_files([self.video, self.video]), [self.video, self.video])

    def test_youtube_auto_download_uses_best_selector(self):
        seen = []
        fixture = self.video
        class FakeDownloader:
            def __init__(self, opts):
                seen.append(opts)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def extract_info(self, url, download):
                return {"filepath": str(fixture), "requested_downloads": [{"filepath": str(fixture)}]}
        with patch("bot.YoutubeDL", FakeDownloader):
            self.assertEqual(bot.download_ytdlp("https://www.youtube.com/watch?v=BaW_jenozKc", "auto", str(self.root)), [self.video])
        self.assertEqual(seen[0]["format"], "bv*+ba/b/ba")
        self.assertEqual(len(seen), 1)

    def test_youtube_never_delivers_video_after_declared_audio_is_lost(self):
        silent = self.root / 'silent.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(self.video),
                        '-an', '-c:v', 'copy', str(silent)], check=True, timeout=20)
        class FakeDownloader:
            def __init__(self, opts):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def extract_info(self, url, download):
                return {'requested_formats': [
                    {'vcodec': 'h264', 'acodec': 'none'},
                    {'vcodec': 'none', 'acodec': 'aac'}],
                    'requested_downloads': [{'filepath': str(silent)}]}
        with patch('bot.YoutubeDL', FakeDownloader):
            with self.assertRaisesRegex(RuntimeError, 'Incomplete source video audio'):
                bot.download_ytdlp('https://www.youtube.com/watch?v=BaW_jenozKc',
                                   'auto', str(self.root))

    def test_remux_preserves_h264_audio(self):
        source = self.root / "remux-source.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(self.video), "-c", "copy", str(source)], check=True)
        final = media_io.prepare_video(source, self.root / "remux-test")
        self.assertTrue(media_io.streamable(final))
        self.assertAlmostEqual(float(media_io.probe(final)["format"]["duration"]), 6, delta=.3)

    def test_video_can_never_be_fitted_as_photo(self):
        with self.assertRaisesRegex(RuntimeError, "Refusing"):
            bot.fit_image(self.video, self.root / "not-a-photo")

    def test_panorama_is_fitted_to_native_photo_limits(self):
        source = self.root / "panorama.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=4000x80",
                        "-frames:v", "1", str(source)], check=True)
        self.assertFalse(bot.telegram_photo_ready(source))
        final = bot.fit_image(source, self.root / "fit-panorama")
        self.assertTrue(bot.telegram_photo_ready(final))
        self.assertEqual(bot.classify(final), "image")

    def test_non_native_video_keeps_resolution_and_sound(self):
        source = self.root / "mpeg4.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(self.video), "-c:v", "mpeg4",
                        "-c:a", "copy", str(source)], check=True)
        final = media_io.native_video(source, self.root / "native-video")
        self.assertTrue(media_io.streamable(final))
        info = media_io.probe(final)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (320, 240))
        self.assertTrue(bot.has_audio(final))
        self.assertAlmostEqual(float(info["format"]["duration"]), 6, delta=.3)

    def test_animation_remains_moving_in_album(self):
        final = media_io.native_video(self.gif, self.root / "native-animation")
        self.assertTrue(media_io.streamable(final))
        info = media_io.probe(final)
        self.assertGreater(float(info["format"]["duration"]), .5)
        self.assertGreater(int(info["streams"][0]["nb_frames"]), 1)

    def test_mp3_album_extracts_every_audio_source(self):
        captured = []
        async def receive(msg, outputs, caption, tmp):
            for output in outputs:
                info = media_io.probe(output)
                self.assertTrue(all(s["codec_type"] == "audio" for s in info["streams"]))
                self.assertEqual(info["streams"][0]["codec_name"], "mp3")
                self.assertAlmostEqual(float(info["format"]["duration"]), 6, delta=.3)
            captured.extend(outputs)
            return len(outputs)
        with patch("bot.send_album", side_effect=receive):
            count = asyncio.run(bot.send_mp3_album(None, [self.video, self.image, self.audio], self.root / "mp3-test"))
        self.assertEqual(count, 2)
        self.assertNotEqual(captured[0], captured[1])


class ExtractorRegressionTests(unittest.TestCase):
    def test_youtube_id_on_wrong_host_not_rewritten(self):
        self.assertEqual(bot.extract_url("https://example.com/?v=BaW_jenozKc"), "https://example.com/?v=BaW_jenozKc")

    def test_snapchat_never_uses_unmatched_recommendation(self):
        doc = {"query": {"snapID": "wrong"}, "props": {"pageProps": {"spotlightFeed": {"spotlightStories": [
            {"story": {"storyId": {"value": "wrong"}}, "metadata": {"videoMetadata": {"contentUrl": "https://cdn.test/wrong"}}}
        ]}}}}
        self.assertEqual(bot._snap_info_from_doc(doc, "https://www.snapchat.com/spotlight/wanted"), {})

    def test_snapchat_story_exact_item_and_media_types(self):
        snaps = [{"snapId": {"value": "a"}, "snapMediaType": 0, "snapUrls": {"mediaUrl": "https://cdn.test/a"}},
                 {"snapId": {"value": "b"}, "snapMediaType": 1, "snapUrls": {"mediaUrl": "https://cdn.test/b"}}]
        doc = {"props": {"pageProps": {"story": {"snapList": snaps}}}}
        self.assertEqual(bot.snapchat_story_entries(doc, "https://www.snapchat.com/add/user/b"), [{"url": "https://cdn.test/b", "kind": "video"}])
        self.assertEqual(bot.snapchat_story_entries(doc, "https://www.snapchat.com/add/user/missing"), [])
        self.assertEqual([x["kind"] for x in bot.snapchat_story_entries(doc, "https://www.snapchat.com/add/user")], ["image", "video"])

    def test_429_wait_is_never_shortened(self):
        self.assertGreaterEqual(bot.telegram_retry_delay(RetryAfter(90), 0), 90)

    def test_ytdlp_settings_no_resolution_cap_and_no_missing_fragments(self):
        with tempfile.TemporaryDirectory() as tmp:
            opts = bot.base_ydl_opts(tmp)
            self.assertFalse(opts["skip_unavailable_fragments"])
            self.assertEqual(opts["age_limit"], 17)

    def test_snapchat_preload_is_exact_single_video(self):
        page = '<link href="https://cf-st.sc-cdn.net/d/one" as="video" rel="preload">'
        self.assertEqual(bot.snapchat_preload(page)["url"], "https://cf-st.sc-cdn.net/d/one")
        self.assertEqual(bot.snapchat_preload(page + page.replace('/one', '/two')), {})
        self.assertEqual(bot.snapchat_preload(page.replace('cf-st.sc-cdn.net', 'attacker.test')), {})

    def test_public_page_redirect_rejects_private_destination(self):
        import httpx
        transport = httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/private"}))
        client = httpx.Client(transport=transport, trust_env=False)
        with patch("bot.httpx.Client", return_value=client), patch("bot.safe_remote_url", side_effect=lambda u: '127.0.0.1' not in u):
            with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                bot.fetch_public_page("https://www.snapchat.com/spotlight/example")


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_does_not_blindly_resend(self):
        send = AsyncMock(side_effect=TimedOut())
        with self.assertRaises(TimedOut):
            await bot._retry_telegram(send)
        self.assertEqual(send.await_count, 1)

    async def test_flood_control_retries_after_requested_wait(self):
        send = AsyncMock(side_effect=[RetryAfter(2), "sent"])
        with patch("bot.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await bot._retry_telegram(send), "sent")
            sleep.assert_awaited_once_with(2.5)

    async def test_image_sent_as_photo_not_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "original.jpg"
            path.write_bytes(b"original-image-bytes")
            seen = []
            async def receive(**kwargs):
                seen.append(kwargs["photo"].input_file_content.read())
            msg = SimpleNamespace(reply_photo=AsyncMock(side_effect=receive), reply_document=AsyncMock())
            with patch("bot.fit_image", return_value=path):
                self.assertEqual(await bot.send_image(msg, path, "", Path(tmp)), 1)
            msg.reply_document.assert_not_called()
            self.assertEqual(seen, [b"original-image-bytes"])

    async def test_worker_timeout_reaps_process(self):
        proc = SimpleNamespace(pid=987654321, returncode=None, wait=AsyncMock(side_effect=[TimeoutError(), 0]))
        with tempfile.TemporaryDirectory() as tmp, \
                patch("bot.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc), \
                patch("bot.os.killpg") as kill:
            with self.assertRaisesRegex(RuntimeError, "deadline"):
                await bot.download_in_worker("https://www.youtube.com/watch?v=BaW_jenozKc", "auto", Path(tmp), {})
            kill.assert_called_once()
            self.assertEqual(proc.wait.await_count, 2)
