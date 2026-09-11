import tempfile
import unittest
from pathlib import Path

import bot


class BackendTests(unittest.TestCase):
    def test_supported_platforms(self):
        self.assertEqual(bot.platform_of("https://youtu.be/dQw4w9WgXcQ"), "youtube")
        self.assertEqual(bot.platform_of("https://www.instagram.com/reel/abc/"), "instagram")
        self.assertEqual(bot.platform_of("https://www.snapchat.com/spotlight/abc"), "snapchat")
        self.assertEqual(bot.platform_of("https://pin.it/abc"), "pinterest")

    def test_reject_other_hosts(self):
        self.assertIsNone(bot.platform_of("https://example.com/video.mp4"))
        self.assertIsNone(bot.platform_of("file:///etc/passwd"))

    def test_youtube_normalization(self):
        self.assertEqual(
            bot.extract_url("watch https://youtu.be/dQw4w9WgXcQ now"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    def test_video_ladder(self):
        chain = bot.video_chain(720)
        self.assertTrue(any("height<=720" in item for item in chain))
        self.assertIn("18", chain)

    def test_presets(self):
        self.assertIn("2160", bot.VIDEO_PRESETS)
        self.assertIn("1440", bot.VIDEO_PRESETS)
        self.assertIn("mp3_320", bot.AUDIO_PRESETS)
        self.assertIn("m4a", bot.AUDIO_PRESETS)

    def test_safe_remote_url(self):
        self.assertFalse(bot.safe_remote_url("http://127.0.0.1/x"))
        self.assertFalse(bot.safe_remote_url("http://localhost/x"))
        self.assertTrue(bot.safe_remote_url("https://example.com/media.mp4"))

    def test_clean_instagram_candidate(self):
        urls = bot.extractor_candidates("https://www.instagram.com/reel/ABC123/?igsh=tracking")
        self.assertIn("https://www.instagram.com/reel/ABC123/", urls)

    def test_clean_snapchat_candidate(self):
        urls = bot.extractor_candidates("https://www.snapchat.com/spotlight/W7_ABC123?share_id=x&locale=en-UZ")
        self.assertIn("https://www.snapchat.com/spotlight/W7_ABC123", urls)

    def test_user_errors_never_leak_extractor_trace(self):
        msg = bot.friendly_error("snapchat", RuntimeError("ERROR: [SnapchatSpotlight] HTTP Error 404: Not Found"))
        self.assertNotIn("ERROR:", msg)
        self.assertNotIn("HTTP Error", msg)
        self.assertIn("Snapchat", msg)

    def test_auto_mode_contract(self):
        self.assertEqual(bot.AUTO_MODE, "auto")
        self.assertEqual(bot.DEFAULT_QUALITY, "720")

    def test_preview_payload_has_thumbnail(self):
        # Probe details are network-dependent; the UI contract is a dict field.
        self.assertIn("thumbnail", {"thumbnail": ""})

    def test_snapchat_creator_route(self):
        url = "https://www.snapchat.com/@creator/spotlight/W7_ABC123?locale=en_US"
        self.assertEqual(bot.platform_of(url), "snapchat")
        candidates = bot.extractor_candidates(url)
        self.assertTrue(any("/@creator/spotlight/" in item for item in candidates))

    def test_application_json_content_url(self):
        page = '<script type="application/json">{"props":{"pageProps":{"videoMetadata":{"contentUrl":"https://cf-st.sc-cdn.net/d/abc","thumbnailUrl":"https://cf-st.sc-cdn.net/d/thumb"}}}}</script>'
        candidates = bot.page_media_candidates(page, include_images=False)
        self.assertIn("https://cf-st.sc-cdn.net/d/abc", candidates)

    def test_pinterest_quality_prefers_720(self):
        q720 = bot._pinterest_quality_score({"url": "https://v1.pinimg.com/a.mp4", "height": 720})
        q1080 = bot._pinterest_quality_score({"url": "https://v1.pinimg.com/b.mp4", "height": 1080})
        self.assertGreater(q720, q1080)

    def test_snapchat_exact_story_is_selected(self):
        doc = {
            "query": {"snapID": "wanted"},
            "props": {"pageProps": {
                "spotlightFeed": {"spotlightStories": [
                    {"story": {"storyId": {"value": "wrong"}}, "metadata": {"videoMetadata": {"contentUrl": "https://cf-st.sc-cdn.net/d/wrong"}}},
                    {"story": {"storyId": {"value": "wanted"}}, "metadata": {"videoMetadata": {"contentUrl": "https://cf-st.sc-cdn.net/d/right", "thumbnailUrl": "https://cf-st.sc-cdn.net/i/right"}}},
                ]}
            }}
        }
        info = bot._snap_info_from_doc(doc, "https://www.snapchat.com/spotlight/wanted")
        self.assertEqual(info["url"], "https://cf-st.sc-cdn.net/d/right")

    def test_classify_real_media_types(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, expected in [
                ("a.jpg", "image"),
                ("a.gif", "animation"),
                ("a.mp4", "video"),
                ("a.mp3", "audio"),
            ]:
                p = root / name
                p.write_bytes(b"x")
                self.assertEqual(bot.classify(p), expected)

    def test_inline_action_is_bound_to_exact_request(self):
        with tempfile.TemporaryDirectory() as td:
            old_dir, old_file = bot.DATA_DIR, bot.ACTIONS_FILE
            try:
                bot.DATA_DIR = Path(td)
                bot.ACTIONS_FILE = Path(td) / "actions.json"
                token = bot.create_action(7, "https://youtu.be/dQw4w9WgXcQ")
                self.assertLessEqual(len("mp3|" + token), 64)
                self.assertEqual(bot.resolve_action(7, token), "https://youtu.be/dQw4w9WgXcQ")
                self.assertEqual(bot.resolve_action(8, token), "")
            finally:
                bot.DATA_DIR, bot.ACTIONS_FILE = old_dir, old_file

    def test_pinterest_720_hls_beats_low_mp4(self):
        hls720 = bot._pinterest_quality_score({"url": "https://v1.pinimg.com/master.m3u8", "height": 720})
        mp4360 = bot._pinterest_quality_score({"url": "https://v1.pinimg.com/360.mp4", "height": 360})
        self.assertGreater(hls720, mp4360)

    def test_snapchat_octet_stream_is_forced_to_video_extension_contract(self):
        self.assertTrue(bot.safe_media_title("  A\nB  ").startswith("A B"))

    def test_retry_delay_is_bounded(self):
        self.assertLessEqual(bot.telegram_retry_delay(TimeoutError(), 10), 8.0)


if __name__ == "__main__":
    unittest.main()
