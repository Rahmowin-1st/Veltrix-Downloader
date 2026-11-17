import json
import unittest
from unittest.mock import patch

import httpx
import bot


ID = 'W7_EDlXWTBiXAEEniNoMPwAAYYnlvaXl5amp6AZ1H3xfEAZ1H2YQIAAAAAQ'


class SnapchatLookupTests(unittest.TestCase):
    def setUp(self):
        proxy = patch('bot.PROXY', '')
        proxy.start()
        self.addCleanup(proxy.stop)

    def test_exact_lookup_uses_full_video_and_never_a_preview_or_recommendation(self):
        requests = []
        def handle(req):
            requests.append(req)
            return httpx.Response(200, json={'items': [
                {'video': {'rawSnapId': 'other', 'videoUrl': 'https://cdn.test/wrong.mp4'}},
                {'tile': {'videoPreviewUrl': 'https://cdn.test/preview.mp4'},
                 'video': {'rawSnapId': ID, 'videoUrl': 'https://cdn.test/full.mp4', 'caption': 'Exact'}}]})
        with patch('bot.safe_remote_url', return_value=True):
            info = bot.snapchat_lookup(ID, client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), **kw))
        self.assertEqual(info['entries'][0]['formats'][0]['url'], 'https://cdn.test/full.mp4')
        self.assertEqual(json.loads(requests[0].content), {'source': 'lookup', 'videoIds': [ID]})
        self.assertNotIn('authorization', requests[0].headers)

    def test_missing_unsafe_and_redirected_results_do_not_substitute_another_clip(self):
        responses = [httpx.Response(200, json={'items': []}),
                     httpx.Response(200, json={'items': [{'video': {'rawSnapId': 'other', 'videoUrl': 'https://cdn.test/a.mp4'}}]}),
                     httpx.Response(200, json={'items': [{'video': {'rawSnapId': ID, 'videoUrl': 'http://127.0.0.1/a.mp4'}}]}),
                     httpx.Response(302, headers={'location': 'https://other.test/'})]
        for response in responses:
            with patch('bot.safe_remote_url', return_value=False):
                self.assertEqual(bot.snapchat_lookup(ID,
                    client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(lambda _: response), **kw)), {})

    def test_page_failure_uses_lookup_and_caches_the_exact_result(self):
        info = {'id': ID, 'entries': [{'kind': 'video', 'formats': [{'url': 'https://cdn.test/full.mp4'}]}]}
        url = 'https://www.snapchat.com/spotlight/' + ID
        with patch('bot.metadata_cache_get', return_value={}), patch('bot.metadata_cache_put') as cache, \
             patch('bot.extractor_candidates', return_value=[url]), patch('bot.fetch_public_page', side_effect=RuntimeError('page changed')), \
             patch('bot.snapchat_lookup', return_value=info) as lookup:
            self.assertEqual(bot.extract_snapchat_public(url), info)
            lookup.assert_called_once_with(ID)
            cache.assert_called_once_with('snapchat:' + url, info)
