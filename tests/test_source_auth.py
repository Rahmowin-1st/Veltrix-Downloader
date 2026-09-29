import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from source_auth import materialize_source_cookies


class SourceAuthTests(unittest.TestCase):
    def test_authorized_cookie_files_are_private_and_platform_specific(self):
        data = b'# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsid\tsecret\n'
        settings = {f'{name}_COOKIES_B64': '' for name in
                    ('YOUTUBE', 'INSTAGRAM', 'PINTEREST', 'SNAPCHAT')}
        settings['INSTAGRAM_COOKIES_B64'] = base64.b64encode(data).decode()
        settings['PINTEREST_COOKIES_B64'] = base64.b64encode(data).decode()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, settings):
            files = materialize_source_cookies(Path(directory))
            self.assertEqual(set(files), {'INSTAGRAM', 'PINTEREST'})
            for platform, path in files.items():
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(os.environ[f'{platform}_COOKIE_FILE'], str(path))

    def test_invalid_cookie_secret_never_exposes_payload(self):
        settings = {f'{name}_COOKIES_B64': '' for name in
                    ('YOUTUBE', 'INSTAGRAM', 'PINTEREST', 'SNAPCHAT')}
        settings['YOUTUBE_COOKIES_B64'] = base64.b64encode(b'secret-data').decode()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, settings):
            with self.assertLogs('veltrix.source_auth', level='WARNING') as logs:
                self.assertEqual(materialize_source_cookies(Path(directory)), {})
            self.assertNotIn('secret-data', ''.join(logs.output))
            self.assertEqual(list(Path(directory).iterdir()), [])
