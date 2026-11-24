import io
import json
import unittest

from refresh_extractors import bump, latest_stable

REQUIREMENTS = ('python-telegram-bot[webhooks]==21.6\n'
                'yt-dlp[default]>=2026.10.26\n'
                'gallery-dl>=1.32.20\n'
                'deno>=2.9.0,<3\n')


class RefreshExtractorTests(unittest.TestCase):
    def test_raises_only_older_floors_and_keeps_everything_else(self):
        text, changes = bump(REQUIREMENTS, {'yt-dlp[default]': '2026.11.23', 'gallery-dl': '1.32.20'})
        self.assertEqual(changes, ['yt-dlp 2026.11.23'])
        self.assertEqual(text, REQUIREMENTS.replace('2026.10.26', '2026.11.23'))
        # Numeric comparison: 2026.11.3 is older than 2026.10.26 is false.
        text, changes = bump(REQUIREMENTS, {'yt-dlp[default]': '2026.9.30', 'gallery-dl': '1.32.100'})
        self.assertEqual(changes, ['gallery-dl 1.32.100'])
        self.assertIn('yt-dlp[default]>=2026.10.26\n', text)
        self.assertEqual(bump(REQUIREMENTS, {}), (REQUIREMENTS, []))

    def test_only_stable_pypi_versions_are_adopted(self):
        def opener(version):
            return lambda url, timeout: io.BytesIO(json.dumps({'info': {'version': version}}).encode())

        self.assertEqual(latest_stable('yt-dlp', opener=opener('2026.11.23')), '2026.11.23')
        for version in ('2026.12.1.234255.dev0', '1.33.0rc1', '', 'latest'):
            self.assertIsNone(latest_stable('yt-dlp', opener=opener(version)))


if __name__ == '__main__':
    unittest.main()
