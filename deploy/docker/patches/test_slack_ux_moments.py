"""Host tests for the KAGE_SLACK_UX moments module. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_slack_ux_moments.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2] / "agents" / "platform" / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

import slack_ux_moments as runtime
import verify_slack_ux_moments as verifier

PR = "https://github.com/acme/fleet-config/pull/412"
SUB = {"platform": "slack", "chat_id": "C0KAGE", "thread_id": "1700000000.000100", "team_id": "T1"}
QUESTION = {"kind": "needs_input", "reason": "Which cluster?\n- seeded-a\n- seeded-b"}


class _Client:
    def __init__(self, adapter):
        self.adapter = adapter

    async def chat_postMessage(self, **kwargs):
        if self.adapter.fail:
            raise RuntimeError("channel_not_found")
        self.adapter.posts.append(kwargs)
        return {"ts": "1700000000.000300"}


class _Adapter:
    def __init__(self, fail=False):
        self.posts = []
        self.teams = []
        self.fail = fail

    def _get_client(self, chat_id, team_id=None):
        self.teams.append(team_id)
        return _Client(self)


def _run(coro):
    return asyncio.run(coro)


class EnabledTest(unittest.TestCase):
    def test_off_unless_the_flag_is_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(runtime.enabled())
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"}):
            self.assertTrue(runtime.enabled())

    def test_off_when_the_renderer_is_missing(self):
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"}), mock.patch.object(runtime, "_moments", None):
            self.assertFalse(runtime.enabled())


class PrOpenedTest(unittest.TestCase):
    def setUp(self):
        runtime._announced.clear()

    def test_posts_in_the_cards_thread_once_per_pr(self):
        adapter = _Adapter()
        self.assertTrue(_run(runtime.pr_opened(adapter, SUB, f"Opened {PR}")))
        self.assertFalse(_run(runtime.pr_opened(adapter, SUB, f"Report: opened {PR}")))
        self.assertEqual(len(adapter.posts), 1)
        post = adapter.posts[0]
        self.assertEqual((post["channel"], post["thread_ts"]), ("C0KAGE", "1700000000.000100"))
        self.assertEqual(adapter.teams, ["T1"])
        self.assertIn("PR #412", post["text"])

    def test_another_channel_gets_its_own(self):
        adapter = _Adapter()
        _run(runtime.pr_opened(adapter, SUB, f"Opened {PR}"))
        _run(runtime.pr_opened(adapter, {**SUB, "chat_id": "C0OTHER"}, f"Opened {PR}"))
        self.assertEqual([p["channel"] for p in adapter.posts], ["C0KAGE", "C0OTHER"])

    def test_a_cited_pr_posts_nothing(self):
        adapter = _Adapter()
        self.assertFalse(_run(runtime.pr_opened(adapter, SUB, f"{PR} already covers it")))
        self.assertEqual(adapter.posts, [])

    def test_a_failed_post_is_retried_next_time(self):
        adapter = _Adapter(fail=True)
        self.assertFalse(_run(runtime.pr_opened(adapter, SUB, f"Opened {PR}")))
        adapter.fail = False
        self.assertTrue(_run(runtime.pr_opened(adapter, SUB, f"Opened {PR}")))

    def test_no_channel_or_no_client_posts_nothing(self):
        self.assertFalse(_run(runtime.pr_opened(_Adapter(), {**SUB, "chat_id": ""}, f"Opened {PR}")))
        self.assertFalse(_run(runtime.pr_opened(object(), SUB, f"Opened {PR}")))

    def test_the_announced_map_is_bounded(self):
        adapter = _Adapter()
        with mock.patch.object(runtime, "ANNOUNCED_MAX", 2):
            for n in range(3):
                _run(runtime.pr_opened(adapter, SUB, f"Opened {PR[:-3]}{n}"))
        self.assertEqual(len(runtime._announced), 2)


class NeedsYouTest(unittest.TestCase):
    def test_posts_a_needs_input_question_with_its_choices(self):
        adapter = _Adapter()
        self.assertTrue(_run(runtime.needs_you(adapter, SUB, QUESTION)))
        blocks = adapter.posts[0]["blocks"]
        labels = [e["text"]["text"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
        self.assertEqual(labels, ["seeded-a", "seeded-b"])
        self.assertEqual(adapter.posts[0]["thread_ts"], SUB["thread_id"])

    def test_other_kinds_and_empty_reasons_post_nothing(self):
        adapter = _Adapter()
        for payload in ({**QUESTION, "kind": "capability"}, {"kind": "needs_input", "reason": ""}, None, "x"):
            self.assertFalse(_run(runtime.needs_you(adapter, SUB, payload)), payload)
        self.assertEqual(adapter.posts, [])

    def test_a_failed_post_returns_false(self):
        self.assertFalse(_run(runtime.needs_you(_Adapter(fail=True), SUB, QUESTION)))


class VerifierTest(unittest.TestCase):
    def setUp(self):
        runtime._announced.clear()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "gateway").mkdir()
        shutil.copy(HERE / "slack_ux_moments.py", self.root / "gateway")
        shutil.copy(HERE / "kanban_progress_lines.py", self.root / "gateway")

    def test_passes_on_the_shipped_modules(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            verifier.main(self.root)

    def test_fails_when_the_caller_stops_importing_it(self):
        (self.root / "gateway" / "kanban_progress_lines.py").write_text("")
        with self.assertRaises(SystemExit):
            verifier.main(self.root)


if __name__ == "__main__":
    unittest.main()
