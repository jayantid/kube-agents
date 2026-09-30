"""Tests for slack_moments, the opened-PR and needs-you layout."""

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_moments as m
import slack_presenter as p

PR = "https://github.com/acme/fleet-config/pull/412"


def _buttons(blocks):
    return [e for b in blocks if b["type"] == "actions" for e in b["elements"]]


def _contexts(blocks):
    return [b["elements"][0]["text"] for b in blocks if b["type"] == "context"]


class StandaloneTest(unittest.TestCase):
    def test_imports_nothing_from_the_gateway_or_slack(self):
        tree = ast.parse(Path(m.__file__).read_text())
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertEqual(roots - {"__future__", "re", "collections", "slack_presenter"}, set())


class OpenedPrTest(unittest.TestCase):
    def test_finds_a_pr_on_a_line_that_says_it_was_opened(self):
        text = f"Checked the limits.\nOpened {PR}/files for review."
        self.assertEqual(m.opened_pr(text), (PR, "fleet-config", "412", f"Opened {PR}/files for review."))

    def test_each_verb_counts_and_case_does_not(self):
        for verb in ("opened", "Created", "RAISED", "filed", "submitted"):
            self.assertIsNotNone(m.opened_pr(f"{verb} {PR}"), verb)

    def test_a_cited_pr_is_not_announced(self):
        self.assertIsNone(m.opened_pr(f"{PR} already covers this"))
        self.assertIsNone(m.opened_pr(f"See {PR}; reopening is not needed"))

    def test_the_verb_must_be_on_the_line_with_the_url(self):
        self.assertIsNone(m.opened_pr(f"Opened the file.\n{PR}"))

    def test_the_verb_must_come_before_the_url(self):
        self.assertIsNone(m.opened_pr(f"regression came from {PR}, opened by bob last week"))
        self.assertIsNone(m.opened_pr(f"Opened the runbook; {PR} explains it"))

    def test_a_pr_or_pull_request_between_the_verb_and_the_url(self):
        for lead in ("Opened PR", "created a pull request:", "Raised a new PR", "filed the PR"):
            self.assertEqual(m.opened_pr(f"{lead} {PR}")[0], PR, lead)
        self.assertEqual(m.opened_pr(f"Opened <{PR}|PR #412>")[0], PR)

    def test_the_opened_pr_is_found_after_a_cited_one(self):
        other = "https://github.com/acme/x/pull/300"
        self.assertEqual(m.opened_pr(f"Following up {other}, opened {PR}")[0], PR)

    def test_an_issue_url_is_not_a_pr(self):
        self.assertIsNone(m.opened_pr("Opened https://github.com/acme/fleet-config/issues/9"))

    def test_empty_and_none(self):
        self.assertIsNone(m.opened_pr(""))
        self.assertIsNone(m.opened_pr(None))


class PrOpenedTest(unittest.TestCase):
    def test_headline_evidence_and_two_url_buttons(self):
        blocks, text = m.pr_opened(PR, "fleet-config", "412", f"Opened PR {PR} raising the limit")
        self.assertEqual(blocks[0]["text"]["text"], "*I opened PR #412 in fleet-config. It's yours to review.*")
        self.assertEqual(_contexts(blocks), ["Opened PR #412 raising the limit"])
        buttons = _buttons(blocks)
        self.assertEqual([b["text"]["text"] for b in buttons], ["Open PR ↗", "Files changed ↗"])
        self.assertEqual([b["url"] for b in buttons], [PR, PR + "/files"])
        self.assertTrue(all(p.LINK_ACTION_ID_PATTERN.search(b["action_id"]) for b in buttons))
        self.assertTrue(all(b["action_id"].startswith("kage_pr.") for b in buttons))
        self.assertEqual(text.split("\n")[1], "Opened PR #412 raising the limit")
        self.assertIn(PR + "/files", text)

    def test_the_workers_line_cannot_mention_anyone(self):
        blocks, text = m.pr_opened(PR, "fleet-config", "412", f"Opened {PR} <!channel>")
        self.assertEqual(_contexts(blocks), ["Opened PR #412 &lt;!channel&gt;"])
        self.assertNotIn("<!channel>", text)


class NeedsYouTest(unittest.TestCase):
    def test_listed_options_become_choice_buttons(self):
        reason = "Which checkout-gateway did you mean?\nTwo clusters run one.\n- seeded-reliability\n2) seeded-debug"
        blocks, text = m.needs_you(reason)
        self.assertEqual(blocks[0]["text"]["text"], "*Which checkout-gateway did you mean?*")
        self.assertEqual(_contexts(blocks), ["Two clusters run one.", m.WAITING])
        buttons = _buttons(blocks)
        self.assertEqual([b["text"]["text"] for b in buttons], ["seeded-reliability", "seeded-debug"])
        self.assertTrue(all(p.CHOICE_ACTION_ID_PATTERN.search(b["action_id"]) for b in buttons))
        self.assertEqual(blocks[-1]["block_id"], p.WAITING_BLOCK_ID)
        self.assertEqual(text.split("\n")[:2], ["*Which checkout-gateway did you mean?*", "Two clusters run one."])
        self.assertIn("seeded-debug", text)

    def test_no_list_is_text_only(self):
        blocks, text = m.needs_you("Which namespace should I scale?")
        self.assertEqual(_buttons(blocks), [])
        self.assertEqual(_contexts(blocks), [m.WAITING])
        self.assertEqual(text, "*Which namespace should I scale?*")

    def test_no_buttons_keeps_the_options_in_the_text(self):
        reason = "Which checkout-gateway did you mean?\n- seeded-reliability\n- seeded-debug"
        blocks, text = m.needs_you(reason, buttons=False)
        self.assertEqual(_buttons(blocks), [])
        self.assertEqual(_contexts(blocks), ["- seeded-reliability\n- seeded-debug", m.WAITING])
        self.assertIn("seeded-debug", text)

    def test_one_option_or_too_many_stay_in_the_text(self):
        for count in (1, p.BUTTONS_PER_ROW + 1):
            options = [f"- option {n}" for n in range(count)]
            blocks, _ = m.needs_you("\n".join(["Pick one", *options]))
            self.assertEqual(_buttons(blocks), [], count)
            self.assertIn("option 0", _contexts(blocks)[0])

    def test_an_option_too_long_for_a_button_keeps_all_of_them_in_the_text(self):
        long = "x" * (p.BUTTON_TEXT_MAX + 1)
        blocks, _ = m.needs_you(f"Pick one\n- short\n- {long}")
        self.assertEqual(_buttons(blocks), [])
        self.assertIn(long, _contexts(blocks)[0])

    def test_a_long_first_line_is_repeated_whole_below_the_clipped_headline(self):
        first = "why " * 60
        blocks, _ = m.needs_you(first)
        self.assertLessEqual(len(blocks[0]["text"]["text"]), p.HEADLINE_MAX + 2)
        self.assertEqual(_contexts(blocks)[0], first.strip())

    def test_detail_is_clipped(self):
        blocks, _ = m.needs_you("Question\n" + "y" * (m.DETAIL_MAX * 2))
        detail = _contexts(blocks)[0]
        self.assertEqual(len(detail), m.DETAIL_MAX)
        self.assertTrue(detail.endswith(p.ELLIPSIS))

    def test_the_reason_cannot_mention_anyone(self):
        blocks, _ = m.needs_you("Question\n<@U123> said so")
        self.assertEqual(_contexts(blocks)[0], "&lt;@U123&gt; said so")

    def test_an_empty_reason_is_no_question(self):
        self.assertIsNone(m.needs_you(""))
        self.assertIsNone(m.needs_you("  \n "))

    def test_settled_drops_the_choices_and_the_waiting_line_only(self):
        blocks, _ = m.needs_you("Which cluster?\nTwo run it.\n- seeded-a\n- seeded-b")
        settled = m.needs_you_settled(blocks)
        self.assertEqual(_buttons(settled), [])
        self.assertEqual(settled, [b for b in blocks if b["type"] != "actions"][:-1])
        self.assertEqual(_contexts(settled), ["Two run it."])

    def test_settled_keeps_a_link_beside_the_choices(self):
        link = {"type": "button", "action_id": "kage.link.0", "url": "https://example.com"}
        choice = {"type": "button", "action_id": "kage_needs.choice.0"}
        settled = m.needs_you_settled([{"type": "actions", "elements": [choice, link]}])
        self.assertEqual(settled, [{"type": "actions", "elements": [link]}])


if __name__ == "__main__":
    unittest.main()
