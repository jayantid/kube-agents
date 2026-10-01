"""Unit tests for slack_presenter — answer layout, buttons and reactions.

Run: python3 -m pytest agents/platform/scripts/test_slack_presenter.py
"""

import asyncio
import os
import sys
import time
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


class SplitAnswerTest(unittest.TestCase):
    def test_first_sentence_is_headline(self):
        md = "checkout-gateway is crashlooping on an OOM. It hit its 256Mi limit.\n\nRaise it to 512Mi."
        headline, body = sp.split_answer(md)
        self.assertEqual(headline, "checkout-gateway is crashlooping on an OOM.")
        self.assertEqual(body, ["It hit its 256Mi limit.", "Raise it to 512Mi."])

    def test_markdown_stripped_from_headline(self):
        headline, body = sp.split_answer("## **Yes**, [seeded-a](https://x) is `healthy`\n- one\n- two")
        self.assertEqual(headline, "Yes, seeded-a is healthy")
        self.assertEqual(body, ["- one\n- two"])

    def test_empty(self):
        self.assertEqual(sp.split_answer(""), ("", []))
        self.assertEqual(sp.split_answer("   \n\n "), ("", []))

    def test_code_fence_not_split(self):
        md = "Here it is.\n\n```\na\n\nb\n```\n\nDone."
        _, body = sp.split_answer(md)
        self.assertEqual(body, ["```\na\n\nb\n```", "Done."])

    def test_leading_code_block_has_no_headline(self):
        headline, body = sp.split_answer("```\nkubectl get pods\n```")
        self.assertEqual(headline, "")
        self.assertEqual(body, ["```\nkubectl get pods\n```"])

    def test_an_abbreviation_does_not_end_the_headline(self):
        for line in (
            "Pods fail readiness, e.g. checkout-gateway in ns. prod.",
            "Pods fail readiness, e.g. Checkout in prod.",
        ):
            self.assertEqual(sp.split_answer(line), (line, []))

    def test_a_headline_is_not_cut_after_etc(self):
        headline, _body = sp.split_answer("Deployments, StatefulSets, etc. All fail readiness. Node-a is cordoned.")
        self.assertEqual(headline, "Deployments, StatefulSets, etc. All fail readiness.")

    def test_a_soft_wrapped_first_sentence_is_the_whole_headline(self):
        md = "payments-api keeps crashing: secret\npayments-db-creds is missing. Since 14:02.\n- one"
        headline, body = sp.split_answer(md)
        self.assertEqual(headline, "payments-api keeps crashing: secret payments-db-creds is missing.")
        self.assertEqual(body, ["Since 14:02.\n- one"])

    def test_a_fence_under_the_first_line_is_not_joined_into_the_headline(self):
        md = "payments-api is crashlooping with:\n```\nOOMKilled. Exit 137\n```\nSince 14:02."
        headline, body = sp.split_answer(md)
        self.assertEqual(headline, "payments-api is crashlooping with:")
        self.assertEqual(body, ["```\nOOMKilled. Exit 137\n```\nSince 14:02."])

    def test_a_leading_code_span_is_not_a_fence(self):
        md = "```payments-api``` is crashlooping on OOM.\n\nRaise the limit."
        headline, body = sp.split_answer(md)
        self.assertEqual(headline, "payments-api is crashlooping on OOM.")
        self.assertEqual(body, ["Raise the limit."])

    def test_a_period_inside_a_code_span_does_not_end_the_headline(self):
        headline, body = sp.split_answer("The pod logs `connection refused. Retrying` on every start. Since 14:02.")
        self.assertEqual(headline, "The pod logs connection refused. Retrying on every start.")
        self.assertEqual(body, ["Since 14:02."])

    def test_fallback_text_keeps_a_plain_headline_as_given(self):
        headline, _body = sp.split_answer("`__init__.py` is missing.")
        self.assertEqual(sp.fallback_text(headline), "*__init__.py is missing.*")

    def test_a_leading_heading_is_not_joined_to_the_next_line(self):
        headline, body = sp.split_answer("##\tSummary\nCheckout is down. Since 14:02.")
        self.assertEqual(headline, "Summary")
        self.assertEqual(body, ["Checkout is down. Since 14:02."])

    def test_bold_around_the_first_sentence_leaves_no_stray_markers(self):
        headline, body = sp.split_answer("**Checkout is crashlooping on OOM. Raise the limit.**")
        self.assertEqual(headline, "Checkout is crashlooping on OOM.")
        self.assertEqual(body, ["**Raise the limit.**"])
        headline, body = sp.split_answer("**Checkout is down.** Raise the limit.")
        self.assertEqual(headline, "Checkout is down.")
        self.assertEqual(body, ["Raise the limit."])
        self.assertEqual(sp.fallback_text(headline), "*Checkout is down.*")

    def test_an_in_word_double_marker_is_not_rebalanced_as_bold(self):
        headline, body = sp.split_answer("The env var DB__HOST is unset. Pods crashloop.")
        self.assertEqual(headline, "The env var DB__HOST is unset.")
        self.assertEqual(body, ["Pods crashloop."])
        headline, body = sp.split_answer("Memory is 2**20 bytes. Raise it.")
        self.assertEqual(headline, "Memory is 2**20 bytes.")
        self.assertEqual(body, ["Raise it."])

    def test_plain_keeps_paired_in_word_double_markers(self):
        self.assertEqual(sp.split_answer("DB__HOST or DB__PORT is unset.")[0], "DB__HOST or DB__PORT is unset.")
        self.assertEqual(sp._plain("Set DB__HOST and DB__PORT"), "Set DB__HOST and DB__PORT")
        self.assertEqual(sp._plain("2**20 and 2**30 bytes"), "2**20 and 2**30 bytes")
        self.assertEqual(sp._plain("**Checkout** and __payments__ are down"), "Checkout and payments are down")

    def test_plain_strips_in_word_star_bold_but_not_underscore_bold(self):
        self.assertEqual(sp._plain("**Pod**s are down"), "Pods are down")
        self.assertEqual(sp._plain("It re**start**ed twice"), "It restarted twice")
        self.assertEqual(sp._plain("**3**x faster"), "3x faster")
        self.assertEqual(sp._plain("snake__case__name"), "snake__case__name")
        self.assertEqual(sp._plain("x**2 and y**2"), "x**2 and y**2")
        headline, body = sp.split_answer("**Pod**s are down. Restart them.")
        self.assertEqual(headline, "Pods are down.")
        self.assertEqual(body, ["Restart them."])

    def test_more_abbreviations_do_not_end_the_headline(self):
        for line in ("Node pool np-1 at rev. 7 is cordoned.", "Certs expired Sept. 30 on seeded-a."):
            self.assertEqual(sp.split_answer(line), (line, []))

    def test_an_abbreviation_in_parentheses_does_not_end_the_headline(self):
        for line, headline in (
            ("Several pods fail (e.g. Checkout) in prod. Raise it.", "Several pods fail (e.g. Checkout) in prod."),
            ("Pods fail (i.e. Checkout). Raise it.", "Pods fail (i.e. Checkout)."),
        ):
            self.assertEqual(sp.split_answer(line)[0], headline)

    def test_max_min_and_no_end_a_sentence(self):
        for line, headline in (
            ("Replicas are at max. Raise the HPA ceiling.", "Replicas are at max."),
            ("Restarted after 5 min. Raise it.", "Restarted after 5 min."),
            ("The answer is no. Checkout is down.", "The answer is no."),
        ):
            self.assertEqual(sp.split_answer(line)[0], headline)

    def test_a_nul_in_the_answer_is_dropped(self):
        self.assertEqual(sp.split_answer("Hello \x005\x00 world. More."), ("Hello 5 world.", ["More."]))

    def test_many_abbreviations_stay_linear(self):
        line = "See e.g. A " * 4000 + "end."
        started = time.monotonic()
        sp.split_answer(line)
        self.assertLess(time.monotonic() - started, 2)

    def test_plain_leaves_code_spans_and_globs_alone(self):
        self.assertEqual(sp._plain("`__init__.py` is missing"), "__init__.py is missing")
        self.assertEqual(sp._plain("Delete `__pycache__` and **this**"), "Delete __pycache__ and this")
        self.assertEqual(sp._plain("Remove *.log,*.tmp"), "Remove *.log,*.tmp")

    def test_clip_keeps_the_start_of_a_long_word(self):
        clipped = sp._clip("apply Option B: " + "x" * 120, sp.BUTTON_TEXT_MAX)
        self.assertTrue(clipped.startswith("apply Option B: xxx"))
        self.assertLessEqual(len(clipped), sp.BUTTON_TEXT_MAX)
        self.assertEqual(sp._clip("word " * 30, 20), "word word word…")

    def test_plain_keeps_a_literal_star_and_drops_markup(self):
        self.assertEqual(sp._plain("Scale replicas 2*3 → 6"), "Scale replicas 2*3 → 6")
        self.assertEqual(sp._plain("Delete *.tmp under /var/cache"), "Delete *.tmp under /var/cache")
        self.assertEqual(sp._plain("**Bold**, *italic*, _also_ and `code`"), "Bold, italic, also and code")
        self.assertEqual(
            sp._plain("see [logs](https://x/query=(k8s_container)) now"), "see logs now"
        )

    def test_a_numbered_line_is_not_cut_at_its_number(self):
        headline, body = sp.split_answer("1. Checkout is crashlooping on OOM. It hit 256Mi.")
        self.assertEqual(headline, "Checkout is crashlooping on OOM.")
        self.assertEqual(body, ["It hit 256Mi."])

    def test_leading_tilde_block_has_no_headline(self):
        headline, body = sp.split_answer("~~~\nkubectl get pods\n~~~")
        self.assertEqual(headline, "")
        self.assertEqual(body, ["~~~\nkubectl get pods\n~~~"])

    def test_long_headline_clipped(self):
        headline, _ = sp.split_answer("word " * 100)
        self.assertLessEqual(len(headline), sp.HEADLINE_MAX)
        self.assertTrue(headline.endswith("…"))


class ButtonsTest(unittest.TestCase):
    def test_link_button(self):
        self.assertEqual(
            sp._button("Open PR ↗", "kage.link.0", url="https://github.com/o/r/pull/1"),
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Open PR ↗", "emoji": True},
                "action_id": "kage.link.0",
                "url": "https://github.com/o/r/pull/1",
            },
        )
        self.assertRegex("kage.link.0", sp.LINK_ACTION_ID_PATTERN)

    def test_choice_button_value_is_label(self):
        button = sp._button("Raise to 512Mi", "triage.choice.0", value="Raise to 512Mi")
        self.assertEqual(button["value"], "Raise to 512Mi")
        self.assertNotIn("url", button)
        self.assertRegex(button["action_id"], sp.CHOICE_ACTION_ID_PATTERN)
        self.assertIsNone(sp.LINK_ACTION_ID_PATTERN.search(button["action_id"]))

    def test_a_long_choice_posts_only_what_its_button_shows(self):
        # The click handler posts the value as the user's turn, so a value
        # longer than the label would send words the user never saw.
        choice = "look at: " + "word " * 40 + "HIDDEN-TAIL"
        actions = [b for b in sp.blocks_report("h", choices=[choice], action_id_prefix="kage") if b["type"] == "actions"]
        (button,) = actions[0]["elements"]
        self.assertEqual(button["value"], button["text"]["text"])
        self.assertLessEqual(len(button["value"]), sp.BUTTON_TEXT_MAX)
        self.assertNotIn("HIDDEN-TAIL", button["value"])

    def test_buttons_wrap_at_five(self):
        rows = sp._actions([sp._button(str(i), f"kage.choice.{i}", value=str(i)) for i in range(7)])
        self.assertEqual([len(b["elements"]) for b in rows], [5, 2])
        self.assertEqual({b["type"] for b in rows}, {"actions"})

    def test_long_label_clipped(self):
        button = sp._button("word " * 40, "kage.choice.0", value="word " * 40)
        self.assertLessEqual(len(button["text"]["text"]), sp.BUTTON_TEXT_MAX)
        self.assertEqual(button["value"], "word " * 40)


class FallbackTextTest(unittest.TestCase):
    def test_same_layout_as_mrkdwn(self):
        text = sp.fallback_text(
            "Two findings.",
            rows=[{"text": "seeded-a: OOM", "severity": "warning"}],
            links=[("Open PR", "https://p")],
            choices=["Yes", "No"],
        )
        self.assertEqual(text, "*Two findings.*\n`warning` seeded-a: OOM\n<https://p|Open PR>\nReply with one of: Yes · No")

    def test_rows_are_mrkdwn(self):
        text = sp.fallback_text("h", rows=[{"text": "**seeded-a**: see [PR](https://p)"}, "plain `x`"])
        self.assertEqual(text, "*h*\n• *seeded-a*: see <https://p|PR>\n• plain `x`")

    def test_rows_cannot_mention_anyone(self):
        text = sp.fallback_text("h", rows=[{"text": "<!channel> & <@U1>", "severity": "<!here>"}])
        self.assertNotIn("<!", text)
        self.assertNotIn("<@", text)
        self.assertIn("&lt;!channel&gt; &amp; &lt;@U1&gt;", text)

    def test_a_row_link_cannot_become_a_mention(self):
        rows = [{"text": "ask [bob](@U024BE7LH) or [team](!channel) in [ops](#C1)"}, "[PR](https://p)"]
        text = sp.fallback_text("h", rows=rows)
        self.assertNotIn("<@", text)
        self.assertNotIn("<!", text)
        self.assertNotIn("<#", text)
        self.assertIn("• ask bob or team in ops", text)
        self.assertIn("<https://p|PR>", text)
        nested = [
            "[[x](@U1)](!channel)",
            "**[minor] c [[z](#a)](@U024BE7LH)**",
            "[[[y](!here)](#C1)](@U2)",
            "[[w](https://p)](!here)",
        ]
        for row in nested:
            with self.subTest(row=row):
                line = sp.fallback_text("h", rows=[row]).splitlines()[1]
                self.assertNotIn("<@", line)
                self.assertNotIn("<!", line)
                self.assertNotIn("<#", line)

    def test_a_rich_text_link_that_is_not_a_web_url_is_text(self):
        elements = sp._rich_elements("see [bob](@U1) and [PR](https://p)")
        self.assertNotIn("@U1", [e.get("url") for e in elements])
        self.assertIn({"type": "text", "text": "bob"}, elements)
        self.assertIn({"type": "link", "url": "https://p", "text": "PR"}, elements)

    def test_a_nul_in_a_row_is_dropped(self):
        self.assertEqual(sp.fallback_text("h", rows=["a \x000\x00 b"]), "*h*\n• a 0 b")

    def test_urls_keep_their_underscores_and_asterisks(self):
        cases = {
            "[docs](https://x.io/__init__/y)": "<https://x.io/__init__/y|docs>",
            "see https://x.io/a/**b**/c now": "see https://x.io/a/**b**/c now",
            "**https://x.io/a_b_**": "*https://x.io/a_b_*",
            "[**docs**](https://x.io/__a__)": "<https://x.io/__a__|*docs*>",
        }
        for row, line in cases.items():
            with self.subTest(row=row):
                self.assertEqual(sp.fallback_text("h", rows=[row]).splitlines()[1], sp.BULLET + line)

    def test_a_rich_text_link_keeps_parentheses_in_its_url(self):
        elements = sp._rich_elements("[w](https://en.wikipedia.org/wiki/Foo_(bar)) after")
        self.assertEqual(elements[0], {"type": "link", "url": "https://en.wikipedia.org/wiki/Foo_(bar)", "text": "w"})
        self.assertEqual(elements[1], {"type": "text", "text": " after"})

    def test_a_rich_text_link_that_is_not_a_safe_url_is_text(self):
        elements = sp._rich_elements("[a](https://x.io|y) [b](javascript:alert)")
        self.assertNotIn("link", [e["type"] for e in elements])
        self.assertIn({"type": "text", "text": "a"}, elements)
        self.assertIn({"type": "text", "text": "b"}, elements)

    def test_empty_rows_are_skipped(self):
        self.assertEqual(sp.fallback_text("h", rows=["", "  ", {"text": ""}, "x"]), "*h*\n• x")

    def test_labels_cannot_mention_anyone(self):
        text = sp.fallback_text("h", links=[("<!here>", "https://p")], choices=["<@U1> & <!channel>", "No"])
        self.assertNotIn("<!", text)
        self.assertNotIn("<@", text)
        self.assertIn("&lt;@U1&gt; &amp; &lt;!channel&gt;", text)


    def test_urls_cannot_mention_anyone_or_break_the_link(self):
        links = [("x", "!channel"), ("y", "@U123"), ("z", "https://a?b=>c|d"), ("ok", "https://p")]
        self.assertEqual(sp.fallback_text("h", links=links), "*h*\n<https://p|ok>")

    def test_a_url_slack_would_refuse_is_dropped(self):
        too_long = "https://x/" + "a" * sp.URL_MAX
        links = [("a", too_long), ("b", "https://p\n"), ("c", "https://"), ("d", "HTTPS://P")]
        self.assertEqual(sp.fallback_text("h", links=links), "*h*\n<HTTPS://P|d>")


class LinkAckTest(unittest.TestCase):
    def test_ack_does_nothing_else(self):
        ack = mock.AsyncMock()
        body = mock.Mock()
        asyncio.run(sp.ack_link_click(ack, body, {"action_id": "kage.link.0"}))
        ack.assert_awaited_once_with()
        self.assertEqual(body.mock_calls, [])


class BlocksReportTest(unittest.TestCase):
    ROWS = [
        {"severity": "critical", "text": "seeded-b and -c admit privileged pods"},
        {"severity": "critical", "text": "default SA is `cluster-admin` on seeded-c"},
    ]

    def _report(self, **kwargs):
        args = dict(
            headline="Security & RBAC audit: 7 findings, 2 critical.",
            note="2 are new since the last run.",
            rows=self.ROWS,
            choices=["look at: seeded-b and -c admit privileged pods"],
            links=[("Ledger issue #231 ↗", "https://l/231")],
            fold_title="all 7 findings",
            fold_rows=self.ROWS + [{"severity": "major", "text": "Workload Identity off"}],
            action_id_prefix="kage_audit",
        )
        args.update(kwargs)
        return sp.blocks_report(**args)

    def test_headline_group_buttons_then_the_fold(self):
        blocks = self._report()
        self.assertEqual(
            [b["type"] for b in blocks], ["rich_text", "divider", "rich_text", "divider", "actions", "container"]
        )
        self.assertEqual(
            blocks[0]["elements"][0]["elements"],
            [
                {"type": "text", "text": "Security & RBAC audit: 7 findings, 2 critical.", "style": {"bold": True}},
                {"type": "text", "text": " 2 are new since the last run."},
            ],
        )

    def test_group_is_headed_by_its_count_and_rows_lead_with_a_code_tag(self):
        header, first, second = self._report()[2]["elements"]
        self.assertEqual(header["elements"], [{"type": "text", "text": "2 critical", "style": {"bold": True}}])
        self.assertEqual(
            first["elements"],
            [
                {"type": "text", "text": "critical", "style": {"code": True}},
                {"type": "text", "text": " "},
                {"type": "text", "text": "seeded-b and -c admit privileged pods"},
            ],
        )
        self.assertIn({"type": "text", "text": "cluster-admin", "style": {"code": True}}, second["elements"])
        self.assertNotIn("•", str(first) + str(second))

    def test_primary_choice_first_then_the_link(self):
        buttons = self._report()[4]["elements"]
        self.assertEqual([b["action_id"] for b in buttons], ["kage_audit.choice.0", "kage_audit.link.0"])
        self.assertEqual(buttons[0]["style"], "primary")
        self.assertEqual(buttons[0]["value"], "look at: seeded-b and -c admit privileged pods")
        self.assertEqual(buttons[1]["url"], "https://l/231")
        self.assertNotIn("style", buttons[1])
        self.assertTrue(sp.CHOICE_ACTION_ID_PATTERN.search(buttons[0]["action_id"]))
        self.assertTrue(sp.LINK_ACTION_ID_PATTERN.search(buttons[1]["action_id"]))

    def test_fold_is_a_collapsed_container(self):
        fold = self._report()[-1]
        self.assertEqual(fold["title"], {"type": "plain_text", "text": "all 7 findings"})
        self.assertTrue(fold["is_collapsible"])
        self.assertTrue(fold["default_collapsed"])
        self.assertEqual(len(fold["child_blocks"][0]["elements"]), 3)

    def test_fold_left_out_when_not_in_place(self):
        self.assertNotIn("container", [b["type"] for b in self._report(fold_in_place=False)])

    def test_has_fold(self):
        self.assertTrue(sp.has_fold(self._report()))
        self.assertFalse(sp.has_fold(self._report(fold_in_place=False)))

    def test_fold_rows_are_bounded(self):
        fold = self._report(fold_rows=[{"text": "x"}] * (sp.FOLD_ROWS_MAX + 5))[-1]
        self.assertEqual(len(fold["child_blocks"][0]["elements"]), sp.FOLD_ROWS_MAX)

    def test_detail_is_a_plain_line_under_the_headline(self):
        head = self._report(detail="Security audit: 2 new across 3 clusters")[0]["elements"]
        self.assertEqual(len(head), 2)
        self.assertEqual(head[1]["elements"], [{"type": "text", "text": "Security audit: 2 new across 3 clusters"}])

    def test_empty_parts_are_omitted(self):
        self.assertEqual(
            [b["type"] for b in sp.blocks_report("h")], ["rich_text"]
        )

    def test_rows_with_no_text_and_no_severity_are_skipped(self):
        self.assertEqual(sp.blocks_report("h", rows=[""], fold_rows=["  "]), sp.blocks_report("h"))
        group = sp.blocks_report("h", rows=["", {"text": "", "severity": "major"}, "x"])[2]["elements"]
        self.assertEqual(group[0]["elements"][0]["text"], "1 major, 1 finding")
        self.assertEqual(len(group), 3)
        for section in group:
            self.assertTrue(section["elements"])

    def test_fold_first_puts_the_fold_above_the_buttons(self):
        self.assertEqual([b["type"] for b in self._report(fold_first=True)][-2:], ["container", "actions"])

    def test_a_row_is_clipped_to_row_text_max(self):
        text = self._report(rows=["x" * (sp.ROW_TEXT_MAX + 50)])[2]["elements"][1]["elements"][0]["text"]
        self.assertLessEqual(len(text), sp.ROW_TEXT_MAX)

    def test_detail_is_plained_and_clipped(self):
        detail = self._report(detail="**" + "y" * (sp.ROW_TEXT_MAX + 50) + "**")[0]["elements"][1]["elements"][0]["text"]
        self.assertLessEqual(len(detail), sp.ROW_TEXT_MAX)
        self.assertNotIn("*", detail)

    def test_a_blank_fold_title_falls_back_to_the_default(self):
        self.assertEqual(self._report(fold_title="   ")[-1]["title"]["text"], sp.DEFAULT_FOLD_TITLE)

    def test_row_links_keep_only_urls_slack_accepts(self):
        too_long = "https://x/" + "a" * sp.URL_MAX
        for url, linked in ((too_long, False), ("HTTPS://P/q", True), ("https://", False)):
            elements = sp._rich_elements(f"[d]({url})")
            self.assertEqual(elements[0]["type"] == "link", linked, url)
            self.assertEqual(f"<{url}|d>" in sp.to_mrkdwn(f"[d]({url})"), linked, url)

    def test_a_row_link_with_parentheses_in_its_url_is_whole_in_both_views(self):
        url = "https://console.cloud.google.com/logs/query;query=resource.type%3D(k8s_container)"
        self.assertEqual(sp._rich_elements(f"[logs]({url})"), [{"type": "link", "url": url, "text": "logs"}])
        self.assertEqual(sp.to_mrkdwn(f"[logs]({url})"), f"<{url}|logs>")

    def test_a_row_detail_is_a_second_line_in_both_views(self):
        row = {"severity": "critical", "text": "privileged pods", "detail": "Any pod can reach the node; **enforce** PSA."}
        section = self._report(rows=[row])[2]["elements"][1]["elements"]
        self.assertEqual(section[3], {"type": "text", "text": "\n"})
        self.assertIn({"type": "text", "text": "enforce", "style": {"bold": True}}, section)
        self.assertEqual(
            sp.fallback_text("h", rows=[row]), "*h*\n`critical` privileged pods\nAny pod can reach the node; *enforce* PSA."
        )

    def test_a_long_row_and_detail_are_clipped_alike_in_both_views(self):
        long = "x" * (sp.ROW_TEXT_MAX + 50)
        _, row_line, detail_line = sp.fallback_text("h", rows=[{"text": long, "detail": long}]).split("\n")
        self.assertEqual(row_line, sp.BULLET + sp._clip(long, sp.ROW_TEXT_MAX))
        self.assertEqual(detail_line, sp._clip(long, sp.ROW_TEXT_MAX))

    def test_group_header_counts(self):
        self.assertEqual(sp.group_header([{"severity": "critical", "text": "a"}, {"severity": "major", "text": "b"}]),
                         "1 critical, 1 major")
        self.assertEqual(sp.group_header(["a", "b"]), "2 findings")
        self.assertEqual(sp.group_header([{"severity": "High", "text": "a"}, "b"]), "1 high, 1 finding")


class SeverityRowTest(unittest.TestCase):
    def test_tag_then_text(self):
        self.assertEqual(sp.severity_row("critical", "x"), "`critical` x")
        self.assertFalse(hasattr(sp, "SEVERITY_MARKERS"))

    def test_any_severity_is_tagged_and_cannot_break_the_code_span(self):
        self.assertEqual(sp.fallback_text("h", rows=[{"text": "t", "severity": "cri`tical"}]), "*h*\n`critical` t")


if __name__ == "__main__":
    unittest.main()
