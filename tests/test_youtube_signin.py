"""Owner-supplied sign-in cookies: inspection, per-job isolation and messages."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
import source_auth
from download_worker import failure_category
from source_auth import (job_cookie_copy, keep_job_cookies, youtube_cookie_state,
                         youtube_signed_in)
from vidfetch_provider import VidFetchQuotaExceeded

HEADER = '# Netscape HTTP Cookie File\n'
FUTURE = str(int(time.time()) + 365 * 86400)
PAST = str(int(time.time()) - 86400)
URL = 'https://www.youtube.com/watch?v=aqz-KE-bpKQ'
BOT_CHECK = "Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies"
SESSION_LIMIT = ("This content isn't available, try again later. Your account has been rate-limited by YouTube "
                 "for up to an hour.")


def jar(*rows: tuple[str, str, str, str]) -> bytes:
    lines = [f'{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}' for domain, name, value, expiry in rows]
    return (HEADER + '\n'.join(lines) + '\n').encode()


SIGNED_IN = jar(('.youtube.com', 'LOGIN_INFO', 'secret-login', FUTURE),
                ('.youtube.com', '__Secure-3PAPISID', 'secret-sid', FUTURE),
                ('.youtube.com', 'PREF', 'f6=40000000', FUTURE))


class SignInInspectionTests(unittest.TestCase):
    def test_signed_in_needs_login_info_and_a_sapisid_on_youtube(self):
        self.assertTrue(youtube_signed_in(SIGNED_IN))
        self.assertTrue(youtube_signed_in(jar(('www.youtube.com', 'LOGIN_INFO', 'x', '0'),
                                              ('.youtube.com', 'SAPISID', 'y', '0'))))
        # Browsers export HttpOnly cookies with a prefix.
        self.assertTrue(youtube_signed_in(SIGNED_IN.replace(b'.youtube.com\tTRUE\t/\tTRUE\t' + FUTURE.encode()
                                                            + b'\tLOGIN_INFO',
                                                            b'#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t'
                                                            + FUTURE.encode() + b'\tLOGIN_INFO', 1)))
        self.assertFalse(youtube_signed_in(jar(('.youtube.com', 'SAPISID', 'y', FUTURE))))
        self.assertFalse(youtube_signed_in(jar(('.youtube.com', 'LOGIN_INFO', 'x', FUTURE))))
        self.assertFalse(youtube_signed_in(jar(('.google.com', 'LOGIN_INFO', 'x', FUTURE),
                                               ('.google.com', 'SAPISID', 'y', FUTURE))))
        self.assertFalse(youtube_signed_in(jar(('.youtube.com', 'LOGIN_INFO', 'x', PAST),
                                               ('.youtube.com', 'SAPISID', 'y', FUTURE))))
        self.assertFalse(youtube_signed_in(b'not a cookie file'))

    def test_state_of_the_configured_file(self):
        with tempfile.TemporaryDirectory() as folder:
            signed_in, signed_out = Path(folder) / 'in.txt', Path(folder) / 'out.txt'
            signed_in.write_bytes(SIGNED_IN)
            signed_out.write_bytes(jar(('.youtube.com', 'PREF', 'x', FUTURE)))
            with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': ''}):
                self.assertEqual(youtube_cookie_state(), 'missing')
            with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': str(Path(folder) / 'absent.txt')}):
                self.assertEqual(youtube_cookie_state(), 'missing')
            with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': str(signed_out)}):
                self.assertEqual(youtube_cookie_state(), 'signed-out')
            with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': str(signed_in)}):
                self.assertEqual(youtube_cookie_state(), 'signed-in')

    def test_startup_reports_a_signed_out_export_without_values(self):
        settings = {f'{name}_COOKIES_B64': '' for name in source_auth.PLATFORMS}
        settings.update({f'{name}_COOKIE_FILE': '' for name in source_auth.PLATFORMS})
        data = jar(('.youtube.com', 'VISITOR_INFO1_LIVE', 'secret-visitor', FUTURE))
        with tempfile.TemporaryDirectory() as secrets, tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, settings), patch.object(source_auth, 'SECRET_DIR', Path(secrets)):
            (Path(secrets) / 'youtube_cookies.txt').write_bytes(data)
            with self.assertLogs('veltrix.source_auth', level='WARNING') as logs:
                source_auth.materialize_source_cookies(Path(directory))
            self.assertIn('no signed-in session', ''.join(logs.output))
            self.assertNotIn('secret-visitor', ''.join(logs.output))


class JobCookieTests(unittest.TestCase):
    def shared(self, folder: str, data: bytes = SIGNED_IN) -> Path:
        path = Path(folder) / 'shared.cookies'
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    def test_each_job_gets_a_private_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            shared = self.shared(folder)
            with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': str(shared)}):
                first = job_cookie_copy('youtube', Path(folder) / 'job1')
                second = job_cookie_copy('YOUTUBE', Path(folder) / 'job2')
            self.assertNotEqual(first, second)
            for copy in (first, second):
                self.assertNotEqual(copy, shared)
                self.assertEqual(copy.read_bytes(), SIGNED_IN)
                self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
            with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': ''}):
                self.assertIsNone(job_cookie_copy('youtube', folder))

    def test_refreshed_cookies_replace_the_shared_file_atomically(self):
        refreshed = SIGNED_IN.replace(b'f6=40000000', b'f6=40000400') + \
            b'.youtube.com\tTRUE\t/\tTRUE\t0\tYSC\tnew\n'
        with tempfile.TemporaryDirectory() as folder:
            shared = self.shared(folder)
            with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': str(shared)}):
                copy = job_cookie_copy('youtube', Path(folder) / 'job')
                copy.write_bytes(refreshed)
                inode = shared.stat().st_ino
                self.assertTrue(keep_job_cookies('youtube', copy))
            self.assertEqual(shared.read_bytes(), refreshed)
            self.assertNotEqual(shared.stat().st_ino, inode)  # replaced, never rewritten in place
            self.assertEqual(shared.stat().st_mode & 0o777, 0o600)
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ['job', 'shared.cookies'])

    def test_a_signed_out_or_broken_jar_never_replaces_the_owner_cookies(self):
        signed_out = jar(('.youtube.com', '__Secure-3PAPISID', 'secret-sid', FUTURE),
                         ('.youtube.com', 'PREF', 'f6=40000000', FUTURE))
        with tempfile.TemporaryDirectory() as folder:
            shared = self.shared(folder)
            with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': str(shared)}):
                for bad in (signed_out, b'', b'garbage\n', SIGNED_IN.replace(HEADER.encode(), b'')):
                    copy = job_cookie_copy('youtube', Path(folder) / 'job')
                    copy.write_bytes(bad)
                    self.assertFalse(keep_job_cookies('youtube', copy))
                    self.assertEqual(shared.read_bytes(), SIGNED_IN)
                self.assertFalse(keep_job_cookies('youtube', None))

    def test_dropping_an_already_expired_cookie_is_not_a_loss(self):
        stale = SIGNED_IN + jar(('.youtube.com', 'OLD', 'x', PAST)).removeprefix(HEADER.encode())
        with tempfile.TemporaryDirectory() as folder:
            shared = self.shared(folder, stale)
            with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': str(shared)}):
                copy = job_cookie_copy('youtube', Path(folder) / 'job')
                copy.write_bytes(SIGNED_IN.replace(b'f6=40000000', b'f6=2'))
                self.assertTrue(keep_job_cookies('youtube', copy))
            self.assertIn(b'f6=2', shared.read_bytes())

    def test_a_read_only_secret_file_is_never_modified(self):
        with tempfile.TemporaryDirectory() as secrets, tempfile.TemporaryDirectory() as folder:
            secret = Path(secrets) / 'youtube_cookies.txt'
            secret.write_bytes(SIGNED_IN)
            with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': str(secret)}), \
                    patch.object(source_auth, 'SECRET_DIR', Path(secrets)):
                copy = job_cookie_copy('youtube', folder)
                copy.write_bytes(SIGNED_IN + b'.youtube.com\tTRUE\t/\tTRUE\t0\tYSC\tnew\n')
                self.assertFalse(keep_job_cookies('youtube', copy))
            self.assertEqual(secret.read_bytes(), SIGNED_IN)

    def test_ytdlp_receives_a_job_copy_and_saves_refreshed_cookies_back(self):
        seen = []

        class FakeYDL:
            def __init__(self, params):
                self.params = params

            def __enter__(self):
                seen.append(self.params.get('cookiefile'))
                return self

            def __exit__(self, *args):
                # yt-dlp rewrites its cookie file in place when it closes.
                Path(self.params['cookiefile']).write_bytes(SIGNED_IN.replace(b'f6=40000000', b'f6=1'))

            def extract_info(self, *args, **kwargs):
                raise RuntimeError(BOT_CHECK)

        with tempfile.TemporaryDirectory() as folder:
            shared = self.shared(folder)
            job = Path(folder) / 'job'
            job.mkdir()
            with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': str(shared)}), \
                    patch('bot.YoutubeDL', FakeYDL), patch('bot.safe_remote_url', return_value=True):
                with self.assertRaisesRegex(RuntimeError, 'confirm you'):
                    bot.download_ytdlp(URL, 'auto', str(job))
            self.assertEqual(len(seen), 1)
            self.assertNotEqual(seen[0], str(shared))
            self.assertTrue(Path(seen[0]).is_relative_to(job))
            self.assertFalse(Path(seen[0]).exists())  # the private copy is removed after the job
            self.assertIn(b'f6=1', shared.read_bytes())

    def test_gallery_dl_receives_a_job_copy(self):
        commands = []

        def run(cmd, **kwargs):
            commands.append(list(cmd))
            path = Path(cmd[cmd.index('--cookies') + 1])
            self.assertEqual(path.read_bytes(), SIGNED_IN)

            class Result:
                returncode = 1
                stdout = ''
            return Result()

        with tempfile.TemporaryDirectory() as folder:
            shared = self.shared(folder)
            with patch.dict(os.environ, {'INSTAGRAM_COOKIE_FILE': str(shared)}), \
                    patch('bot.subprocess.run', side_effect=run):
                with self.assertRaisesRegex(RuntimeError, 'Gallery metadata unavailable'):
                    bot.download_gallery('https://www.instagram.com/p/abc/', str(Path(folder) / 'job'))
            used = Path(commands[0][commands[0].index('--cookies') + 1])
            self.assertNotEqual(used, shared)
            self.assertFalse(used.exists())
            self.assertEqual(shared.read_bytes(), SIGNED_IN)


class YouTubeBlockMessageTests(unittest.TestCase):
    def message(self, cookie_file: str) -> str:
        with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': cookie_file}):
            return bot.friendly_error('youtube', RuntimeError(BOT_CHECK))

    def test_bot_check_message_follows_the_owner_sign_in_state(self):
        with tempfile.TemporaryDirectory() as folder:
            signed_in, signed_out = Path(folder) / 'in.txt', Path(folder) / 'out.txt'
            signed_in.write_bytes(SIGNED_IN)
            signed_out.write_bytes(jar(('.youtube.com', 'PREF', 'x', FUTURE)))
            missing, incomplete, rejected = self.message(''), self.message(str(signed_out)), self.message(str(signed_in))
        self.assertIn('automated-traffic check', missing)
        self.assertIn('Instagram, Snapchat and Pinterest links still work', missing)
        self.assertIn('sign-in is incomplete', incomplete)
        self.assertIn('no longer accepts the bot’s sign-in', rejected)
        for text in (missing, incomplete, rejected):
            # Users must never be prompted to send account files in chat.
            self.assertNotIn('cookie', text.lower())
            self.assertTrue(text.startswith('YouTube: '))
        self.assertIn("automated-traffic check", bot.friendly_error('instagram', RuntimeError(BOT_CHECK)))

    def test_account_rate_limit_is_explained_and_retryable(self):
        error = RuntimeError(SESSION_LIMIT)
        self.assertIn('about an hour', bot.friendly_error('youtube', error))
        self.assertEqual(failure_category(error), 'RateLimited')

    def test_refused_sign_in_outranks_a_backup_provider_quota(self):
        with tempfile.TemporaryDirectory() as folder:
            cookies = Path(folder) / 'shared.cookies'
            cookies.write_bytes(SIGNED_IN)
            settings = {'VIDFETCH_API_KEY': 'key', 'NEWISTY_YOUTUBE': '0', 'COBALT_API_URL': ''}
            with patch.dict(os.environ, settings), patch('bot.safe_remote_url', return_value=True), \
                    patch('bot.download_youtube_public', return_value=[]), \
                    patch('vidfetch_provider.fetch', side_effect=VidFetchQuotaExceeded('VidFetch create HTTP 402')), \
                    patch('bot.download_ytdlp', side_effect=RuntimeError(BOT_CHECK)):
                with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': str(cookies)}):
                    with self.assertRaisesRegex(RuntimeError, 'confirm you'):
                        bot.grab(URL, 'auto', str(Path(folder) / 'a'))
                with patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': ''}):
                    with self.assertRaises(VidFetchQuotaExceeded):
                        bot.grab(URL, 'auto', str(Path(folder) / 'b'))

    def test_an_account_rate_limit_moves_to_the_backup_provider_at_once(self):
        calls = []

        class FakeYDL:
            def __init__(self, params):
                self.params = params

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def extract_info(self, *args, **kwargs):
                calls.append(self.params['format'])
                raise RuntimeError(SESSION_LIMIT)

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'VIDFETCH_API_KEY': 'key', 'NEWISTY_YOUTUBE': '0', 'COBALT_API_URL': '', 'YOUTUBE_COOKIE_FILE': ''}), \
                patch('bot.safe_remote_url', return_value=True), patch('bot.download_youtube_public', return_value=[]), \
                patch('bot.YoutubeDL', FakeYDL), patch('vidfetch_provider.fetch', return_value=[]) as provider:
            with self.assertRaisesRegex(RuntimeError, 'rate-limited'):
                bot.grab(URL, 'auto', folder)
        self.assertEqual(calls, ['bv*+ba/b/ba'])  # no second selector against a limited session
        provider.assert_called_once()


if __name__ == '__main__':
    unittest.main()
