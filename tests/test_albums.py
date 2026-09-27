import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from source_metadata import instagram_audio_urls, instagram_extractor, pinterest_entries, find_pinterest_pin
from telegram.error import TimedOut


class MetadataTests(unittest.TestCase):
    def test_pinterest_page_never_selects_related_pin(self):
        document = {"pins": [{"id": "wrong", "videos": {"video_list": {"url": "unrelated"}}},
                             {"id": "42", "images": {"orig": {"url": "chosen"}}}]}
        self.assertEqual(find_pinterest_pin(document, "42")["id"], "42")
        self.assertEqual(find_pinterest_pin(document, "missing"), {})

    def test_pinterest_carousel_video_wins_over_cover(self):
        result = pinterest_entries({"carousel_data": {"carousel_slots": [
            {"images": {"orig": {"url": "https://cdn.test/one.jpg"}}},
            {"images": {"orig": {"url": "https://cdn.test/cover.jpg"}},
             "videos": {"video_list": {"large": {"url": "https://cdn.test/two.mp4", "width": 1920, "height": 1080}}}},
        ]}})
        self.assertEqual([e["kind"] for e in result], ["image", "video"])
        self.assertTrue(result[1]["formats"][0]["url"].endswith("two.mp4"))

    def test_pinterest_missing_video_is_not_a_photo(self):
        with self.assertRaisesRegex(RuntimeError, "poster rejected"):
            pinterest_entries({"is_video": True, "images": {"orig": {"url": "https://cdn.test/cover.jpg"}}})

    def test_pinterest_story_keeps_music_and_order(self):
        entries = pinterest_entries({"story_pin_data": {"pages": [{"blocks": [
            {"type": "story_pin_image_block", "image_signature": "abcdef123"},
            {"type": "story_pin_music_block", "audio": {"audio_url": "https://cdn.test/music.m4a"}},
            {"type": "story_pin_video_block", "video": {"video_list": {"a": {"url": "https://cdn.test/clip.mp4"}}}},
        ]}]}})
        self.assertEqual([e["kind"] for e in entries], ["image", "audio", "video"])

    def test_instagram_music_is_exact_post_only(self):
        item = {"music_metadata": {"music_info": {"music_asset_info": {
            "progressive_download_url": "https://cdn.test/selected.m4a"}}},
            "recommendations": {"audio_url": "https://cdn.test/unrelated.mp3"}}
        self.assertEqual(instagram_audio_urls(item), ["https://cdn.test/selected.m4a"])

    def test_real_parth_parser_preserves_music(self):
        from parth_dl import InstagramDownloader
        downloader = instagram_extractor(InstagramDownloader(quiet=True))
        info = downloader.media_extractor._parse_media_item({"code": "abc", "user": {},
            "image_versions2": {"candidates": [{"url": "https://cdn.test/image.jpg"}]},
            "music_metadata": {"music_info": {"audio_src": "https://cdn.test/music.mp3"}}})
        self.assertEqual(info["audio_urls"], ["https://cdn.test/music.mp3"])

    def test_real_parth_parser_rejects_partial_carousel(self):
        from parth_dl import InstagramDownloader
        downloader = instagram_extractor(InstagramDownloader(quiet=True))
        with self.assertRaisesRegex(RuntimeError, "Incomplete"):
            downloader.media_extractor._parse_graphql_media({"edge_sidecar_to_children": {"edges": [
                {"node": {"is_video": True}}, {"node": {"display_url": "https://cdn.test/a.jpg"}},
            ]}})


class AlbumTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.messages = []
        async def receive(**kwargs):
            media = kwargs.get("media")
            if media:
                self.messages.append([item.type for item in media])
                for item in media:
                    self.assertTrue(item.media.input_file_content.read())
            else:
                self.messages.append([next(k for k in ("photo", "video", "audio") if k in kwargs)])
        self.msg = SimpleNamespace(reply_media_group=AsyncMock(side_effect=receive),
                                   reply_photo=AsyncMock(side_effect=receive),
                                   reply_video=AsyncMock(side_effect=receive),
                                   reply_audio=AsyncMock(side_effect=receive))
        self.patches = [patch("bot.classify", side_effect=lambda p: {".jpg": "image", ".mp4": "video", ".m4a": "audio"}[p.suffix]),
                        patch("bot.fit_image", side_effect=lambda path, dest: path),
                        patch("media_io.native_video", side_effect=lambda path, dest: path)]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def files(self, suffixes):
        files = []
        for index, suffix in enumerate(suffixes):
            path = self.root / f"{index}{suffix}"
            path.write_bytes(b"test-upload")
            files.append(path)
        return files

    async def test_mixed_photo_video_is_one_album(self):
        count = await bot.send_album(self.msg, self.files([".jpg", ".mp4", ".jpg"]), "caption", self.root)
        self.assertEqual(count, 3)
        self.assertEqual(self.messages, [["photo", "video", "photo"]])
        self.msg.reply_photo.assert_not_called()

    async def test_more_than_ten_are_not_dropped(self):
        await bot.send_album(self.msg, self.files([".jpg"] * 23), "caption", self.root)
        self.assertEqual([len(m) for m in self.messages], [10, 10, 3])

    async def test_audio_boundaries_keep_order(self):
        await bot.send_album(self.msg, self.files([".jpg", ".mp4", ".m4a", ".m4a", ".jpg"]), "", self.root)
        self.assertEqual(self.messages, [["photo", "video"], ["audio", "audio"], ["photo"]])

    async def test_uncertain_album_upload_is_not_retried(self):
        self.msg.reply_media_group.side_effect = TimedOut()
        with self.assertRaises(TimedOut):
            await bot.send_album(self.msg, self.files([".jpg", ".mp4"]), "", self.root)
        self.assertEqual(self.msg.reply_media_group.await_count, 1)

    async def test_main_job_uses_album_and_one_post_button(self):
        files = self.files([".jpg", ".mp4", ".mp4"])
        status = SimpleNamespace(photo=[], video=None, audio=None, document=None, animation=None,
                                 edit_text=AsyncMock(), delete=AsyncMock())
        self.msg.chat_id = 42
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        markup = bot.InlineKeyboardMarkup([[bot.InlineKeyboardButton("MP3", callback_data="mp3|test")]])
        with patch("bot.download_in_worker", new_callable=AsyncMock, return_value=files), \
                patch("bot.has_audio", side_effect=lambda p: p.suffix == ".mp4"), \
                patch("bot.post_mp3_button", return_value=markup) as button, \
                patch("bot._global_sem", None), patch("bot._job_locks", {}):
            await bot.run_job(self.msg, context, 42, "https://www.instagram.com/p/test/", "auto", status)
        self.assertEqual(self.messages, [["photo", "video", "video"]])
        self.assertEqual(button.call_args.args[2], files[1:])
        self.assertEqual(button.call_count, 1)
        self.assertEqual(status.edit_text.call_args.kwargs["reply_markup"], markup)
        status.delete.assert_not_called()

    async def test_expired_post_cache_redownloads_all_items(self):
        query = SimpleNamespace(data="mp3|post", from_user=SimpleNamespace(id=42),
                                answer=AsyncMock(), message=SimpleNamespace(reply_text=AsyncMock()))
        with patch("bot.resolve_action_data", side_effect=[
                {"url": "https://www.instagram.com/p/test/", "post": True, "children": ["a", "b"]},
                {"cache_path": ""}, {"cache_path": ""}]), patch("bot.run_job", new_callable=AsyncMock) as job:
            await bot.on_callback(SimpleNamespace(callback_query=query), None)
        job.assert_awaited_once()
        self.assertNotIn("selected_audio_index", job.call_args.kwargs)

    async def test_post_button_caches_all_tracks_and_checks_owner(self):
        with patch("bot.DATA_DIR", self.root), patch("bot.CACHE_DIR", self.root / "cache"), patch("bot.ACTIONS_FILE", self.root / "actions.json"):
            markup = bot.post_mp3_button(42, "https://www.instagram.com/p/abc/", self.files([".mp4", ".m4a"]), "title")
            token = markup.inline_keyboard[0][0].callback_data.split("|", 1)[1]
            row = bot.resolve_action_data(42, token)
            self.assertTrue(row["post"])
            self.assertEqual(len(row["children"]), 2)
            self.assertEqual(bot.resolve_action_data(43, token), {})
            for child in row["children"]:
                self.assertTrue(Path(bot.resolve_action_data(42, child)["cache_path"]).is_file())

    async def test_both_polling_and_upload_use_ipv4_option(self):
        with patch.dict(os.environ, {"TELEGRAM_IPV4": "1"}), patch("httpx.AsyncHTTPTransport", wraps=bot.httpx.AsyncHTTPTransport) as transport:
            app = bot.application_builder("123456:dummy-token-for-tests").build()
            self.assertEqual(transport.call_count, 2)
            for call in transport.call_args_list:
                self.assertEqual(call.kwargs["local_address"], "0.0.0.0")
            self.assertIsNot(app.bot._request[0], app.bot._request[1])
            for request in app.bot._request:
                await request.shutdown()

    async def test_generic_proxy_does_not_hijack_telegram(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://broken.invalid:9999"}, clear=False), \
                patch("httpx.AsyncHTTPTransport", wraps=bot.httpx.AsyncHTTPTransport) as transport:
            os.environ.pop("TELEGRAM_PROXY", None)
            request = bot.telegram_request()
            self.assertIsNone(transport.call_args.kwargs["proxy"])
            await request.shutdown()
