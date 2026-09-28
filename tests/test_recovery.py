import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import NetworkError, TimedOut

import bot
import media_io
from runtime_jobs import JobManager, ChatTarget, mark_state
from telegram_network import ConnectFallback
from transfer import download, TransferLimit


class BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b'a' * 65536
        raise httpx.ReadError('connection lost')


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / 'media.mp4'
    def tearDown(self):
        self.tmp.cleanup()
    def get(self, handler, **kwargs):
        return download('https://cdn.test/clip', self.out, headers={}, safe_url=lambda u: 'cdn.test' in u,
                        budget=lambda _: None, max_bytes=200000, sleep=lambda _: None,
                        client_factory=lambda **opts: httpx.Client(transport=httpx.MockTransport(handler), **opts), **kwargs)

    def test_connection_loss_resumes_exact_remaining_bytes(self):
        requests = []
        def serve(req):
            requests.append(req)
            if len(requests) == 1:
                return httpx.Response(200, headers={'content-length': '131072', 'etag': '"version1"'}, stream=BrokenStream())
            self.assertEqual(req.headers['range'], 'bytes=65536-')
            self.assertEqual(req.headers['if-range'], '"version1"')
            return httpx.Response(206, headers={'content-range': 'bytes 65536-131071/131072', 'etag': '"version1"'}, content=b'b' * 65536)
        self.assertTrue(self.get(serve))
        self.assertEqual(self.out.read_bytes(), b'a' * 65536 + b'b' * 65536)
        self.assertEqual(len(requests), 2)
        self.assertFalse(self.out.with_suffix('.mp4.part').exists())

    def test_ignored_range_restarts_without_appending(self):
        count = 0
        def serve(req):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(200, headers={'content-length': '131072', 'etag': '"old"'}, stream=BrokenStream())
            return httpx.Response(200, headers={'etag': '"new"'}, content=b'new complete media')
        self.assertTrue(self.get(serve))
        self.assertEqual(self.out.read_bytes(), b'new complete media')

    def test_changed_etag_never_concatenates(self):
        count = 0
        def serve(req):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(200, headers={'content-length': '131072', 'etag': '"old"'}, stream=BrokenStream())
            return httpx.Response(206, headers={'content-range': 'bytes 65536-131071/131072', 'etag': '"new"'}, content=b'b' * 65536)
        with self.assertRaisesRegex(RuntimeError, 'resume'):
            self.get(serve)
        self.assertFalse(self.out.exists())

    def test_private_redirect_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
            self.get(lambda _: httpx.Response(302, headers={'location': 'http://127.0.0.1/secret'}))

    def test_storage_limit_not_swallowed_as_transient_error(self):
        with self.assertRaises(TransferLimit):
            self.get(lambda _: httpx.Response(200, headers={'content-length': '300000'}, content=b''))

    def test_non_media_response_never_becomes_a_photo(self):
        with self.assertRaisesRegex(RuntimeError, 'Non-media'):
            self.get(lambda _: httpx.Response(200, headers={'content-type': 'text/html'}, content=b'<html>denied'))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'jobs.sqlite3'
        self.jobs = JobManager(self.path, max_pending=3, per_user=2)
    def tearDown(self):
        self.tmp.cleanup()
    def add(self, n=1, uid=1):
        result, row = self.jobs.admit(uid, uid, f'https://youtu.be/{n}', 'auto')
        self.assertEqual(result, 'accepted')
        return row
    def test_restart_replays_only_pre_upload_jobs(self):
        a, b = self.add(), self.add(2)
        c = self.add(3, 2)
        self.jobs.state(b['id'], 'downloading')
        self.jobs.state(c['id'], 'sending')
        recovered = JobManager(self.path)
        ready, interrupted = recovered.recover()
        self.assertEqual({r['id'] for r in ready}, {a['id'], b['id']})
        self.assertEqual([r['id'] for r in interrupted], [c['id']])
        self.assertEqual(recovered.recent(2, 2)[0]['state'], 'interrupted')
    def test_queue_caps_duplicate_and_ownership(self):
        row = self.add()
        self.assertEqual(self.jobs.admit(1, 1, row['url'], 'auto')[0], 'duplicate')
        self.add(2)
        self.assertEqual(self.jobs.admit(1, 1, 'x', 'auto')[0], 'user_full')
        self.add(3, 2)
        self.assertEqual(self.jobs.admit(3, 3, 'y', 'auto')[0], 'full')
        self.assertFalse(self.jobs.recent(9, 1))
    def test_recovery_budget_prevents_crash_loop(self):
        self.add()
        for _ in range(3):
            self.assertEqual(len(self.jobs.recover()[0]), 1)
        self.assertEqual(self.jobs.recover()[0], [])


class SourceRecoveryTests(unittest.TestCase):
    def test_empty_gallery_error_allows_pinterest_fallback(self):
        with tempfile.TemporaryDirectory() as folder, patch('bot.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='[[-1, {"error": "unavailable"}]]')):
            with self.assertRaises(RuntimeError) as caught:
                bot.download_gallery('https://www.pinterest.com/pin/123/', folder)
            self.assertFalse(bot.fatal_download_error(caught.exception))

    def test_instagram_incomplete_metadata_is_not_swallowed(self):
        with patch('bot.extract_instagram_public', side_effect=RuntimeError('Incomplete Instagram carousel metadata')):
            with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
                bot.download_instagram_dedicated('https://www.instagram.com/p/x/', '/unused')

    def test_audio_bytes_override_incorrect_m4a_suffix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'wrong.m4a'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440',
                            '-t', '0.3', '-c:a', 'libopus', '-f', 'ogg', str(source)], check=True, timeout=15)
            result = media_io.native_audio(source, root / 'ready')
            self.assertEqual(result.suffix, '.mp3')
            self.assertEqual(media_io.probe(result)['streams'][0]['codec_name'], 'mp3')

    def test_conversion_process_is_reaped_on_shutdown(self):
        # Separate Python process keeps the shutdown flag out of this test runner.
        script = '''
import sys, time, threading
import media_process
worker = threading.Thread(target=lambda: media_process.run([sys.executable, '-c', 'import time; time.sleep(60)']))
worker.start()
for _ in range(100):
    with media_process._lock:
        started = bool(media_process._active)
    if started: break
    time.sleep(.01)
assert started
media_process.stop_all()
worker.join(2)
assert not worker.is_alive()
assert not media_process._active
'''
        subprocess.run([sys.executable, '-c', script], check=True, timeout=5)


class ConnectivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_intent_is_durable_before_network_and_not_replayed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = JobManager(root / 'jobs.sqlite3')
            _, row = manager.admit(1, 1, 'https://youtu.be/test', 'auto')
            photo = root / 'photo.jpg'
            photo.write_bytes(b'fixture')
            async def upload(**kwargs):
                self.assertEqual(manager.recent(1, 1)[0]['state'], 'sending')
                raise TimedOut()
            msg = SimpleNamespace(chat_id=1, reply_photo=AsyncMock(side_effect=upload))
            status = SimpleNamespace(edit_text=AsyncMock())
            context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
            async def run(_):
                await bot.run_job(msg, context, 1, row['url'], 'auto', status)
            with patch('bot.download_in_worker', new_callable=AsyncMock, return_value=[photo]), \
                    patch('bot.classify', return_value='image'), patch('bot.fit_image', side_effect=lambda p, d: p), \
                    patch('bot._global_sem', None), patch('bot._job_locks', {}):
                await manager.launch(row, run)
            self.assertEqual(manager.recent(1, 1)[0]['state'], 'interrupted')
            self.assertEqual(manager.recover(), ([], []))
            msg.reply_photo.assert_awaited_once()

    async def test_queued_cancellation_does_not_leak_metrics(self):
        lock = asyncio.Lock()
        await lock.acquire()
        status = SimpleNamespace(edit_text=AsyncMock())
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        msg = SimpleNamespace(chat_id=1)
        metrics = dict(bot._metrics, active_jobs=0, queued_jobs=0)
        with patch('bot._metrics', metrics), patch('bot._job_locks', {1: lock}), patch('bot._global_sem', None):
            task = asyncio.create_task(bot.run_job(msg, context, 1, 'https://youtu.be/x', 'auto', status))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(metrics['queued_jobs'], 0)
            self.assertEqual(metrics['active_jobs'], 0)
        lock.release()

    async def test_transport_fallback_only_before_request_transmission(self):
        for error, fallback_calls in [(httpx.ConnectError('dns'), 1), (httpx.ReadTimeout('uncertain'), 0), (httpx.WriteError('uncertain'), 0)]:
            first = SimpleNamespace(handle_async_request=AsyncMock(side_effect=error), aclose=AsyncMock())
            second = SimpleNamespace(handle_async_request=AsyncMock(return_value=httpx.Response(200)), aclose=AsyncMock())
            transport = ConnectFallback(first, second)
            if fallback_calls:
                self.assertEqual((await transport.handle_async_request(httpx.Request('POST', 'https://api.test'))).status_code, 200)
            else:
                with self.assertRaises(type(error)):
                    await transport.handle_async_request(httpx.Request('POST', 'https://api.test'))
            self.assertEqual(second.handle_async_request.await_count, fallback_calls)
            await transport.aclose()
            first.aclose.assert_awaited_once()
            second.aclose.assert_awaited_once()

    async def test_route_fallback_keeps_the_working_route(self):
        first = SimpleNamespace(handle_async_request=AsyncMock(side_effect=httpx.ConnectTimeout('connect')),
                                aclose=AsyncMock())
        second = SimpleNamespace(handle_async_request=AsyncMock(return_value=httpx.Response(200)),
                                 aclose=AsyncMock())
        transport = ConnectFallback(first, second)
        for _ in range(2):
            self.assertEqual((await transport.handle_async_request(httpx.Request('POST', 'https://api.test'))).status_code, 200)
        self.assertEqual(first.handle_async_request.await_count, 1)
        self.assertEqual(second.handle_async_request.await_count, 2)
        await transport.aclose()

    async def test_proxy_route_is_tried_only_after_both_connect_failures(self):
        first = SimpleNamespace(handle_async_request=AsyncMock(side_effect=httpx.ConnectTimeout('ipv4')),
                                aclose=AsyncMock())
        second = SimpleNamespace(handle_async_request=AsyncMock(side_effect=httpx.ConnectError('automatic')),
                                 aclose=AsyncMock())
        proxy = SimpleNamespace(handle_async_request=AsyncMock(return_value=httpx.Response(200)),
                                aclose=AsyncMock())
        transport = ConnectFallback(ConnectFallback(first, second), proxy)
        self.assertEqual((await transport.handle_async_request(
            httpx.Request('POST', 'https://api.test/getMe'))).status_code, 200)
        self.assertEqual(first.handle_async_request.await_count, 1)
        self.assertEqual(second.handle_async_request.await_count, 1)
        proxy.handle_async_request.assert_awaited_once()
        await transport.aclose()

    async def test_safe_upload_retry_and_uncertain_timeout(self):
        error = NetworkError('offline')
        error.__cause__ = httpx.ConnectError('dns')
        send = AsyncMock(side_effect=[error, 'delivered'])
        with patch('bot.asyncio.sleep', new_callable=AsyncMock):
            self.assertEqual(await bot._retry_telegram(send), 'delivered')
        send = AsyncMock(side_effect=TimedOut())
        with self.assertRaises(TimedOut):
            await bot._retry_telegram(send)
        send.assert_awaited_once()

    async def test_many_downloads_do_not_block_start(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = JobManager(Path(folder) / 'jobs.sqlite3')
            release = asyncio.Event()
            async def pending(*args, **kwargs):
                await release.wait()
                mark_state('done')
            with patch('bot._jobs', manager), patch('bot.run_job', side_effect=pending):
                for uid in range(12):
                    msg = SimpleNamespace(chat_id=uid, reply_text=AsyncMock())
                    await asyncio.wait_for(bot.dispatch_job(msg, None, uid, f'https://youtu.be/{uid}', 'auto'), .5)
                start = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))
                await asyncio.wait_for(bot.cmd_start(start, None), .5)
                start.effective_message.reply_text.assert_awaited_once()
                self.assertEqual(len(manager.tasks), 12)
                release.set()
                await asyncio.gather(*list(manager.tasks.values()))

    async def test_recovered_target_preserves_topic(self):
        api = SimpleNamespace(send_message=AsyncMock(), send_media_group=AsyncMock())
        target = ChatTarget(api, -123, 77)
        await target.reply_text('ready')
        api.send_message.assert_awaited_once_with(chat_id=-123, message_thread_id=77, text='ready')

    async def test_shutdown_retains_pre_upload_job_for_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = JobManager(Path(folder) / 'jobs.sqlite3')
            _, row = manager.admit(1, 1, 'https://youtu.be/x', 'auto')
            started = asyncio.Event()
            async def run(_):
                mark_state('downloading')
                started.set()
                await asyncio.Event().wait()
            manager.launch(row, run)
            await started.wait()
            await manager.shutdown()
            self.assertEqual(len(manager.recover()[0]), 1)
