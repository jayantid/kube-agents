"""Unit tests for slack_blocks_post — Block Kit through the credential proxy's Slack relay.

Run: python3 -m pytest agents/platform/scripts/test_slack_blocks_post.py
"""

import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_blocks_post as sbp

RELAY = "http://127.0.0.1:8765"
BLOCKS = [{"type": "divider"}]


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(body: dict) -> urllib.error.HTTPError:
    raw = json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(RELAY, 502, "Bad Gateway", {}, io.BytesIO(raw))


class PostTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {sbp.RELAY_ENV: RELAY})
        env.start()
        self.addCleanup(env.stop)

    def _post(self, answer, thread_ts=""):
        effect = answer if isinstance(answer, Exception) else None
        response = None if effect else _Response(json.dumps({"response": answer}).encode("utf-8"))
        with patch.object(sbp.urllib.request, "urlopen", return_value=response, side_effect=effect) as urlopen:
            ts = sbp.post("C1", "fallback", BLOCKS, thread_ts)
        return ts, urlopen.call_args.args[0]

    def test_posts_blocks_through_the_relay_and_returns_the_ts(self):
        ts, request = self._post({"ok": True, "ts": "1.2"}, thread_ts="0.9")
        self.assertEqual(ts, "1.2")
        self.assertEqual(request.full_url, RELAY + sbp.RELAY_API_PATH)
        payload = json.loads(request.data)
        self.assertEqual(payload["method"], "chat.postMessage")
        self.assertEqual(
            payload["arguments"]["json"],
            {"channel": "C1", "text": "fallback", "blocks": BLOCKS, "thread_ts": "0.9"},
        )

    def test_an_ok_without_a_ts_still_counts_as_posted(self):
        ts, _ = self._post({"ok": True})
        self.assertEqual(ts, "")

    def test_slack_saying_no_is_refused(self):
        with self.assertRaisesRegex(sbp.Refused, "invalid_blocks"):
            self._post({"ok": False, "error": "invalid_blocks"})

    def test_a_relayed_slack_error_is_refused(self):
        with self.assertRaisesRegex(sbp.Refused, "invalid_blocks"):
            self._post(_http_error({"error": "slack_api_error", "slack": {"error": "invalid_blocks"}}))

    def test_a_relay_failure_is_not_a_refusal(self):
        with self.assertRaises(urllib.error.HTTPError):
            self._post(_http_error({"error": "upstream timeout"}))

    def test_no_relay_posts_nothing(self):
        os.environ[sbp.RELAY_ENV] = " "
        self.assertFalse(sbp.configured())
        with patch.object(sbp.urllib.request, "urlopen") as urlopen, self.assertRaises(sbp.NotConfigured):
            sbp.post("C1", "fallback", BLOCKS)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
