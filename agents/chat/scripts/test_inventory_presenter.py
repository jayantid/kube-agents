"""Tests for inventory_presenter and its flag gate in bootstrap_delivery."""

import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "platform" / "scripts"))

import bootstrap_delivery
import inventory_presenter

REPORT = """# GKE Environment Scan

I scanned 3 clusters and 41 workloads. Posture is mostly healthy.

1. **[critical] seeded-b and seeded-c admit privileged pods**
   Any workload can escape to the node; enforce baseline Pod Security.
2. **Default service account is cluster-admin on seeded-c**
   A compromised pod owns the cluster; remove the binding.
3. **Workload Identity is off on seeded-a (major)**
   Pods fall back to the node SA; enable it on the node pool.
4. **payments-api has no PodDisruptionBudget**
   An upgrade can take every replica down; add a PDB.

Also found: 18 more items, tracked in the findings queue — ask for the full list.

The full inventory is available — just ask.
"""

PRESENTED = """**I scanned 3 clusters and 41 workloads and found 22 things to look at.** Two are worth fixing first:

`critical` seeded-b and seeded-c admit privileged pods
Any workload can escape to the node; enforce baseline Pod Security.
**Default service account is cluster-admin on seeded-c**
A compromised pod owns the cluster; remove the binding.

The full inventory is available — just ask.
"""


class PresentTest(unittest.TestCase):
    def test_the_card_headline_top_two_and_the_closing_line(self):
        self.assertEqual(inventory_presenter.present(REPORT), PRESENTED)

    def test_the_rest_and_the_roll_up_are_left_out(self):
        out = inventory_presenter.present(REPORT)
        for absent in ("Workload Identity", "payments-api", "Also found", "18 more", "more worth a look", "mostly healthy"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, out)

    def test_two_items_have_no_lead(self):
        report = "# Scan\n\nAll quiet. One thing stands out.\n\n1. **A (minor)**\n   Fix it.\n2. **B**\n   Fix that.\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 2 things to look at:**\n\n`minor` A\nFix it.\n**B**\nFix that.\n",
        )

    def test_three_items_show_two_under_the_neutral_lead(self):
        report = "Posture.\n\n1. **A**\n   x\n2. **B**\n   y\n3. **C**\n   z\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 3 things to look at.** Start with these two:\n\n**A**\nx\n**B**\ny\n",
        )

    def test_severity_in_the_sentence_is_not_a_label(self):
        report = "Posture.\n\n1. **A**\n   This is not critical.\n"
        self.assertEqual(
            inventory_presenter.present(report), "**I found 1 thing to look at:**\n\n**A**\nThis is not critical.\n"
        )

    def test_a_partial_bold_headline_keeps_the_whole_line(self):
        report = (
            "Posture.\n\n"
            "1. **Critical:** seeded-b and seeded-c admit privileged pods.\n   Enforce baseline.\n"
            "2. **seeded-c:** the default service account is cluster-admin.\n   Remove the binding.\n"
            "3. **No PDB** on payments-api\n   Add one.\n"
        )
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 3 things to look at.** Two are worth fixing first:\n\n"
            "`critical` seeded-b and seeded-c admit privileged pods.\nEnforce baseline.\n"
            "**seeded-c: the default service account is cluster-admin.**\nRemove the binding.\n",
        )

    def test_a_bold_sentence_is_the_headline_and_the_rest_its_sentence(self):
        report = "Posture.\n\n1. **Default SA is cluster-admin on seeded-c.** A compromised pod owns it.\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 1 thing to look at:**\n\n**Default SA is cluster-admin on seeded-c.**\nA compromised pod owns it.\n",
        )

    def test_a_lazy_continuation_stays_in_its_item(self):
        report = (
            "Posture.\n\n"
            "1. **A on seeded-b**\nAny workload can escape.\n"
            "2. **B on seeded-c**\nA compromised pod owns it.\n"
            "3. **C on payments-api**\nAn upgrade takes it down.\n\n"
            "The full inventory is available.\n"
        )
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 3 things to look at.** Start with these two:\n\n**A on seeded-b**\nAny workload can escape.\n"
            "**B on seeded-c**\nA compromised pod owns it.\n\n"
            "The full inventory is available.\n",
        )

    def test_criticals_are_never_rolled_up(self):
        report = "Posture.\n\n" + "".join(f"{i}. **[critical] problem {i}**\n   x\n" for i in range(1, 4))
        report += "4. **other**\n   y\n"
        out = inventory_presenter.present(report)
        rows = "\n".join(f"`critical` problem {i}\nx" for i in range(1, 4))
        self.assertIn("**I found 4 things to look at.** Three are worth fixing first:\n\n" + rows, out)
        self.assertNotIn("other", out)
        self.assertNotIn("[critical]", out)

    def test_a_quiet_cluster_gets_the_neutral_lead(self):
        report = "No critical or major findings.\n\n"
        report += "".join(f"{i}. **item {i} (minor)**\n   x\n" for i in range(1, 4))
        out = inventory_presenter.present(report)
        self.assertIn("Start with these two:", out)
        self.assertNotIn("worth fixing", out)

    def test_a_severity_word_inside_a_name_is_not_a_label(self):
        report = "Posture.\n\n1. **major-version skew on node pool `minor-pool`**\n   Upgrade.\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 1 thing to look at:**\n\n**major-version skew on node pool `minor-pool`**\nUpgrade.\n",
        )

    def test_a_gap_is_its_own_line_under_the_bold_headline(self):
        report = "2 clusters were unreachable. Scanned 3 clusters.\n\n1. **A (major)**\n   x\n2. **B**\n   y\n3. **C**\n   z\n"
        out = inventory_presenter.present(report)
        self.assertTrue(
            out.startswith(
                "**I scanned 3 clusters and found 3 things to look at.**\n"
                "2 clusters were unreachable. Two are worth fixing first:\n\n"
            ),
            out,
        )

    def test_only_the_counts_the_posture_states_are_named(self):
        report = "Scanned 2 clusters, e.g. prod and staging. Posture is weak.\n\n1. **A**\n   x\n"
        self.assertEqual(
            inventory_presenter.present(report), "**I scanned 2 clusters and found 1 thing to look at:**\n\n**A**\nx\n"
        )

    def test_a_bold_title_line_is_dropped_like_a_heading(self):
        report = "**GKE Environment Scan**\n\nScanned 2 clusters and 41 workloads. Mostly healthy.\n\n1. **A thing**\n   Fix it.\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I scanned 2 clusters and 41 workloads and found 1 thing to look at:**\n\n**A thing**\nFix it.\n",
        )

    def test_a_bold_posture_sentence_is_not_a_title(self):
        report = "**Two problems need attention.**\n\n1. **A thing**\n   Fix it.\n"
        self.assertEqual(
            inventory_presenter.present(report), "**I found 1 thing to look at:**\n\n**A thing**\nFix it.\n"
        )

    def test_a_multi_line_sentence_is_joined_under_its_headline(self):
        report = "Posture.\n\n1. **A**\n   Pods fall back to the node SA;\n   enable it on the pool.\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 1 thing to look at:**\n\n**A**\nPods fall back to the node SA; enable it on the pool.\n",
        )

    def test_an_item_with_no_sentence_is_its_headline_alone(self):
        report = "Posture.\n\n1. **A**\n2. **B**\n   Fix it.\n"
        self.assertEqual(inventory_presenter.present(report), "**I found 2 things to look at:**\n\n**A**\n**B**\nFix it.\n")

    def test_a_closing_line_with_a_count_is_kept(self):
        report = TWO_ITEMS + (
            "Also found: 18 more items, tracked in the findings queue.\n\n"
            "The full inventory covers 41 workloads and 22 findings; ask for it.\n"
        )
        out = inventory_presenter.present(report)
        self.assertTrue(out.endswith("\n\nThe full inventory covers 41 workloads and 22 findings; ask for it.\n"), out)
        self.assertNotIn("Also found", out)
        self.assertNotIn("Ask me to see all", out)

    def test_a_roll_up_that_was_the_last_line_becomes_the_ask(self):
        for tail, total in (
            ("Reply 'list' to see the other 3 findings in the full inventory.", 5),
            ("Also found: 18 more items.", 20),
        ):
            with self.subTest(tail=tail):
                out = inventory_presenter.present(TWO_ITEMS + tail + "\n")
                self.assertIn(f"found {total} things to look at", out)
                self.assertTrue(out.endswith(f"\n\nAsk me to see all {total}.\n"), out)
                self.assertNotIn(tail, out)

    def test_unparseable_reports_are_unchanged(self):
        for report in (
            "",
            "# Report\n\n| Cluster | ... |\n",
            "# Scan\n\n1. **A**\n   x\n",  # no posture
            "Posture.\n\n1. plain item, no bold headline\n",
            "Posture.\n\n1. **A**\n   x\n\n## Another section\n",
            "Posture.\n\n1. **Critical:**\n   x\n",  # a label and nothing to label
            "Posture.\n\n1. **A**\n   x\n\nSee 2. **B** and 3. **C** too.\n",  # items inside a paragraph
        ):
            with self.subTest(report=report):
                self.assertEqual(inventory_presenter.present(report), report)



#: tm-7's probe: two listed items, the first critical, so both show.
TWO_ITEMS = """# Scan

I scanned 3 clusters and 41 workloads. Mostly healthy.

1. **critical: seeded-b admits privileged pods.** Enforce baseline.
2. **Default SA is cluster-admin on seeded-c.** Remove the binding.

"""

PROBE_ITEMS = """1. **[HIGH] No PDB on payments/api in prod**
   A node upgrade drains every replica at once; add a PodDisruptionBudget.

2. **[MEDIUM] No readinessProbe on web in dev**
   Traffic reaches pods before they are ready; add a readinessProbe.

3. **[LOW] Unpinned image tag on batch/cron in dev**
   A re-pull can change the code; pin the digest.
"""

GAP_POSTURE = (
    "2 clusters could not be scanned (permission denied); across the other 5 clusters and 41 workloads posture is fair."
)


class BlocksTest(unittest.TestCase):
    def _types(self, blocks):
        return [block["type"] for block in blocks]

    def _buttons(self, blocks):
        return [b["text"]["text"] for b in blocks[-1]["elements"]]

    def _headline(self, report):
        # (headline, note), or (headline, note, detail) when the posture names a gap.
        headline, note, detail = inventory_presenter.card_headline(inventory_presenter._shape(report))
        return (headline, note, detail) if detail else (headline, note)

    def _probe(self, posture, rollup="Also found: 19 more."):
        # tm-7's probe: three listed items under a model-plausible posture and roll-up.
        return f"# GKE Environment Scan\n\n{posture}\n\n{PROBE_ITEMS}\n{rollup}\n\nAsk me for the full inventory.\n"

    def test_mock_v3a_shape(self):
        blocks, text = inventory_presenter.blocks(REPORT)
        self.assertEqual(self._types(blocks), ["rich_text", "divider", "rich_text", "divider", "actions"])
        head = blocks[0]["elements"][0]["elements"]
        self.assertEqual(
            head[0],
            {
                "type": "text",
                "text": "I scanned 3 clusters and 41 workloads and found 22 things to look at.",
                "style": {"bold": True},
            },
        )
        self.assertEqual(head[1]["text"], " Two are worth fixing first:")
        rows = blocks[2]["elements"]
        # No count above the rows: the first section is the first finding.
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["elements"][0], {"type": "text", "text": "critical", "style": {"code": True}})
        buttons = blocks[4]["elements"]
        self.assertEqual([b["action_id"] for b in buttons], ["kage_inventory.choice.0", "kage_inventory.choice.1"])
        self.assertEqual(buttons[0]["style"], "primary")
        # Four listed plus the roll-up's "18 more".
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 22"])
        self.assertTrue(
            text.startswith(
                "*I scanned 3 clusters and 41 workloads and found 22 things to look at. Two are worth fixing first:*\n"
                "`critical` seeded-b"
            )
        )

    def test_the_total_is_the_only_count_on_the_card(self):
        blocks, _ = inventory_presenter.blocks(REPORT)
        card = str(blocks)
        for gone in ("1 critical", "more worth a look", "Also found", "18", "Posture is mostly healthy", "full inventory"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, card)
        self.assertNotIn("container", self._types(blocks))

    def test_a_posture_total_counts_only_with_no_roll_up(self):
        report = REPORT.replace("41 workloads.", "41 workloads, 23 findings.")
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 22"])
        report = report.replace("Also found: 18 more items, tracked in the findings queue — ask for the full list.\n\n", "")
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 23"])
        self.assertIn("found 23 things", self._headline(report)[0])

    def test_a_roll_ups_stated_total_is_not_added_to_its_breakdown(self):
        for rollup in (
            "Also found: 18 more items: 2 high, 16 lower-priority findings, tracked in the findings queue.",
            "Also found: 18 more items (2 high, 16 medium)",
            "Also found: 18 (2 high, 16 medium).",
            "Plus 18 lower-priority findings: 2 high, 16 medium.",
        ):
            with self.subTest(rollup=rollup):
                report = TWO_ITEMS + rollup + "\n"
                blocks, text = inventory_presenter.blocks(report)
                self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 20"])
                self.assertIn("found 20 things to look at", text)
                self.assertIn("found 20 things to look at", inventory_presenter.present(report))

    def test_a_counted_closing_line_with_no_roll_up_is_kept_and_not_counted(self):
        for closing in (
            "The full inventory covers 41 workloads and 22 findings; ask for it.",
            "Most of the 20 findings are low risk; ask for the full list.",
            "The full inventory covers 41 workloads and 22 findings; high availability is fine.",
        ):
            with self.subTest(closing=closing):
                report = TWO_ITEMS.replace("Mostly healthy.", "Mostly healthy; 20 findings.") + closing + "\n"
                blocks, _ = inventory_presenter.blocks(report)
                self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 20"])
                presented = inventory_presenter.present(report)
                self.assertIn("found 20 things to look at", presented)
                self.assertIn(closing, presented)
                self.assertNotIn("Ask me to see all", presented)

    def test_a_first_term_heading_its_breakdown_is_the_roll_ups_count(self):
        for rollup, total in (
            ("18 high-priority findings remain: 2 critical, 16 high.", 20),
            ("Also found: 18 high-priority findings: 2 critical, 16 high.", 20),
            ("There are 4 issues in 2 namespaces: 3 high, 1 low.", 6),
        ):
            with self.subTest(rollup=rollup):
                blocks, _ = inventory_presenter.blocks(TWO_ITEMS + rollup + "\n")
                self.assertEqual(self._buttons(blocks), ["Fix the first one", f"See all {total}"])

    def test_a_severity_breakdown_is_summed_even_when_its_terms_coincide(self):
        for rollup, total in (
            ("Also found: 2 high, 1 medium and 1 low finding.", 6),
            ("Also found: 3 high, 1 medium, 2 low.", 8),
            ("Also found: 18 items: 2 high, 16 low.", 20),
            ("I also found 18 findings remain: 2 high, 16 low.", 20),
            ("Also found: 2 high-priority and 5 low findings.", 9),
            ("Also found: 2 high-priority findings and 5 low.", 9),
            ("Plus 2 high-severity, 3 medium-severity and 4 low-severity findings.", 11),
        ):
            with self.subTest(rollup=rollup):
                blocks, _ = inventory_presenter.blocks(TWO_ITEMS + rollup + "\n")
                self.assertEqual(self._buttons(blocks), ["Fix the first one", f"See all {total}"])

    def test_the_roll_up_wins_over_an_earlier_counting_paragraph(self):
        report = TWO_ITEMS + "Most need attention: 6 findings touch prod.\n\nAlso found: 12 more.\n"
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 14"])
        presented = inventory_presenter.present(report)
        self.assertIn("Most need attention: 6 findings touch prod.", presented)
        self.assertNotIn("Also found: 12 more.", presented)

    def test_a_larger_roll_up_total_beats_the_posture(self):
        # "3 findings need attention now" counts the shown ones, not the 14 behind them.
        report = "Scanned 2 clusters; 3 findings need attention now.\n\n" + "".join(
            f"{i}. **[critical] problem {i}**\n   x\n" for i in range(1, 4)
        )
        report += "\nAlso found: 14 more items, tracked in the findings queue.\n"
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 17"])
        self.assertEqual(
            self._headline(report),
            ("I scanned 2 clusters and found 17 things to look at.", "Three are worth fixing first:"),
        )

    def test_a_roll_up_without_more_is_counted(self):
        report = REPORT.replace("18 more items", "14 items")
        self.assertNotEqual(report, REPORT)
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 18"])

    def test_every_row_carries_its_sentence_as_a_second_line(self):
        blocks, text = inventory_presenter.blocks(REPORT)
        rows = blocks[2]["elements"]
        self.assertEqual(rows[0]["elements"][-1]["text"], "Any workload can escape to the node; enforce baseline Pod Security.")
        self.assertIn("\nA compromised pod owns the cluster; remove the binding.", text)

    def test_everything_shown_has_no_lead_and_no_see_all(self):
        report = "# Scan\n\nI scanned 2 clusters and 9 workloads.\n\n1. **A (major)**\n   Fix it.\n2. **B**\n   Fix that.\n"
        blocks, text = inventory_presenter.blocks(report)
        self.assertEqual(self._headline(report), ("I scanned 2 clusters and 9 workloads and found 2 things to look at:", ""))
        self.assertEqual(self._buttons(blocks), ["Fix the first one"])
        self.assertTrue(text.startswith("*I scanned 2 clusters and 9 workloads and found 2 things to look at:*\n"))

    def test_one_of_each_is_singular(self):
        report = "I scanned 1 cluster and 1 workload.\n\n1. **A (major)**\n   Fix it.\n"
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._headline(report), ("I scanned 1 cluster and 1 workload and found 1 thing to look at:", ""))
        self.assertEqual(self._buttons(blocks), ["Fix it"])

    def test_one_top_row_with_more_behind_it(self):
        report = "I scanned 4 GKE clusters.\n\n1. **A (critical)**\n   Fix it.\n\nAlso found: 6 more items.\n"
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(
            self._headline(report), ("I scanned 4 clusters and found 7 things to look at.", "One is worth fixing first:")
        )
        self.assertEqual(self._buttons(blocks), ["Fix it", "See all 7"])

    def test_a_quiet_cluster_gets_the_neutral_lead(self):
        report = "I scanned three clusters and 12 workloads. All quiet.\n\n1. **A (minor)**\n   x\n2. **B**\n   y\n"
        report += "\nAlso found: 3 more items.\n"
        self.assertEqual(
            self._headline(report),
            ("I scanned 3 clusters and 12 workloads and found 5 things to look at.", "Start with these two:"),
        )

    def test_a_posture_with_no_counts_says_only_what_was_found(self):
        report = "Posture.\n\n1. **A (major)**\n   x\n2. **B**\n   y\n3. **C**\n   z\n"
        self.assertEqual(self._headline(report), ("I found 3 things to look at.", "Two are worth fixing first:"))

    def test_the_primary_button_names_the_first_finding_in_its_value(self):
        # The label is what the card shows; the value is the turn a session with no thread context reads.
        blocks, _ = inventory_presenter.blocks(REPORT)
        primary = blocks[-1]["elements"][0]
        self.assertEqual(
            (primary["text"]["text"], primary["value"]),
            ("Fix the first one", "Fix the first one: seeded-b and seeded-c admit privileged pods"),
        )
        self.assertIn("seeded-b and seeded-c admit privileged pods", str(blocks[2]))

    def test_a_single_row_names_it_under_fix_it(self):
        report = "# Scan\n\nI scanned 1 cluster.\n\n1. **`kube-system` has no **NetworkPolicy****\n   Add one.\n"
        blocks, _ = inventory_presenter.blocks(report)
        primary = blocks[-1]["elements"][0]
        self.assertEqual((primary["text"]["text"], primary["value"]), ("Fix it", "Fix it: kube-system has no NetworkPolicy"))

    def test_the_value_names_the_row_as_the_card_shows_it(self):
        report = "# Scan\n\nI scanned 1 cluster.\n\n1. **On *seeded-b*, _privileged_ ~pods~ run**\n   Fix.\n"
        blocks, _ = inventory_presenter.blocks(report)
        value = blocks[-1]["elements"][0]["value"]
        self.assertEqual(value, "Fix it: On seeded-b, privileged pods run")
        shown = ["".join(e["text"] for e in section["elements"]) for section in blocks[2]["elements"]]
        self.assertTrue(any(value.removeprefix("Fix it: ") in line for line in shown), shown)

    def test_the_value_is_the_first_row_exactly_as_the_card_shows_it(self):
        long_title = "seeded-b admits privileged pods " + "word " * 70
        cases = {
            "severity": REPORT,
            "markup": "Scan.\n\n1. **`kube-system` on *seeded-b* has ~no~ [policy](https://example.com/p) (major)**\n   x\n"
            "2. **B**\n   y\n",
            "clipped": f"Scan.\n\n1. **{long_title}(critical)**\n   x\n2. **B**\n   y\n",
            # Clipped as markdown, so the link's URL counts against the clip; clipping
            # the plain text instead leaves more of the title than the card shows.
            "clipped link": f"Scan.\n\n1. **[seeded-b](https://example.com/{'p' * 60}) admits {long_title}**\n   x\n"
            "2. **B**\n   y\n",
        }
        for name, report in cases.items():
            with self.subTest(name):
                blocks, _ = inventory_presenter.blocks(report)
                primary = blocks[-1]["elements"][0]
                elements = blocks[2]["elements"][0]["elements"]
                if elements[0].get("style") == {"code": True} and elements[1] == {"type": "text", "text": " "}:
                    elements = elements[2:]
                first_line = "".join(e["text"] for e in elements).split("\n", 1)[0]
                self.assertEqual(primary["value"], f"{primary['text']['text']}: {first_line}")
                if name.startswith("clipped"):
                    self.assertTrue(first_line.endswith("…"), first_line)

    def test_a_gap_the_posture_names_is_its_own_line_under_the_headline(self):
        report = self._probe(GAP_POSTURE)
        self.assertEqual(
            self._headline(report),
            (
                "I scanned 5 clusters and 41 workloads and found 22 things to look at.",
                "",
                "2 clusters could not be scanned (permission denied). Start with these two:",
            ),
        )
        blocks, text = inventory_presenter.blocks(report)
        self.assertIn("2 clusters could not be scanned (permission denied).", str(blocks[0]))
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 22"])
        self.assertIn("2 clusters could not be scanned (permission denied).", text)

    def test_a_clause_saying_nothing_was_missed_is_not_a_gap(self):
        for posture in (
            "I scanned 3 clusters and 41 workloads; no clusters were unreachable.",
            "I scanned 3 clusters and 41 workloads; 0 clusters unreachable.",
            "I scanned 3 clusters and 41 workloads; none were skipped.",
            "I scanned 3 clusters with no unreachable nodes and 41 workloads.",
            "I scanned 3 clusters and 41 workloads; there were 0 clusters that were unreachable.",
            "I scanned 3 clusters and 41 workloads; not a single cluster was unreachable.",
        ):
            with self.subTest(posture=posture):
                self.assertEqual(
                    self._headline(self._probe(posture)),
                    ("I scanned 3 clusters and 41 workloads and found 22 things to look at.", "Start with these two:"),
                )

    def test_a_gap_after_a_clean_part_of_its_clause_is_kept(self):
        for posture, gap in (
            ("No drift, but 2 clusters could not be scanned (permission denied).", "2 clusters could not be scanned (permission denied)."),
            ("I scanned 3 clusters; 2 clusters with no credentials could not be scanned.", "2 clusters with no credentials could not be scanned."),
        ):
            with self.subTest(posture=posture):
                self.assertEqual(self._headline(self._probe(posture))[2], f"{gap} Start with these two:")

    def test_every_gap_is_kept_with_the_names_its_colon_lists(self):
        for posture, gap in (
            ("I scanned 3 clusters, but seeded-d was unreachable, and seeded-e was skipped.", "seeded-d was unreachable, seeded-e was skipped."),
            ("I scanned 3 clusters (1 skipped, 2 unreachable).", "1 skipped, 2 unreachable."),
            ("I scanned 3 clusters (2 new and 1 skipped).", "1 skipped."),
            ("I scanned 3 clusters (2 new but 1 unreachable).", "1 unreachable."),
            ("I scanned 3 clusters; skipped: seeded-d (no credentials).", "Skipped: seeded-d (no credentials)."),
            ("I scanned 3 clusters; 2 clusters could not be scanned: seeded-d, seeded-e.", "2 clusters could not be scanned: seeded-d, seeded-e."),
        ):
            with self.subTest(posture=posture):
                self.assertEqual(
                    self._headline(self._probe(posture)),
                    ("I scanned 3 clusters and found 22 things to look at.", "", f"{gap} Start with these two:"),
                )

    def test_a_sentence_opening_on_a_number_is_a_clause_of_its_own(self):
        # Its count is not the scan's: only the clause with the scan verb is counted.
        report = self._probe("I scanned 3 clusters. 41 workloads run as root.")
        self.assertEqual(self._headline(report)[0], "I scanned 3 clusters and found 22 things to look at.")

    def test_a_gap_sharing_a_sentence_with_the_scan_leaves_the_counts(self):
        for posture, gap in (
            ("I scanned 3 clusters and 41 workloads, but 2 clusters could not be scanned.", "2 clusters could not be scanned."),
            ("I scanned 41 workloads across 3 clusters. 1 of 4 clusters was not scanned.", "1 of 4 clusters was not scanned."),
            ("I scanned 3 clusters and 41 workloads (seeded-d unreachable, 5 namespaces denied).", "seeded-d unreachable."),
        ):
            with self.subTest(posture=posture):
                self.assertEqual(
                    self._headline(self._probe(posture)),
                    (
                        "I scanned 3 clusters and 41 workloads and found 22 things to look at.",
                        "",
                        f"{gap} Start with these two:",
                    ),
                )

    def test_a_version_number_is_not_a_cluster_count(self):
        report = self._probe("I scanned two GKE 1.30 clusters and 41 workloads.")
        self.assertEqual(self._headline(report)[0], "I scanned 41 workloads and found 22 things to look at.")

    def test_a_count_outside_a_scan_clause_is_not_named(self):
        posture = "One cluster is past end of support; the fleet of 4 clusters and 60 workloads is otherwise healthy."
        self.assertEqual(self._headline(self._probe(posture))[0], "I found 22 things to look at.")

    def test_scan_clauses_that_disagree_name_no_count(self):
        posture = "I scanned 3 clusters and 41 workloads. I reviewed 2 clusters in depth."
        self.assertEqual(self._headline(self._probe(posture))[0], "I scanned 41 workloads and found 22 things to look at.")

    def test_a_roll_up_split_by_severity_is_summed(self):
        report = self._probe("I scanned 3 clusters and 41 workloads.", "Also found: 2 high, 5 medium and 12 low findings.")
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 22"])
        self.assertNotIn("Also found", str(blocks))

    def test_a_roll_up_in_other_words_is_counted(self):
        report = self._probe("I scanned 3 clusters and 41 workloads.", "Plus 19 lower-priority findings in the full inventory.")
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 22"])

    def test_a_scope_count_in_a_roll_up_is_not_its_count(self):
        for rollup, total in (
            ("Also found 3 namespaces with 19 issues.", 22),
            ("Also found: 2 clusters with 19 lower-priority findings.", 22),
            ("Plus 3 namespaces carry 19 more issues.", 22),
            ("19 issues in 2 namespaces: x, y.", 22),
        ):
            with self.subTest(rollup=rollup):
                blocks, _ = inventory_presenter.blocks(self._probe("I scanned 3 clusters.", rollup))
                self.assertEqual(self._buttons(blocks)[-1], f"See all {total}")

    def test_a_roll_up_counts_whatever_it_calls_its_findings(self):
        for rollup in (
            "Also found 19 more.",
            "Also found 19 warnings.",
            "Also found 19 misconfigured workloads.",
            "Also found 19 best-practice gaps in dev.",
            "Also found 19 lower priority best-practice issues.",
            "Also found 19 workloads without resource limits in 3 namespaces.",
            "Also found 19 misconfigured workloads across 3 clusters.",
            "Also found 19 workload misconfigurations across 3 clusters.",
        ):
            with self.subTest(rollup=rollup):
                report = self._probe("I scanned 3 clusters.", rollup)
                self.assertEqual(self._headline(report)[0], "I scanned 3 clusters and found 22 things to look at.")
                self.assertEqual(self._buttons(inventory_presenter.blocks(report)[0])[-1], "See all 22")

    def test_a_closing_total_counts_and_all_is_not_more(self):
        report = self._probe("I scanned 3 clusters.", "Ask to see all 22 findings.")
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 22"])

    def test_a_decimal_is_not_a_posture_total(self):
        report = self._probe("I scanned 3 clusters running 1.30 and 4.5 findings per cluster on average.", "")
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 3"])

    def test_unparseable_is_none(self):
        self.assertIsNone(inventory_presenter.blocks("# Report\n\n| Cluster | ... |\n"))

class OriginPlatformTest(unittest.TestCase):
    def _with_jobs(self, get_job):
        cron = types.ModuleType("cron")
        jobs = types.ModuleType("cron.jobs")
        jobs.get_job = get_job
        return mock.patch.dict(sys.modules, {"cron": cron, "cron.jobs": jobs})

    def test_reads_the_bound_platform(self):
        with self._with_jobs(lambda _id: {"origin": {"platform": "slack", "chat_id": "C1"}}):
            self.assertEqual(bootstrap_delivery._origin_platform(), "slack")

    def test_a_missing_job_or_origin_is_none(self):
        for job in (None, {}, {"origin": None}):
            with self.subTest(job=job), self._with_jobs(lambda _id, job=job: job):
                self.assertIsNone(bootstrap_delivery._origin_platform())

    def test_a_get_job_error_is_none(self):
        def boom(_id):
            raise OSError("jobs.json unreadable")

        with self._with_jobs(boom), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(bootstrap_delivery._origin_platform())

    def test_no_cron_module_is_none(self):
        with mock.patch.dict(sys.modules, {"cron": None, "cron.jobs": None}), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(bootstrap_delivery._origin_platform())


class DeliveryFlagTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        (self.d / "INVENTORY.md").write_text(REPORT, encoding="utf-8")
        (self.d / ".user_aligned").touch()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, flag, platform="slack", origin=None):
        env = {k: v for k, v in os.environ.items() if k != "KAGE_SLACK_UX"}
        if flag is not None:
            env["KAGE_SLACK_UX"] = flag
        buf = io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(bootstrap_delivery, "_origin_platform", origin or (lambda: platform)),
            contextlib.redirect_stdout(buf),
        ):
            rc = bootstrap_delivery.main(self.d)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_flag_unset_delivers_verbatim(self):
        self.assertEqual(self._run(None), REPORT)

    def test_flag_off_delivers_verbatim(self):
        self.assertEqual(self._run("0"), REPORT)

    def test_flag_on_delivers_presented_and_archives_original(self):
        self.assertEqual(self._run("1"), PRESENTED)
        self.assertEqual((self.d / "INVENTORY.delivered.md").read_text(encoding="utf-8"), REPORT)

    def test_flag_on_without_a_relay_prints_the_text(self):
        # A Slack origin with a chat id, so only the missing relay stops the blocks.
        import slack_blocks_post

        origin = {"platform": "slack", "chat_id": "C1"}
        with (
            mock.patch.object(bootstrap_delivery, "_origin", lambda: dict(origin)),
            mock.patch.object(slack_blocks_post, "post") as poster,
        ):
            self.assertEqual(self._run("1"), PRESENTED)
        poster.assert_not_called()

    def test_google_chat_is_verbatim_with_the_flag_on(self):
        self.assertEqual(self._run("1", platform="google_chat"), REPORT)

    def test_a_missing_origin_is_verbatim_with_the_flag_on(self):
        self.assertEqual(self._run("1", platform=None), REPORT)

    def test_presenter_failure_delivers_verbatim(self):
        boom = mock.patch.object(inventory_presenter, "present", side_effect=ValueError("boom"))
        with boom, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self._run("1"), REPORT)



class BlocksDeliveryTest(unittest.TestCase):
    ORIGIN = {"platform": "slack", "chat_id": "C1", "thread_id": "1.5"}

    def setUp(self):
        import slack_blocks_post

        self.sbp = slack_blocks_post
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        (self.d / "INVENTORY.md").write_text(REPORT, encoding="utf-8")
        (self.d / ".user_aligned").touch()

    def _run(self, post, flag="1", origin=ORIGIN):
        env = {k: v for k, v in os.environ.items() if k != "KAGE_SLACK_UX"}
        env.update({"KAGE_SLACK_UX": flag, "SLACK_RELAY_URL": "http://127.0.0.1:8765"})
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(bootstrap_delivery, "_origin", lambda: dict(origin)),
            mock.patch.object(self.sbp, "post", side_effect=post) as poster,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(bootstrap_delivery.main(self.d), 0)
        self.posts = poster.call_args_list
        return out.getvalue()

    def test_posts_blocks_into_the_origin_and_prints_nothing(self):
        self.assertEqual(self._run(lambda *a: "9.9"), "")
        (post,) = self.posts
        channel, text, blocks, thread_ts = post.args
        self.assertEqual((channel, thread_ts), ("C1", "1.5"))
        self.assertEqual(blocks[-1]["elements"][0]["text"]["text"], "Fix the first one")
        self.assertTrue(text.startswith("*I scanned 3 clusters and 41 workloads and found 22 things to look at."))
        self.assertTrue((self.d / ".bootstrap_completed").exists())
        self.assertEqual((self.d / "INVENTORY.delivered.md").read_text(encoding="utf-8"), REPORT)

    def test_flag_off_is_byte_identical(self):
        self.assertEqual(self._run(lambda *a: "9.9", flag="0"), REPORT)
        self.assertEqual(self.posts, [])

    def test_a_refusal_that_is_not_about_blocks_is_not_retried(self):
        self.assertEqual(self._run(self.sbp.Refused("channel_not_found")), PRESENTED)
        self.assertEqual(len(self.posts), 1)

    def test_refused_blocks_are_not_retried(self):
        # The card has no fold to leave out, so the same blocks would be refused again.
        self.assertEqual(self._run(self.sbp.Refused("invalid_blocks")), PRESENTED)
        self.assertEqual(len(self.posts), 1)

    def test_a_relay_failure_prints_the_text(self):
        self.assertEqual(self._run(OSError("connection refused")), PRESENTED)
        self.assertEqual(len(self.posts), 1)

    def test_a_post_that_may_have_landed_still_prints_the_text(self):
        # The first inventory is sent once, so a second copy beats none.
        self.assertEqual(self._run(TimeoutError("timed out")), PRESENTED)
        self.assertEqual(len(self.posts), 1)

    def test_no_chat_id_prints_the_text(self):
        self.assertEqual(self._run(lambda *a: "9.9", origin={"platform": "slack"}), PRESENTED)
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()
