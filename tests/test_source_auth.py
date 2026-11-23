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


class SecretFileTests(unittest.TestCase):
    def test_render_secret_file_becomes_a_private_writable_copy(self):
        import source_auth
        data = b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n'
        settings = {f'{name}_COOKIES_B64': '' for name in source_auth.PLATFORMS}
        settings.update({f'{name}_COOKIE_FILE': '' for name in source_auth.PLATFORMS})
        with tempfile.TemporaryDirectory() as secrets, tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, settings), patch.object(source_auth, 'SECRET_DIR', Path(secrets)):
            (Path(secrets) / 'youtube_cookies.txt').write_bytes(data)
            files = materialize_source_cookies(Path(directory))
            self.assertEqual(set(files), {'YOUTUBE'})
            copy = files['YOUTUBE']
            self.assertTrue(copy.is_relative_to(Path(directory)))
            self.assertEqual(copy.read_bytes(), data)
            self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
            self.assertEqual(os.environ['YOUTUBE_COOKIE_FILE'], str(copy))

    def test_writable_owner_cookie_file_is_used_in_place(self):
        import source_auth
        settings = {f'{name}_COOKIES_B64': '' for name in source_auth.PLATFORMS}
        with tempfile.TemporaryDirectory() as secrets, tempfile.TemporaryDirectory() as directory:
            own = Path(directory) / 'phone.cookies'
            own.write_bytes(b'# Netscape HTTP Cookie File\n')
            settings['INSTAGRAM_COOKIE_FILE'] = str(own)
            with patch.dict(os.environ, settings), patch.object(source_auth, 'SECRET_DIR', Path(secrets)):
                self.assertEqual(materialize_source_cookies(Path(directory) / 'copies'), {})
                self.assertEqual(os.environ['INSTAGRAM_COOKIE_FILE'], str(own))
