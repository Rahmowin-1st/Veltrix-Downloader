import unittest

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


if __name__ == "__main__":
    unittest.main()
