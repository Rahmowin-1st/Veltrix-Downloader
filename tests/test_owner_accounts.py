"""Owner accounts on every platform: sign-in state, pacing and messages."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from source_auth import cookie_state, pace_account, signed_in

FUTURE = str(int(time.time()) + 365 * 86400)
PAST = str(int(time.time()) - 86400)


def jar(*rows):
    lines = [f'{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}' for domain, name, value, expiry in rows]
    return ('# Netscape HTTP Cookie File\n' + '\n'.join(lines) + '\n').encode()


class SignInStateTests(unittest.TestCase):
    def test_instagram_needs_a_session_id(self):
        self.assertTrue(signed_in('instagram', jar(('.instagram.com', 'sessionid', 'secret', FUTURE),
                                                   ('.instagram.com', 'csrftoken', 'x', FUTURE))))
        self.assertFalse(signed_in('INSTAGRAM', jar(('.instagram.com', 'csrftoken', 'x', FUTURE))))
        self.assertFalse(signed_in('INSTAGRAM', jar(('.instagram.com', 'sessionid', 'secret', PAST))))

    def test_pinterest_needs_a_session_and_auth_flag(self):
        signed = jar(('.pinterest.com', '_pinterest_sess', 'secret', FUTURE), ('.pinterest.com', '_auth', '1', FUTURE))
        visitor = jar(('.pinterest.com', '_pinterest_sess', 'secret', FUTURE), ('.pinterest.com', '_auth', '0', FUTURE))
        self.assertTrue(signed_in('pinterest', signed))
        self.assertFalse(signed_in('pinterest', visitor))

    def test_snapchat_state_is_unknown_and_files_report_by_platform(self):
        self.assertIsNone(signed_in('snapchat', jar(('.snapchat.com', 'sc-a-session', 'x', FUTURE))))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'snap.txt'
            path.write_bytes(jar(('.snapchat.com', 'x', 'y', FUTURE)))
            with patch.dict(os.environ, {'SNAPCHAT_COOKIE_FILE': str(path), 'INSTAGRAM_COOKIE_FILE': ''}):
                self.assertEqual(cookie_state('snapchat'), 'present')
                self.assertEqual(cookie_state('instagram'), 'missing')


class AccountPacingTests(unittest.TestCase):
    def test_new_jobs_on_one_platform_are_spaced_but_fallbacks_are_not(self):
        now = [1000.0]
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds

        with tempfile.TemporaryDirectory() as folder:
            pace = lambda platform, job: pace_account(platform, job, 5, Path(folder),
                                                      clock=lambda: now[0], sleep=sleep)
            self.assertEqual(pace('instagram', 'job1'), 0)
            now[0] += 1
            self.assertEqual(pace('instagram', 'job1'), 0)  # same job: a fallback route
            now[0] += 1
            self.assertEqual(pace('instagram', 'job2'), 4)  # 5 s after the previous use
            self.assertEqual(pace('youtube', 'job3'), 0)    # another platform has its own pace
            now[0] += 10
            self.assertEqual(pace('instagram', 'job4'), 0)
        self.assertEqual(waits, [4])

    def test_job_cookies_paces_only_when_an_account_is_used(self):
        with tempfile.TemporaryDirectory() as folder:
            cookies = Path(folder) / 'ig.txt'
            cookies.write_bytes(jar(('.instagram.com', 'sessionid', 'secret', FUTURE)))
            with patch('source_auth.pace_account') as pace, patch('bot.DATA_DIR', Path(folder)):
                with patch.dict(os.environ, {'INSTAGRAM_COOKIE_FILE': str(cookies)}):
                    with bot.job_cookies('instagram', Path(folder) / 'job') as copy:
                        self.assertIsNotNone(copy)
                pace.assert_called_once()
                self.assertEqual(pace.call_args.args[0], 'instagram')
                pace.reset_mock()
                with patch.dict(os.environ, {'INSTAGRAM_COOKIE_FILE': ''}):
                    with bot.job_cookies('instagram', Path(folder) / 'job2') as copy:
                        self.assertIsNone(copy)
                pace.assert_not_called()


class AccessMessageTests(unittest.TestCase):
    def test_access_messages_follow_the_owner_sign_in(self):
        error = RuntimeError('Login required to view this post')
        with tempfile.TemporaryDirectory() as folder:
            cookies = Path(folder) / 'ig.txt'
            cookies.write_bytes(jar(('.instagram.com', 'sessionid', 'secret', FUTURE)))
            with patch.dict(os.environ, {'INSTAGRAM_COOKIE_FILE': ''}):
                none = bot.friendly_error('instagram', error)
            with patch.dict(os.environ, {'INSTAGRAM_COOKIE_FILE': str(cookies)}):
                account = bot.friendly_error('instagram', error)
            cookies.write_bytes(jar(('.instagram.com', 'csrftoken', 'x', FUTURE)))
            with patch.dict(os.environ, {'INSTAGRAM_COOKIE_FILE': str(cookies)}):
                incomplete = bot.friendly_error('instagram', error)
        self.assertIn('requires access the bot does not have', none)
        self.assertIn("account cannot see this post", account)
        self.assertIn('sign-in is incomplete', incomplete)
        for text in (none, account, incomplete):
            self.assertNotIn('cookie', text.lower())


if __name__ == '__main__':
    unittest.main()
