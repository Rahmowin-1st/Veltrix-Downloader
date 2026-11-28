"""YouTube's JS challenge solver must fit a 512 MB host and explain its failures."""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
import media_io
from download_worker import failure_category


class DenoLauncherTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(self.folder)], check=False))
        self.real = self.folder / 'real-deno'
        # Prints its V8 flags and arguments; "sleep" keeps it busy for the lock test.
        self.real.write_text('#!/bin/sh\nif [ "$1" = sleep ]; then sleep 2; fi\necho "$DENO_V8_FLAGS|$*"\n')
        self.real.chmod(0o755)
        with patch('bot.DATA_DIR', self.folder / 'data'):
            self.launcher = bot.deno_launcher(str(self.real))

    def run_launcher(self, *args, env=None, timeout=10):
        environment = {key: value for key, value in os.environ.items() if key != 'DENO_V8_FLAGS'}
        environment.update(env or {})
        return subprocess.run([self.launcher, *args], capture_output=True, text=True, timeout=timeout,
                              env=environment)

    def test_runs_deno_with_a_small_v8_semi_space_and_the_same_arguments(self):
        self.assertNotEqual(self.launcher, str(self.real))
        self.assertTrue(os.access(self.launcher, os.X_OK))
        done = self.run_launcher('run', '--ext=js', '-')
        self.assertEqual(done.stdout.strip(), '--max-semi-space-size=1|run --ext=js -')
        # An owner value wins; an empty one would make Deno fail, so it is replaced.
        self.assertEqual(self.run_launcher('run', env={'DENO_V8_FLAGS': '--max-old-space-size=200'}).stdout.strip(),
                         '--max-old-space-size=200|run')
        self.assertEqual(self.run_launcher('run', env={'DENO_V8_FLAGS': ''}).stdout.strip(),
                         '--max-semi-space-size=1|run')

    def test_only_one_solver_runs_at_a_time_but_version_checks_never_wait(self):
        first = subprocess.Popen([self.launcher, 'sleep'], stdout=subprocess.PIPE, text=True)
        self.addCleanup(first.kill)
        time.sleep(0.5)
        started = time.monotonic()
        self.assertIn('--version', self.run_launcher('--version').stdout)
        self.assertLess(time.monotonic() - started, 1.5)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_launcher('run', timeout=0.8)  # waits for the running solver
        first.communicate(timeout=10)
        self.assertEqual(self.run_launcher('run').returncode, 0)

    def test_the_launcher_is_rewritten_only_when_it_changes(self):
        target = Path(self.launcher)
        before = target.stat().st_mtime_ns
        with patch('bot.DATA_DIR', self.folder / 'data'):
            self.assertEqual(bot.deno_launcher(str(self.real)), self.launcher)
        self.assertEqual(target.stat().st_mtime_ns, before)
        self.assertIsNone(bot.deno_launcher(None))

    def test_ytdlp_uses_the_launcher(self):
        with patch('bot.DENO_LAUNCHER', self.launcher):
            opts = bot.base_ydl_opts(str(self.folder / 'job'))
        self.assertEqual(opts['js_runtimes'], {'deno': {'path': self.launcher}})


class MemoryAwareConcurrencyTests(unittest.TestCase):
    def limit(self, content):
        files = {'/sys/fs/cgroup/memory.max': content}

        def read_text(path, *args, **kwargs):
            if str(path) in files and files[str(path)] is not None:
                return files[str(path)]
            raise OSError('missing')

        return patch.object(media_io.Path, 'read_text', read_text)

    def test_memory_limit_from_cgroups(self):
        with self.limit('536870912\n'):
            self.assertEqual(media_io.memory_limit_bytes(), 512 * 1024 * 1024)
        with self.limit('max\n'):
            self.assertIsNone(media_io.memory_limit_bytes())
        with self.limit(None):
            self.assertIsNone(media_io.memory_limit_bytes())

    def test_a_512_mb_host_runs_one_job_unless_the_owner_sets_more(self):
        with patch.dict(os.environ, {'MAX_CONCURRENT_JOBS': ''}):
            with patch('media_io.memory_limit_bytes', return_value=512 * 1024 * 1024):
                self.assertEqual(bot.default_concurrency(), 1)
            with patch('media_io.memory_limit_bytes', return_value=2 * 1024 ** 3):
                self.assertEqual(bot.default_concurrency(), 2)
            with patch('media_io.memory_limit_bytes', return_value=None):
                self.assertEqual(bot.default_concurrency(), 2)
        with patch.dict(os.environ, {'MAX_CONCURRENT_JOBS': '3'}), \
                patch('media_io.memory_limit_bytes', return_value=512 * 1024 * 1024):
            self.assertEqual(bot.default_concurrency(), 3)


class ChallengeFailureTests(unittest.TestCase):
    def test_a_failed_solver_is_explained_and_recorded(self):
        attempts = []

        class FakeYDL:
            def __init__(self, params):
                self.params = params

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def extract_info(self, *args, **kwargs):
                attempts.append(self.params['format'])
                self.params['logger'].warning('[youtube] abc: n challenge solving failed: Some formats may be '
                                              'missing. Error running deno process (returncode: -9) https://x.y/z')
                raise RuntimeError('ERROR: [youtube] abc: Requested format is not available. '
                                   'Use --list-formats for a list of available formats')

        with tempfile.TemporaryDirectory() as folder, patch('bot.YoutubeDL', FakeYDL), \
                patch('bot.safe_remote_url', return_value=True), \
                patch.dict(os.environ, {'YOUTUBE_COOKIE_FILE': ''}):
            with self.assertRaises(RuntimeError) as caught:
                bot.download_ytdlp('https://www.youtube.com/watch?v=aqz-KE-bpKQ', 'auto', folder)
        notes = getattr(caught.exception, '__notes__', [])
        self.assertTrue(notes and notes[0].startswith('YouTube JS challenge: '))
        self.assertIn('returncode: -9', notes[0])
        self.assertNotIn('https://', notes[0])
        message = bot.friendly_error('youtube', caught.exception)
        self.assertIn('could not unlock this video', message)
        self.assertEqual(failure_category(caught.exception), 'ExtractionFailed')  # keeps the Try again button
        self.assertEqual(attempts, ['bv*+ba/b/ba'])  # the failed solver is not run a second time


if __name__ == '__main__':
    unittest.main()
