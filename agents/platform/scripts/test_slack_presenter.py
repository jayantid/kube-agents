"""Unit tests for slack_presenter — answer layout, buttons and reactions.

Run: python3 -m pytest agents/platform/scripts/test_slack_presenter.py
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_presenter as sp


class FlagTest(unittest.TestCase):
    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(sp.enabled())

    def test_on_values(self):
        for value in ("1", "true", "TRUE", " yes ", "on"):
            with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": value}):
                self.assertTrue(sp.enabled(), value)

    def test_other_values_are_off(self):
        for value in ("", "0", "false", "off", "no", "enabled"):
            with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": value}):
                self.assertFalse(sp.enabled(), value)


class ArrivalReactionTest(unittest.TestCase):
    CASES = (
        # question / check -> eyes
        ("is checkout-gateway restarting? it looks like it's crashlooping?", "eyes"),
        ("why is the node pool at 90%?", "eyes"),
        ("check the ingress certs on seeded-a", "eyes"),
        ("<@U0KAGE> what version is seeded-b on", "eyes"),
        # change -> hammer_and_wrench
        ("fix it", "hammer_and_wrench"),
        ("can you fix the crashlooping pod?", "hammer_and_wrench"),
        ("bump the image to 1.4.2", "hammer_and_wrench"),
        ("roll back checkout to yesterday's release", "hammer_and_wrench"),
        ("please rollback the deploy", "hammer_and_wrench"),
        ("open a PR for the memory limit", "hammer_and_wrench"),
        ("open a pull request with that change", "hammer_and_wrench"),
        ("scale the web deployment down to 2", "hammer_and_wrench"),
        ("upgrade seeded-c to 1.31", "hammer_and_wrench"),
        # board -> clipboard
        ("board", "clipboard"),
        ("what's running right now?", "clipboard"),
        ("status of the fleet audit", "clipboard"),
        # incident -> rotating_light
        ("checkout is down", "rotating_light"),
        ("we're paging on payments", "rotating_light"),
        ("sev1 outage in us-east4", "rotating_light"),
        ("payments pods in crashloopbackoff", "rotating_light"),
        # no match -> eyes
        ("thanks", "eyes"),
        ("", "eyes"),
        (None, "eyes"),
    )

    def test_keyword_map(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(sp.arrival_reaction(text), expected)

    def test_word_boundaries(self):
        # "prefix", "downstream", "statusline" and "scaled-up names" are not the keywords.
        self.assertEqual(sp.arrival_reaction("the prefix looks odd"), "eyes")
        self.assertEqual(sp.arrival_reaction("downstream latency"), "eyes")
        self.assertEqual(sp.arrival_reaction("the statusline plugin"), "eyes")

    def test_change_beats_incident_and_question(self):
        self.assertEqual(sp.arrival_reaction("prod is down, roll back now"), "hammer_and_wrench")
        self.assertEqual(sp.arrival_reaction("should we scale down?"), "hammer_and_wrench")


class SettleReactionTest(unittest.TestCase):
    def test_outcomes(self):
        self.assertEqual(sp.settle_reaction("done"), "white_check_mark")
        self.assertEqual(sp.settle_reaction("blocked"), "double_vertical_bar")
        self.assertEqual(sp.settle_reaction("failed"), "x")
        self.assertIsNone(sp.settle_reaction("cancelled"))

    def test_kanban_kinds(self):
        self.assertEqual(sp.settle_for_kanban_kind("completed"), "done")
        self.assertEqual(sp.settle_for_kanban_kind("blocked"), "blocked")
        self.assertEqual(sp.settle_for_kanban_kind("review_requested"), "blocked")
        self.assertEqual(sp.settle_for_kanban_kind("gave_up"), "failed")
        self.assertEqual(sp.settle_for_kanban_kind("block_loop_detected"), "blocked")
        # Retried by the dispatcher, or bookkeeping: nothing has settled.
        for kind in ("crashed", "timed_out", "status", "heartbeat", "archived", "unblocked"):
            self.assertIsNone(sp.settle_for_kanban_kind(kind), kind)

    def test_no_reaction_is_a_removal(self):
        names = set(sp.SETTLE_REACTIONS.values()) | {
            sp.REACTION_QUESTION, sp.REACTION_CHANGE, sp.REACTION_BOARD, sp.REACTION_INCIDENT,
        }
        self.assertEqual(len(names), 7)


class BlocksAnswerTest(unittest.TestCase):
    def test_headline_only(self):
        self.assertEqual(
            sp.blocks_answer("All three clusters are healthy."),
            [{"type": "section", "text": {"type": "mrkdwn", "text": "*All three clusters are healthy.*"}}],
        )

    def test_headline_escaped(self):
        blocks = sp.blocks_answer("a < b & c")
        self.assertEqual(blocks[0]["text"]["text"], "*a &lt; b &amp; c*")

    def test_link_buttons(self):
        blocks = sp.blocks_answer(
            "h", links=[("Open PR ↗", "https://github.com/o/r/pull/1"), {"text": "Files", "url": "https://f"}]
        )
        self.assertEqual(
            blocks[1],
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open PR ↗", "emoji": True},
                        "action_id": "kage.link.0",
                        "url": "https://github.com/o/r/pull/1",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Files", "emoji": True},
                        "action_id": "kage.link.1",
                        "url": "https://f",
                    },
                ],
            },
        )
        for button in blocks[1]["elements"]:
            self.assertRegex(button["action_id"], sp.LINK_ACTION_ID_PATTERN)

    def test_choice_buttons_value_is_label(self):
        blocks = sp.blocks_answer("h", choices=["Raise to 512Mi", "Leave it"], action_id_prefix="triage")
        elements = blocks[1]["elements"]
        self.assertEqual([e["value"] for e in elements], ["Raise to 512Mi", "Leave it"])
        self.assertEqual([e["action_id"] for e in elements], ["triage.choice.0", "triage.choice.1"])
        self.assertNotIn("url", elements[0])
        for button in elements:
            self.assertIsNone(sp.LINK_ACTION_ID_PATTERN.search(button["action_id"]))

    def test_buttons_wrap_at_five(self):
        blocks = sp.blocks_answer("h", choices=[str(i) for i in range(7)])
        self.assertEqual([len(b["elements"]) for b in blocks[1:]], [5, 2])
        ids = [e["action_id"] for b in blocks[1:] for e in b["elements"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_long_label_clipped(self):
        button = sp.blocks_answer("h", choices=["word " * 40])[1]["elements"][0]
        self.assertLessEqual(len(button["text"]["text"]), sp.BUTTON_TEXT_MAX)
        self.assertEqual(button["value"], "word " * 40)

    def test_order(self):
        blocks = sp.blocks_answer("h", links=[("l", "https://l")], choices=["c"])
        self.assertEqual([b["type"] for b in blocks], ["section", "actions", "actions"])
        self.assertIn("url", blocks[1]["elements"][0])
        self.assertIn("value", blocks[2]["elements"][0])


class FallbackTextTest(unittest.TestCase):
    def test_same_layout_as_mrkdwn(self):
        text = sp.fallback_text("Two findings.", links=[("Open PR", "https://p")], choices=["Yes", "No"])
        self.assertEqual(text, "*Two findings.*\n<https://p|Open PR>\nReply with one of: Yes · No")

    def test_labels_cannot_mention_anyone(self):
        text = sp.fallback_text("h", links=[("<!here>", "https://p")], choices=["<@U1> & <!channel>", "No"])
        self.assertNotIn("<!", text)
        self.assertNotIn("<@", text)
        self.assertIn("&lt;@U1&gt; &amp; &lt;!channel&gt;", text)


class LinkAckTest(unittest.TestCase):
    def test_ack_does_nothing_else(self):
        ack = mock.AsyncMock()
        body = mock.Mock()
        asyncio.run(sp.ack_link_click(ack, body, {"action_id": "kage.link.0"}))
        ack.assert_awaited_once_with()
        self.assertEqual(body.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
