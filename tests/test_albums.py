import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from source_metadata import (instagram_audio_urls, instagram_extractor,
                             pinterest_entries, pinterest_post_audio_urls, find_pinterest_pin)
from telegram.error import TimedOut, BadRequest


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

    def test_pinterest_carousel_post_music_is_not_artwork(self):
        pin = {'carousel_data': {'carousel_slots': [
            {'images': {'orig': {'url': 'https://cdn.test/cover.jpg'}}}]},
            'music_metadata': {'audio_url': 'https://cdn.test/music.m4a',
                               'artwork': {'url': 'https://cdn.test/cover.jpg'}},
            'recommendations': [{'audio_url': 'https://cdn.test/unrelated.mp3'}]}
        self.assertEqual(pinterest_post_audio_urls(pin), ['https://cdn.test/music.m4a'])
        self.assertEqual([entry['kind'] for entry in pinterest_entries(pin)], ['image', 'audio'])

    def test_pinterest_visual_delivery_keeps_music_for_mp3_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = [
                {'kind': 'image', 'formats': [{'url': 'https://cdn.test/photo.jpg'}]},
                {'kind': 'audio', 'formats': [{'url': 'https://cdn.test/music.m4a'}]},
            ]
            def download(url, path, **kwargs):
                path.write_bytes(b'fixture')
                return True
            with patch('bot.safe_remote_url', return_value=True), \
                 patch('bot._download_direct_file', side_effect=download), \
                 patch('bot.classify', side_effect=lambda p: 'audio' if p.suffix == '.m4a' else 'image'), \
                 patch('bot.validate_media_files', side_effect=lambda paths: paths):
                visual = bot.download_source_entries(entries, root / 'pin', 'https://pinterest.com/pin/42/')
            self.assertEqual(len(visual), 1)
            self.assertEqual(visual[0].suffix, '.jpg')
            self.assertEqual([p.suffix for p in bot.files_in(root / 'soundtrack')], ['.m4a'])

    def test_video_variants_choose_a_real_audio_track(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            silent, audible = root / 'silent.mp4', root / 'audible.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                            '-i', 'testsrc2=size=320x240:rate=25', '-t', '0.4',
                            '-c:v', 'libx264', str(silent)], check=True, timeout=20)
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                            '-i', 'testsrc2=size=320x240:rate=25', '-f', 'lavfi',
                            '-i', 'sine=frequency=440', '-t', '0.4', '-c:v', 'libx264',
                            '-c:a', 'aac', str(audible)], check=True, timeout=20)
            formats = [
                {'url': 'https://cdn.test/silent.mp4', 'width': 1080, 'height': 1920, 'has_audio': True},
                {'url': 'https://cdn.test/audible.mp4', 'width': 720, 'height': 1280, 'has_audio': True},
            ]
            def download(url, output, **kwargs):
                shutil.copy2(audible if url.endswith('audible.mp4') else silent, output)
                return True
            with patch('bot.safe_remote_url', return_value=True), \
                 patch('bot._download_direct_file', side_effect=download):
                pin_files = bot.download_source_entries([{'kind': 'video', 'formats': formats}],
                                                        root / 'pin', 'https://pinterest.com/pin/42/')
                instagram_files, _ = bot.download_instagram_dedicated(
                    'https://instagram.com/p/test/', str(root / 'instagram_job'),
                    {'entries': [{'kind': 'video', 'formats': formats}], 'type': 'video'})
            self.assertTrue(bot.has_audio(pin_files[0]))
            self.assertTrue(bot.has_audio(instagram_files[0]))

    def test_silent_instagram_extractor_does_not_hide_an_audible_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            silent, audible = root / 'silent.mp4', root / 'audible.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                            '-i', 'testsrc2=size=160x120:rate=25', '-t', '0.3',
                            '-c:v', 'libx264', str(silent)], check=True, timeout=20)
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                            '-i', 'testsrc2=size=160x120:rate=25', '-f', 'lavfi',
                            '-i', 'sine=frequency=440', '-t', '0.3', '-c:v', 'libx264',
                            '-c:a', 'aac', str(audible)], check=True, timeout=20)
            with patch('bot.safe_remote_url', return_value=True), \
                 patch('bot.download_instagram_dedicated', return_value=([silent], 'video')), \
                 patch('bot.download_gallery', return_value=[audible]) as gallery, \
                 patch('bot.download_ytdlp', side_effect=AssertionError('Should use audible fallback')):
                for mode in ('auto', 'mp3_320'):
                    self.assertEqual(bot.grab('https://instagram.com/reel/test/', mode,
                                              str(root / 'job')), [audible])
            self.assertEqual(gallery.call_count, 2)

    def test_instagram_single_post_music_is_muxed_into_silent_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            silent, music = root / 'silent.mp4', root / 'music.m4a'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                            '-i', 'testsrc2=size=320x240:rate=25', '-t', '0.8',
                            '-c:v', 'libx264', str(silent)], check=True, timeout=20)
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                            '-i', 'sine=frequency=440', '-t', '0.3', '-c:a', 'aac',
                            str(music)], check=True, timeout=20)
            def download(url, output, **kwargs):
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(music if 'music' in url else silent, output)
                return True
            job = root / 'job'
            info = {'entries': [{'kind': 'video', 'formats': [
                {'url': 'https://cdn.test/silent.mp4', 'width': 320, 'height': 240}]}],
                'audio_urls': ['https://cdn.test/music.m4a'], 'type': 'video'}
            with patch('bot.safe_remote_url', return_value=True), \
                 patch('bot._download_direct_file', side_effect=download):
                videos, _ = bot.download_instagram_dedicated('https://instagram.com/reel/test/',
                                                             str(job), info)
            self.assertEqual(len(videos), 1)
            self.assertTrue(bot.has_audio(videos[0]))
            self.assertEqual(len(bot.audio_sources(videos, job / 'soundtrack')), 1)
            self.assertEqual(bot.audio_sources(videos, job / 'soundtrack')[0].suffix, '.m4a')

    def test_pinterest_post_music_is_muxed_without_duplicate_mp3_track(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            silent, music = root / 'silent.mp4', root / 'music.m4a'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                            '-i', 'testsrc2=size=160x120:rate=25', '-t', '0.6',
                            '-c:v', 'libx264', str(silent)], check=True, timeout=20)
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
                            '-i', 'sine=frequency=440', '-t', '0.3', '-c:a', 'aac',
                            str(music)], check=True, timeout=20)
            pin = {'carousel_data': {'carousel_slots': [{'videos': {'video_list': {
                'mp4': {'url': 'https://cdn.test/silent.mp4', 'width': 160, 'height': 120}}}}]},
                'audio': {'audio_url': 'https://cdn.test/music.m4a'}}
            def download(url, output, **kwargs):
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(music if 'music' in url else silent, output)
                return True
            with patch('bot.safe_remote_url', return_value=True), \
                 patch('bot._download_direct_file', side_effect=download):
                videos = bot.download_source_entries(pinterest_entries(pin), root / 'pin',
                                                     'https://pinterest.com/pin/42/')
            self.assertEqual(len(videos), 1)
            self.assertTrue(bot.has_audio(videos[0]))
            sources = bot.audio_sources(videos, root / 'soundtrack')
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].suffix, '.m4a')

    def test_instagram_music_is_exact_post_only(self):
        item = {"music_metadata": {"music_info": {"music_asset_info": {
            "progressive_download_url": "https://cdn.test/selected.m4a"}}},
            "recommendations": {"audio_url": "https://cdn.test/unrelated.mp3"}}
        self.assertEqual(instagram_audio_urls(item), ["https://cdn.test/selected.m4a"])

    def test_instagram_carousel_child_music_is_extracted(self):
        item = {'carousel_media': [{'clips_metadata': {'music_info': {
            'music_asset_info': {'progressive_download_url': 'https://cdn.test/music.m4a'}}}}],
            'recommendations': {'audio_url': 'https://cdn.test/unrelated.mp3'}}
        self.assertEqual(instagram_audio_urls(item), ['https://cdn.test/music.m4a'])

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
                from telegram.request._requestparameter import RequestParameter
                encoded = RequestParameter.from_input("media", media)
                self.assertEqual(len(encoded.multipart_data), len(media))
                self.assertTrue(all(item["media"].startswith("attach://")
                                    for item in json.loads(encoded.json_value)))
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
                        patch("media_io.native_audio", side_effect=lambda path, dest: path),
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

    async def test_mp3_button_is_under_the_delivered_video(self):
        files = self.files(['.mp4'])
        sent_video = SimpleNamespace(edit_reply_markup=AsyncMock())
        async def send_video(**kwargs):
            self.messages.append(['video'])
            return sent_video
        self.msg.reply_video.side_effect = send_video
        status = SimpleNamespace(photo=None, video=None, audio=None, document=None, animation=None,
                                 edit_text=AsyncMock(), delete=AsyncMock())
        self.msg.chat_id = 42
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        markup = bot.InlineKeyboardMarkup([[bot.InlineKeyboardButton('MP3', callback_data='mp3|test')]])
        with patch('bot.download_in_worker', new_callable=AsyncMock, return_value=files), \
             patch('bot.has_audio', return_value=True), \
             patch('bot.post_mp3_button', return_value=markup), \
             patch('bot._global_sem', None), patch('bot._job_locks', {}):
            await bot.run_job(self.msg, context, 42, 'https://www.instagram.com/reel/test/', 'auto', status)
        sent_video.edit_reply_markup.assert_awaited_once()
        self.assertEqual(sent_video.edit_reply_markup.call_args.kwargs['reply_markup'], markup)
        self.assertIsNone(status.edit_text.call_args.kwargs['reply_markup'])

    async def test_photo_only_post_has_mp3_check_action(self):
        files = self.files([".jpg", ".jpg"])
        status = SimpleNamespace(photo=None, video=None, audio=None, document=None, animation=None,
                                 edit_text=AsyncMock(), delete=AsyncMock())
        self.msg.chat_id = 42
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        markup = bot.InlineKeyboardMarkup([[bot.InlineKeyboardButton('MP3', callback_data='mp3|test')]])
        with patch("bot.download_in_worker", new_callable=AsyncMock, return_value=files), \
                patch("bot.has_audio", return_value=False), \
                patch("bot.post_mp3_button", return_value=markup) as button, \
                patch("bot._global_sem", None), patch("bot._job_locks", {}):
            await bot.run_job(self.msg, context, 42, "https://www.pinterest.com/pin/42/", "auto", status)
        self.assertEqual(self.messages, [["photo", "photo"]])
        button.assert_called_once()
        self.assertEqual(button.call_args.args[2], [])
        self.assertEqual(status.edit_text.call_args.kwargs["reply_markup"], markup)

    async def test_silent_video_still_gets_mp3_check_action(self):
        files = self.files(['.mp4'])
        sent_video = SimpleNamespace(edit_reply_markup=AsyncMock())
        async def send_video(**kwargs):
            self.messages.append(['video'])
            return sent_video
        self.msg.reply_video.side_effect = send_video
        status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        self.msg.chat_id = 42
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        markup = bot.InlineKeyboardMarkup([[bot.InlineKeyboardButton('MP3', callback_data='mp3|test')]])
        with patch('bot.download_in_worker', new_callable=AsyncMock, return_value=files), \
             patch('bot.has_audio', return_value=False), \
             patch('bot.post_mp3_button', return_value=markup) as button, \
             patch('bot._global_sem', None), patch('bot._job_locks', {}):
            await bot.run_job(self.msg, context, 42, 'https://www.instagram.com/reel/test/', 'auto', status)
        button.assert_called_once()
        sent_video.edit_reply_markup.assert_awaited_once()

    async def test_mp3_button_is_sent_when_status_edit_fails_after_album(self):
        files = self.files([".jpg", ".mp4"])
        status = SimpleNamespace(photo=None, video=None, audio=None, document=None, animation=None,
                                 edit_text=AsyncMock(side_effect=BadRequest('status unavailable')),
                                 delete=AsyncMock())
        self.msg.chat_id = 42
        self.msg.reply_text = AsyncMock()
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        markup = bot.InlineKeyboardMarkup([[bot.InlineKeyboardButton("MP3", callback_data="mp3|test")]])
        with patch("bot.download_in_worker", new_callable=AsyncMock, return_value=files), \
                patch("bot.has_audio", side_effect=lambda p: p.suffix == ".mp4"), \
                patch("bot.post_mp3_button", return_value=markup), \
                patch("bot._global_sem", None), patch("bot._job_locks", {}):
            await bot.run_job(self.msg, context, 42, "https://www.instagram.com/p/test/", "auto", status)
        self.assertEqual(self.messages, [["photo", "video"]])
        self.msg.reply_text.assert_awaited_once_with("🎵 MP3", reply_markup=markup)

    async def test_expired_post_cache_redownloads_all_items(self):
        query = SimpleNamespace(data="mp3|post", from_user=SimpleNamespace(id=42),
                                answer=AsyncMock(), message=SimpleNamespace(chat_id=42, reply_text=AsyncMock()))
        manager = bot.JobManager(self.root / 'jobs.sqlite3')
        with patch("bot.resolve_action_data", side_effect=[
                {"url": "https://www.instagram.com/p/test/", "post": True, "children": ["a", "b"]},
                {"cache_path": ""}, {"cache_path": ""}]), patch("bot.run_job", new_callable=AsyncMock) as job, \
                patch('bot._jobs', manager):
            await bot.on_callback(SimpleNamespace(callback_query=query), None)
            await asyncio.gather(*list(manager.tasks.values()))
        job.assert_awaited_once()
        self.assertNotIn("selected_audio_index", job.call_args.kwargs)

    async def test_post_button_caches_all_tracks_and_checks_owner(self):
        with patch("bot.DATA_DIR", self.root), patch("bot.CACHE_DIR", self.root / "cache"), patch("bot.ACTIONS_FILE", self.root / "actions.json"):
            markup = bot.post_mp3_button(42, "https://www.instagram.com/p/abc/", self.files([".mp4", ".m4a"]), "title")
            token = markup.inline_keyboard[0][0].callback_data.split("|")[2]
            row = bot.resolve_action_data(42, token)
            self.assertTrue(row["post"])
            self.assertEqual(len(row["children"]), 2)
            self.assertEqual(bot.resolve_action_data(43, token), {})
            for child in row["children"]:
                self.assertTrue(Path(bot.resolve_action_data(42, child)["cache_path"]).is_file())

    async def test_mp3_recovers_after_render_loses_action_files(self):
        url = "https://www.instagram.com/p/abc/"
        with patch("bot.DATA_DIR", self.root), patch("bot.CACHE_DIR", self.root / "cache"), \
                patch("bot.ACTIONS_FILE", self.root / "actions.json"), patch("bot.BOT_TOKEN", "test-bot-token"):
            markup = bot.post_mp3_button(42, url, [], "title")
            data = markup.inline_keyboard[0][0].callback_data
            bot.ACTIONS_FILE.unlink()
            query = SimpleNamespace(data=data, from_user=SimpleNamespace(id=42),
                                    answer=AsyncMock(), message=SimpleNamespace(
                                        chat_id=42, reply_markup=markup,
                                        reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock()))))
            manager = bot.JobManager(self.root / "restarted.sqlite3")
            with patch("bot._jobs", manager), patch("bot.safe_remote_url", return_value=True), \
                    patch("bot.run_job", new_callable=AsyncMock) as job:
                await bot.on_callback(SimpleNamespace(callback_query=query), None)
                await asyncio.gather(*list(manager.tasks.values()))
            job.assert_awaited_once()
            self.assertEqual(job.call_args.args[3], url)
            unauthorized = SimpleNamespace(message=query.message)
            self.assertEqual(bot.recover_mp3_action(unauthorized, 43, data), {})

    async def test_both_polling_and_upload_use_ipv4_option(self):
        with patch.dict(os.environ, {"TELEGRAM_IPV4": "1"}), patch("telegram_network.environment_proxy", return_value=None), \
                patch("httpx.AsyncHTTPTransport", wraps=bot.httpx.AsyncHTTPTransport) as transport:
            app = bot.application_builder("123456:dummy-token-for-tests").build()
            self.assertEqual(transport.call_count, 4)
            for call in transport.call_args_list[::2]:
                self.assertEqual(call.kwargs["local_address"], "0.0.0.0")
            for call in transport.call_args_list[1::2]:
                self.assertIsNone(call.kwargs['local_address'])
            self.assertIsNot(app.bot._request[0], app.bot._request[1])
            for request in app.bot._request:
                await request.shutdown()

    async def test_automatic_preference_keeps_ipv4_fallback_for_both_requests(self):
        with patch.dict(os.environ, {"TELEGRAM_IPV4": "0"}), patch("telegram_network.environment_proxy", return_value=None), \
                patch("httpx.AsyncHTTPTransport", wraps=bot.httpx.AsyncHTTPTransport) as transport:
            app = bot.application_builder("123456:dummy-token-for-tests").build()
            self.assertEqual(transport.call_count, 4)
            for call in transport.call_args_list[::2]:
                self.assertIsNone(call.kwargs["local_address"])
            for call in transport.call_args_list[1::2]:
                self.assertEqual(call.kwargs["local_address"], "0.0.0.0")
            for request in app.bot._request:
                await request.shutdown()

    async def test_environment_proxy_only_after_direct_routes(self):
        with patch.dict(os.environ, {"TELEGRAM_IPV4": "1"}), \
                patch("telegram_network.environment_proxy", return_value="http://proxy.test:3128"), \
                patch("httpx.AsyncHTTPTransport", wraps=bot.httpx.AsyncHTTPTransport) as transport:
            request = bot.telegram_request()
            self.assertEqual(transport.call_count, 3)
            self.assertEqual(transport.call_args_list[0].kwargs['local_address'], '0.0.0.0')
            self.assertEqual(transport.call_args_list[0].kwargs['retries'], 0)
            self.assertIsNone(transport.call_args_list[0].kwargs['proxy'])
            self.assertIsNone(transport.call_args_list[1].kwargs['local_address'])
            self.assertEqual(transport.call_args_list[2].kwargs['proxy'], 'http://proxy.test:3128')
            await request.shutdown()

    async def test_custom_bot_api_does_not_fall_back_to_system_proxy(self):
        with patch('bot.TELEGRAM_API_BASE', 'http://127.0.0.1:8081'), \
                patch('telegram_network.environment_proxy', return_value='http://proxy.test:3128'), \
                patch('httpx.AsyncHTTPTransport', wraps=bot.httpx.AsyncHTTPTransport) as transport:
            request = bot.telegram_request()
            self.assertEqual(transport.call_count, 2)
            await request.shutdown()

    async def test_generic_proxy_does_not_hijack_telegram(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://broken.invalid:9999"}, clear=False), \
                patch("httpx.AsyncHTTPTransport", wraps=bot.httpx.AsyncHTTPTransport) as transport:
            os.environ.pop("TELEGRAM_PROXY", None)
            request = bot.telegram_request()
            self.assertIsNone(transport.call_args_list[0].kwargs["proxy"])
            await request.shutdown()
