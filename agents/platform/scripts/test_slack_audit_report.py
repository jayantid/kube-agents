"""Unit tests for slack_audit_report — the fleet-audit headline for Slack.

Run: python3 -m pytest agents/platform/scripts/test_slack_audit_report.py
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_audit_report as sar

LEDGER = "https://github.com/acme/fleet-config/issues/231"
REF = sar.LedgerRef(LEDGER, "acme/fleet-config", 231)

#: The longest a pathological input may take; the backtracking it guards against took minutes.
FAST_SECONDS = 1.0
LONG_INPUT = 20000

#: The SOP's relayed line (compliance_audit_sop.md), with its trailing ledger URL.
REPORT = f"Security & RBAC posture audit: 2 new, 1 resolved across 3 clusters — {LEDGER}"


def finding(title, fid):
    return f'<a id="finding-{fid}"></a>\n\n#### {title} <!-- finding:{fid} -->\n\n- **Where:** `seeded-a` — `x`\n'


BODY = (
    "Summary line.\n\n"
    "### Critical (2)\n\n"
    + finding("seeded-b, seeded-c: `cluster-admin` bound to the default service account", "rbac-1")
    + finding("seeded-c: a ClusterRole grants `*` on secrets", "rbac-2")
    + "\n### Major (1)\n\n"
    + finding("seeded-a: Workload Identity is off on one node pool", "wi-1")
    + "\n### Minor (4)\n\n"
    + finding("seeded-a: a namespace has no NetworkPolicy", "np-1")
    + "\n### Skipped\n\n"
    + finding("not a finding", "skip-1")
)
ISSUE = {
    "title": "[audit] Security & RBAC Posture Audit — 7 findings (2 critical)",
    "body": BODY,
    "state": "open",
    "labels": ["agent:audit", "severity:critical"],
}
LINE = "Security & RBAC posture audit: 2 new, 1 resolved across 3 clusters"


class LedgerRefTest(unittest.TestCase):
    def test_the_sop_line_names_its_ledger(self):
        self.assertEqual(sar.ledger_ref(REPORT), REF)

    def test_every_sop_shape(self):
        for line in (
            (
                f"AI Workload Security Audit: 1 critical, 3 major, 2 minor across 2 of 7 clusters "
                f"(2 new, 1 resolved, 1 remediation PR opened) — {LEDGER}"
            ),
            f"Upgrade & patch readiness: 3 new findings (1 critical), 2 resolved, across 11 clusters — {LEDGER}",
            f"Workload Reliability Audit: 2 critical — {LEDGER}.",
            f"Cost audit: 2 new – <{LEDGER}>",
            f"Cost audit: 2 new — [#231]({LEDGER})",
            f"Cost audit: 2 new - {LEDGER}\n",
            f"Security audit: 2 new across 3 of 5 clusters. [Ledger issue #231]({LEDGER})",
            f"## Security audit\n\n- a finding\n\nLedger: {LEDGER}\n",
        ):
            with self.subTest(line=line):
                self.assertEqual(sar.ledger_ref(line), REF)

    def test_repro_b_a_finding_url_is_not_the_ledger(self):
        report = (
            "- **[critical] seeded-a** — affected by https://github.com/kubernetes/kubernetes/issues/124000\n"
            f"Ledger: {LEDGER}\n"
        )
        self.assertEqual(sar.ledger_ref(report), REF)

    def test_a_url_that_does_not_end_the_report_is_not_the_ledger(self):
        self.assertIsNone(sar.ledger_ref(f"See {LEDGER} for the details of this."))

    def test_a_url_without_a_dash_or_label_is_not_the_ledger(self):
        self.assertIsNone(sar.ledger_ref(f"affected by {LEDGER}"))

    def test_a_pull_request_is_not_the_ledger(self):
        self.assertIsNone(sar.ledger_ref(REPORT.replace("/issues/", "/pull/")))

    def test_a_link_label_without_ledger_is_not_the_ledger(self):
        self.assertIsNone(sar.ledger_ref(f"Security audit: 2 new. [see this]({LEDGER})"))

    def test_bracket_runs_do_not_backtrack(self):
        for text in ("[ledger " * (LONG_INPUT // 8), "[" + "ledger " * (LONG_INPUT // 7), "— " * LONG_INPUT):
            with self.subTest(text=text[:16]):
                start = time.monotonic()
                self.assertIsNone(sar.ledger_ref(text))
                sar.needs_fold(text, "")
                self.assertLess(time.monotonic() - start, FAST_SECONDS)
        start = time.monotonic()
        self.assertIsNone(sar.ledger_ref("[" * LONG_INPUT))
        self.assertEqual(sar._row_text("[" * LONG_INPUT), "[" * LONG_INPUT)
        self.assertLess(time.monotonic() - start, FAST_SECONDS)

    def test_a_long_report_still_names_its_ledger(self):
        self.assertEqual(sar.ledger_ref("[ledger " * (LONG_INPUT // 8) + "\n" + REPORT), REF)

    def test_a_dot_segment_repository_is_not_the_ledger(self):
        for repo in ("../..", "./x", "acme/.."):
            with self.subTest(repo=repo):
                self.assertIsNone(sar.ledger_ref(f"done — https://github.com/{repo}/issues/1"))

    def test_an_overlong_issue_number_is_not_the_ledger(self):
        self.assertIsNone(sar.ledger_ref("done — https://github.com/o/r/issues/" + "9" * 5000))


class HeadlineFromIssueTest(unittest.TestCase):
    def test_mock_08_layout(self):
        self.assertEqual(
            sar.headline_from_issue(ISSUE, REF, REPORT),
            "**Security & RBAC Posture audit: 7 findings, 2 critical.** 2 are new since the last run.\n"
            f"{LINE}\n"
            "`critical` seeded-b, seeded-c: `cluster-admin` bound to the default service account\n"
            "`critical` seeded-c: a ClusterRole grants `*` on secrets\n"
            f"[Ledger issue #231 ↗]({LEDGER}): all 7 findings",
        )

    def test_rows_follow_the_severity_sections(self):
        body = BODY.replace("### Critical (2)", "### Nothing").replace(
            "### Minor (4)", "### Minor (4)\n\n" + finding("minor one", "m-0")
        )
        rows = sar.headline_from_issue(dict(ISSUE, body=body), REF, REPORT).splitlines()[2:4]
        self.assertEqual(rows[0], "`major` seeded-a: Workload Identity is off on one node pool")
        self.assertTrue(rows[1].startswith("`minor` minor one"))

    def test_no_new_count_leaves_the_headline_bare(self):
        first = sar.headline_from_issue(ISSUE, REF, "").splitlines()[0]
        self.assertEqual(first, "**Security & RBAC Posture audit: 7 findings, 2 critical.**")

    def test_one_new_is_singular(self):
        self.assertIn(" 1 is new since the last run.", sar.headline_from_issue(ISSUE, REF, "1 new — x"))

    def test_singular_title(self):
        issue = dict(ISSUE, title="[audit] Cost Audit — 1 finding (0 critical)", body="### Major (1)\n\n" + finding("idle", "c"))
        headline = sar.headline_from_issue(issue, REF).splitlines()
        self.assertEqual(headline[0], "**Cost audit: 1 finding, 0 critical.**")
        self.assertEqual(headline[-1], f"[Ledger issue #231 ↗]({LEDGER}): 1 finding")

    def test_a_closed_ledger_does_not_parse(self):
        # A clean run closes the ledger over its old title: 7 findings is the last run's count.
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, state="closed"), REF, REPORT))
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, state=""), REF, REPORT))

    def test_an_issue_without_the_ledger_label_does_not_parse(self):
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, labels=["bug"]), REF, REPORT))
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, labels=None), REF, REPORT))

    def test_labels_as_names_or_dicts(self):
        for labels in (["agent:audit"], [{"name": "agent:audit"}], ("agent:audit",)):
            with self.subTest(labels=labels):
                self.assertIsNotNone(sar.headline_from_issue(dict(ISSUE, labels=labels), REF, REPORT))

    def test_labels_of_any_other_shape_are_not_the_ledger(self):
        for labels in ("agent:audit-retired", "agent:audit", {"agent:audit": 1}, [{"label": "agent:audit"}], 7):
            with self.subTest(labels=labels):
                self.assertIsNone(sar.headline_from_issue(dict(ISSUE, labels=labels), REF, REPORT))

    def test_overlong_counts_do_not_parse(self):
        big = "9" * 5000
        for report in (f"{big} new findings — {LEDGER}", f"{big} resolved — {LEDGER}", f"{big} findings — {LEDGER}"):
            with self.subTest(report=report[-40:]):
                self.assertIsNone(sar.headline_from_issue(ISSUE, REF, report))
        title = f"[audit] Security & RBAC Posture Audit — {big} findings (2 critical)"
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, title=title), REF, REPORT))

    def test_the_relayed_line_cannot_post_a_link_or_a_mention(self):
        report = f"Security audit: 3 new <!channel> see <https://evil.example|Ledger issue #7 ↗> [x](https://evil.example) — {LEDGER}"
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 7 findings (2 critical)")
        texts = [
            sar.headline_from_issue(issue, REF, report),
            str(sar.blocks_from_issue(issue, REF, report)),
            sar.headline_fallback(report, REF),
        ]
        for text in texts:
            self.assertNotIn("evil.example", text)
            self.assertNotIn("<!", text)
        self.assertIn("3 new !channel see Ledger issue #7 ↗ x", texts[0])

    def test_the_state_is_read_in_any_case(self):
        self.assertIsNotNone(sar.headline_from_issue(dict(ISSUE, state="OPEN"), REF, REPORT))

    def test_a_zero_finding_title_does_not_parse(self):
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 0 findings (0 critical)", body="All clear.")
        self.assertIsNone(sar.headline_from_issue(issue, REF, REPORT))

    def test_the_relayed_line_keeps_coverage_resolved_and_prs(self):
        line = (
            "AI Workload Security Audit: 1 critical, 3 major, 2 minor across 2 of 7 clusters "
            "(2 new, 1 resolved, 1 remediation PR opened)"
        )
        lines = sar.headline_from_issue(ISSUE, REF, f"Here's the audit.\n{line} — {LEDGER}").splitlines()
        self.assertEqual(lines[1], line)
        self.assertNotIn(LEDGER, lines[1])

    def test_a_report_that_is_only_the_link_adds_no_line(self):
        lines = sar.headline_from_issue(ISSUE, REF, f"Ledger: {LEDGER}").splitlines()
        self.assertTrue(lines[1].startswith("`critical` "))

    def test_more_new_findings_than_the_ledger_lists_does_not_parse(self):
        self.assertIsNone(sar.headline_from_issue(ISSUE, REF, "8 new — x"))

    def test_the_new_count_comes_from_the_ledger_line(self):
        report = f"seeded-a: 3 new node pools without Workload Identity\n{REPORT}"
        first = sar.headline_from_issue(ISSUE, REF, report).splitlines()[0]
        self.assertTrue(first.endswith(" 2 are new since the last run."), first)

    def test_new_counts_by_severity_state_no_single_number(self):
        report = f"Cost audit: 1 new critical, 2 new major, 1 resolved — {LEDGER}"
        first = sar.headline_from_issue(ISSUE, REF, report).splitlines()[0]
        self.assertEqual(first, "**Security & RBAC Posture audit: 7 findings, 2 critical.**")

    def test_a_zero_finding_run_over_an_open_ledger_does_not_parse(self):
        # A partial or held run with nothing found leaves the ledger open over last run's title.
        for line in (
            "Security & RBAC posture audit: 0 findings, coverage incomplete (2 gaps)",
            "Security & RBAC posture audit: no findings; 1 carried finding unaccounted",
            "Security & RBAC posture audit: zero findings across 3 of 5 clusters",
            # The SOPs leave the wording of a held or partial clean line to the model.
            "Upgrade & patch readiness: clean this run; 2 carried (up-1, up-2) held, ledger stays open",
            "Cost audit: clean across 3 of 5 clusters (2 coverage gaps)",
            "Security & RBAC posture audit: no new findings across 3 of 5 clusters (2 coverage gaps)",
            "Security & RBAC posture audit: nothing new across 3 of 5 clusters",
            "Security & RBAC posture audit: 0 new, 0 resolved across 3 of 5 clusters (2 gaps)",
            "Security & RBAC posture audit: nothing reproduced across 3 of 5 clusters",
            # A held run may count its carried findings by severity; that is not a change.
            "Workload Reliability Audit: held 2 carried findings (1 critical): pdb-a, pdb-b",
            "Workload Reliability Audit: clean, 2 carried findings held (1 critical, 1 major)",
        ):
            with self.subTest(line=line):
                self.assertIsNone(sar.headline_from_issue(ISSUE, REF, f"{line} — {LEDGER}"))

    def test_a_line_that_counts_a_change_parses(self):
        for line in (
            "Security & RBAC posture audit: 0 new, 2 resolved across 3 clusters",
            "Workload Reliability Audit: 2 critical, 6 major across 4 clusters",
            "Cost audit: 1 new critical, 2 new major",
        ):
            with self.subTest(line=line):
                self.assertIsNotNone(sar.headline_from_issue(ISSUE, REF, f"{line} — {LEDGER}"))

    def test_a_new_findings_count_is_not_a_total(self):
        report = f"Upgrade & patch readiness: 3 new findings (1 critical), 2 resolved — {LEDGER}"
        self.assertIsNotNone(sar.headline_from_issue(ISSUE, REF, report))

    def test_a_matching_total_parses(self):
        self.assertIsNotNone(sar.headline_from_issue(ISSUE, REF, f"Audit: 7 findings, 2 new — {LEDGER}"))

    def test_held_rows_are_not_findings(self):
        body = (
            "### Critical (1)\n\n"
            + finding("seeded-c: a ClusterRole grants `*` on secrets", "rbac-2")
            + "\n<!-- audit-held:begin -->\n## Held by the collector\n\n"
            + finding("seeded-a: held from an earlier run", "held-1")
            + "<!-- audit-held:end -->\n"
        )
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 1 finding (1 critical)", body=body)
        headline = sar.headline_from_issue(issue, REF)
        self.assertNotIn("held from an earlier run", headline)
        self.assertEqual(headline.count("`critical` "), 1)

    def test_a_crlf_body_keeps_its_rows(self):
        crlf = dict(ISSUE, body=BODY.replace("\n", "\r\n"))
        self.assertEqual(sar.headline_from_issue(crlf, REF, REPORT), sar.headline_from_issue(ISSUE, REF, REPORT))

    def test_a_title_cannot_post_a_link_or_a_mention(self):
        title = "[seeded-a docs](https://evil.example/x) and <https://evil.example/y|seeded-b> <!channel>"
        body = "### Critical (1)\n\n" + finding(title, "evil")
        row = sar.headline_from_issue(dict(ISSUE, body=body), REF).splitlines()[1]
        self.assertNotIn("evil.example", row)
        self.assertNotIn("<", row)
        self.assertIn("seeded-a docs and seeded-b !channel", row)

    def test_a_nested_link_in_a_title_leaves_no_link_behind(self):
        for title in ("[[y](@U2)](!channel)", "[[[z](#C1)](@U2)](!here)", "<!channel|[y](@U2)>"):
            with self.subTest(title=title):
                text = sar._row_text(title)
                self.assertIsNone(sar.MD_LINK.search(text))
                self.assertIsNone(sar.SLACK_LINK.search(text))

    def test_the_audit_name_cannot_post_a_link_or_a_mention(self):
        title = "[audit] Security & RBAC Posture Audit <!channel> [see](https://evil.example/x) — 7 findings (2 critical)"
        head = sar.headline_from_issue(dict(ISSUE, title=title), REF).splitlines()[0]
        self.assertNotIn("evil.example", head)
        self.assertNotIn("<", head)
        self.assertEqual(head, "**Security & RBAC Posture Audit !channel see: 7 findings, 2 critical.**")

    def test_a_hash_line_in_fenced_evidence_ends_no_section(self):
        # fleet-audit's trim_command marks a long evidence command with a column-0 "# " line.
        command = "```bash\nkubectl get clusterrolebindings\n# … (command truncated by audit_report.py)\n"
        for close in ("```\n", ""):  # balanced, and a fence the model left open
            with self.subTest(balanced=bool(close)):
                body = (
                    "### Critical (2)\n\n"
                    + finding("seeded-b: first critical", "a")
                    + command
                    + close
                    + finding("seeded-c: second critical", "b")
                    + "\n### Major (1)\n\n"
                    + finding("seeded-a: a major", "x")
                )
                rows = sar.headline_from_issue(dict(ISSUE, body=body), REF).splitlines()[1:3]
                self.assertTrue(rows[0].endswith("seeded-b: first critical"), rows)
                self.assertTrue(rows[1].endswith("seeded-c: second critical"), rows)

    def test_an_unbalanced_fence_ends_at_the_next_severity_heading(self):
        # An impact note can carry a stray fence; the renderer's severity heading still closes it.
        body = (
            "### Critical (1)\n\n"
            + finding("crit one", "c")
            + "- **Impact:** see\n```\n"
            + "\n### Major (2)\n\n"
            + finding("maj one", "m1")
            + finding("maj two", "m2")
        )
        self.assertEqual(
            sar._severity_findings(body),
            [("critical", "crit one"), ("major", "maj one"), ("major", "maj two")],
        )

    def test_a_heading_line_in_balanced_evidence_ends_no_fence(self):
        # Evidence output can print a column-0 "### " line; only a marked heading closes an open fence.
        evidence = "```\n### generated\n#### seeded-z: not a finding\n```\n"
        body = (
            "### Critical (2)\n\n"
            + finding("seeded-b: first critical", "a")
            + evidence
            + finding("seeded-c: second critical", "b")
        )
        rows = sar.headline_from_issue(dict(ISSUE, body=body), REF).splitlines()[1:3]
        self.assertTrue(rows[0].endswith("seeded-b: first critical"), rows)
        self.assertTrue(rows[1].endswith("seeded-c: second critical"), rows)

    def test_a_severity_heading_in_balanced_evidence_relabels_nothing(self):
        body = (
            "### Critical (2)\n\n"
            "#### First critical <!-- finding:a.1 -->\n\n"
            "```\nkubectl logs x\n### Minor (1)\n```\n\n"
            '<a id="b"></a>\n\n'
            "#### Second critical <!-- finding:a.2 -->\n"
        )
        self.assertEqual(
            sar._severity_findings(body), [("critical", "First critical"), ("critical", "Second critical")]
        )

    def test_a_severity_heading_in_an_open_fence_still_ends_it(self):
        body = (
            "### Major (1)\n\n"
            "#### Real major <!-- finding:a.1 -->\n\n"
            "```\nlog\n### Minor (1)\n\n"
            "#### A minor <!-- finding:b.1 -->\n\n```\n"
        )
        self.assertEqual(sar._severity_findings(body), [("major", "Real major"), ("minor", "A minor")])

    def test_zero_in_the_title_with_findings_in_the_body_is_not_clean(self):
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 0 findings (0 critical)")
        self.assertIsNone(sar.headline_from_issue(issue, REF))

    def test_coverage_incomplete_title_does_not_parse(self):
        issue = dict(ISSUE, title="[audit] Cost Audit — coverage incomplete (2 gaps, 0 findings)", body="")
        self.assertIsNone(sar.headline_from_issue(issue, REF))

    def test_title_must_be_whole(self):
        issue = dict(ISSUE, title="Re: " + ISSUE["title"])
        self.assertIsNone(sar.headline_from_issue(issue, REF))

    def test_a_retyped_dash_still_matches(self):
        self.assertIsNotNone(sar.headline_from_issue(dict(ISSUE, title=ISSUE["title"].replace("—", "-")), REF))

    def test_repro_c_quoted_evidence_cannot_make_it_clean(self):
        report = (
            "- **[critical] seeded-c** — ClusterRoleBinding grants cluster-admin\n"
            '  evidence: "[audit] Security & RBAC Posture Audit — 0 findings (0 critical) '
            'https://github.com/evil/phish/issues/1"\n'
            f"Ledger: {LEDGER}\n"
        )
        self.assertEqual(sar.ledger_ref(report), REF)
        headline = sar.headline_from_issue(ISSUE, REF, report)
        self.assertNotIn("clean", headline)
        self.assertNotIn("evil", headline)

    def test_long_rows_are_clipped(self):
        body = "### Critical (1)\n\n" + finding("word " * 60, "w")
        row = sar.headline_from_issue(dict(ISSUE, body=body), REF).splitlines()[1]
        self.assertTrue(row.endswith("…"))
        self.assertLessEqual(len(row), len("`critical` ") + sar.FINDING_ROW_MAX)

    def test_repro_a_a_clipped_row_closes_its_code_span(self):
        title = (
            "seeded-c: ClusterRoleBinding `default-sa-admin binds cluster-admin to "
            "system:serviceaccount:payments:default and grants every verb on every resource in the cluster` is live"
        )
        body = "### Critical (1)\n\n" + finding(title, "long")
        row = sar.headline_from_issue(dict(ISSUE, body=body), REF).splitlines()[1]
        self.assertTrue(row.endswith("`…"), row)
        self.assertEqual(row.count("`") % 2, 0)
        self.assertLessEqual(len(row), len("`critical` ") + sar.FINDING_ROW_MAX)


class HeadlineFallbackTest(unittest.TestCase):
    def test_the_sop_line_in_bold_with_the_ledger_link(self):
        self.assertEqual(
            sar.headline_fallback(REPORT, REF),
            f"**Security & RBAC posture audit: 2 new, 1 resolved across 3 clusters**\n[Ledger issue #231 ↗]({LEDGER})",
        )

    def test_a_multi_line_report_leads_with_its_first_line(self):
        report = f"## Security audit: 3 findings\n\n- a finding\n\nLedger: {LEDGER}\n"
        self.assertTrue(sar.headline_fallback(report, REF).startswith("**Security audit: 3 findings**\n"))

    def test_an_orienting_sentence_above_the_ledger_line_is_not_the_headline(self):
        report = f"Here's this morning's security audit.\n{REPORT}"
        self.assertEqual(sar.headline_fallback(report, REF), sar.headline_fallback(REPORT, REF))

    def test_an_orienting_sentence_is_not_the_headline_over_a_bare_link(self):
        report = f"Here's this morning's security audit.\n{LINE}\nLedger: {LEDGER}"
        self.assertEqual(sar.headline_fallback(report, REF), sar.headline_fallback(REPORT, REF))

    def test_no_line_with_counts_over_a_bare_link_has_no_headline(self):
        report = f"Here's this morning's security audit.\n- seeded-a: 2 open findings\nLedger: {LEDGER}"
        self.assertIsNone(sar.headline_fallback(report, REF))

    def test_a_long_line_keeps_its_coverage(self):
        line = "Workload Reliability Audit: " + "2 critical, 6 major, 11 minor, " * 5 + "across 4 of 9 clusters"
        self.assertIn("across 4 of 9 clusters", sar.headline_fallback(f"{line} — {LEDGER}", REF))

    def test_a_bare_ledger_line_has_no_headline(self):
        self.assertIsNone(sar.headline_fallback(f"Ledger: {LEDGER}", REF))


class LedgerLineTest(unittest.TestCase):
    def test_a_ledger_url_wrapped_onto_its_own_line_is_not_the_line(self):
        for report in (f"{LINE} —\n{LEDGER}", f"{LINE}\nLedger:\n<{LEDGER}>"):
            with self.subTest(report=report):
                self.assertEqual(sar.ledger_ref(report), REF)
                self.assertEqual(sar._ledger_line(report), LINE)
                self.assertTrue(sar.headline_fallback(report, REF).startswith(f"**{LINE}**"))


class BalancedClipTest(unittest.TestCase):
    def test_a_short_title_with_a_stray_backtick_is_left_alone(self):
        title = "seeded-a: `default SA is cluster-admin"
        self.assertEqual(sar._balanced_clip(title, sar.FINDING_ROW_MAX), title)


class NeedsFoldTest(unittest.TestCase):
    def test_a_line_shown_whole_is_not_folded(self):
        report = f"{LINE} — {LEDGER}"
        self.assertFalse(sar.needs_fold(report, sar.headline_from_issue(ISSUE, REF, report)))
        self.assertFalse(sar.needs_fold(report, sar.headline_fallback(report, REF)))

    def test_a_line_whose_pr_links_were_flattened_is_folded(self):
        report = f"{LINE}, remediation PRs opened: [#12](https://github.com/acme/fleet-config/pull/12) — {LEDGER}"
        self.assertTrue(sar.needs_fold(report, sar.headline_from_issue(ISSUE, REF, report)))

    def test_a_clipped_line_is_folded(self):
        prs = ", ".join(f"https://github.com/acme/fleet-config/pull/{n}" for n in range(1230, 1236))
        report = f"{LINE}, remediation PRs opened: {prs} — {LEDGER}"
        self.assertTrue(sar.needs_fold(report, sar.headline_from_issue(ISSUE, REF, report)))
        self.assertTrue(sar.needs_fold(report, sar.headline_fallback(report, REF)))

    def test_emphasis_in_a_line_shown_whole_is_not_folded(self):
        report = f"Cost audit: **2 new**, 1 resolved across `3` clusters — {LEDGER}"
        self.assertFalse(sar.needs_fold(report, sar.headline_from_issue(ISSUE, REF, report)))
        self.assertFalse(sar.needs_fold(report, sar.headline_fallback(report, REF)))

    def test_several_lines_are_folded(self):
        report = f"Audit\n\n- a finding\n{LEDGER}"
        self.assertTrue(sar.needs_fold(report, sar.headline_fallback(report, REF) or ""))


class HasMoreTest(unittest.TestCase):
    def test_one_line_has_nothing_more(self):
        self.assertFalse(sar.has_more(REPORT + "\n"))

    def test_several_lines_do(self):
        self.assertTrue(sar.has_more(f"Audit\n\n- a finding\n{LEDGER}"))


class BlocksFromIssueTest(unittest.TestCase):
    def test_a_click_posts_only_what_the_look_at_button_shows(self):
        # The top finding outruns the 75-character label; the click handler
        # posts the value as the user's turn, so the clipped tail stays out.
        blocks, _, _ = sar.blocks_from_issue(ISSUE, REF, REPORT)
        choice = next(b for b in blocks if b["type"] == "actions")["elements"][0]
        self.assertTrue(choice["text"]["text"].endswith("…"))
        self.assertEqual(choice["value"], choice["text"]["text"])
        self.assertNotIn("account", choice["value"])

    def test_mock_08(self):
        blocks, text, rest = sar.blocks_from_issue(ISSUE, REF, REPORT)
        self.assertEqual(
            [b["type"] for b in blocks], ["rich_text", "divider", "rich_text", "divider", "actions", "container"]
        )
        head = blocks[0]["elements"][0]["elements"]
        self.assertEqual(head[0]["text"], "Security & RBAC Posture audit: 7 findings, 2 critical.")
        self.assertEqual(head[1]["text"], " 2 are new since the last run.")
        header = blocks[2]["elements"][0]["elements"][0]["text"]
        self.assertEqual(header, "2 critical")
        choice, link = blocks[4]["elements"]
        self.assertEqual(choice["text"]["text"], "look at: seeded-b, seeded-c: cluster-admin bound to the default service…")
        self.assertEqual(choice["action_id"], "kage_audit.choice.0")
        self.assertEqual(choice["style"], "primary")
        self.assertEqual((link["text"]["text"], link["url"]), ("Ledger issue #231 ↗", LEDGER))
        # The issue lists four of the seven its title counts, and the fold says so.
        self.assertEqual(blocks[5]["title"]["text"], "4 of 7 findings")
        self.assertEqual(len(blocks[5]["child_blocks"][0]["elements"]), 4)
        self.assertEqual(rest, "")
        self.assertEqual(
            text,
            "*Security &amp; RBAC Posture audit: 7 findings, 2 critical.*\n"
            "`critical` seeded-b, seeded-c: `cluster-admin` bound to the default service account\n"
            "`critical` seeded-c: a ClusterRole grants `*` on secrets\n"
            f"<{LEDGER}|Ledger issue #231 ↗>",
        )

    def test_choice_label_comes_from_the_finding(self):
        body = BODY.replace("seeded-b, seeded-c: `cluster-admin` bound to the default service account", "privileged pods")
        blocks, _, _ = sar.blocks_from_issue(dict(ISSUE, body=body), REF, REPORT)
        self.assertEqual(blocks[4]["elements"][0]["text"]["text"], "look at: privileged pods")

    def test_every_finding_listed_is_all_of_them(self):
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 4 findings (2 critical)")
        blocks, _, _ = sar.blocks_from_issue(issue, REF, REPORT)
        self.assertEqual(blocks[-1]["title"]["text"], "all 4 findings")

    def test_more_findings_listed_than_counted_keeps_the_title_count(self):
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 3 findings (2 critical)")
        blocks, _, rest = sar.blocks_from_issue(issue, REF, REPORT, fold_in_place=False)
        self.assertIn("3 findings", str(blocks[0]))
        self.assertTrue(rest.startswith("**all 3 findings**"), rest)
        self.assertEqual(len(rest.splitlines()), 4)
        blocks, _, _ = sar.blocks_from_issue(issue, REF, REPORT)
        self.assertEqual(blocks[-1]["title"]["text"], "all 3 findings")

    def test_without_the_fold_the_findings_are_markdown_for_the_thread(self):
        blocks, _, rest = sar.blocks_from_issue(ISSUE, REF, REPORT, fold_in_place=False)
        self.assertEqual(blocks[-1]["type"], "actions")
        self.assertEqual(
            rest.splitlines()[:2],
            ["**4 of 7 findings**", "`critical` seeded-b, seeded-c: `cluster-admin` bound to the default service account"],
        )
        self.assertEqual(len(rest.splitlines()), 5)

    def test_the_relayed_line_sits_under_the_headline(self):
        blocks, _, _ = sar.blocks_from_issue(ISSUE, REF, REPORT)
        self.assertEqual(blocks[0]["elements"][1]["elements"], [{"type": "text", "text": LINE}])

    def test_no_blocks_wherever_the_text_headline_falls_back(self):
        for label, issue, report in (
            ("closed", dict(ISSUE, state="closed"), REPORT),
            ("unlabelled", dict(ISSUE, labels=["bug"]), REPORT),
            ("stale", ISSUE, f"Security audit: 9 new — {LEDGER}"),
            ("old total", ISSUE, f"Security audit: 0 findings across 2 of 3 clusters — {LEDGER}"),
            ("zero", dict(ISSUE, title="[audit] Cost Audit — 0 findings (0 critical)"), REPORT),
        ):
            with self.subTest(label):
                self.assertIsNone(sar.headline_from_issue(issue, REF, report))
                self.assertIsNone(sar.blocks_from_issue(issue, REF, report))

    def test_block_rows_cannot_post_a_link_or_a_mention(self):
        body = "### Critical (1)\n\n" + finding("see [the fix](https://evil.example) <!channel>", "l-1")
        blocks, text, rest = sar.blocks_from_issue(dict(ISSUE, body=body), REF, REPORT, fold_in_place=False)
        self.assertNotIn("evil.example", str(blocks) + text + rest)
        self.assertNotIn("<!", str(blocks) + text + rest)
        self.assertEqual(blocks[4]["elements"][0]["value"], "look at: see the fix !channel")

    def test_held_and_crlf_rows_in_the_fold(self):
        body = (
            "### Critical (1)\r\n\r\n"
            + finding("seeded-c: a ClusterRole grants `*` on secrets", "rbac-2").replace("\n", "\r\n")
            + "\r\n<!-- audit-held:begin -->\r\n## Held by the collector\r\n\r\n"
            + finding("seeded-a: held from an earlier run", "held-1")
        )
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 1 finding (1 critical)", body=body)
        blocks, _, _ = sar.blocks_from_issue(issue, REF, f"Security audit: 1 new — {LEDGER}")
        self.assertEqual(blocks[-1]["title"]["text"], "1 finding")
        self.assertNotIn("held from an earlier run", str(blocks))

    def test_clean_and_unparsed_runs_have_no_blocks(self):
        clean = {"title": "[audit] Cost Audit — 0 findings (0 critical)", "body": "Nothing."}
        self.assertIsNone(sar.blocks_from_issue(clean, REF, "Cost audit: clean — " + LEDGER))
        self.assertIsNone(sar.blocks_from_issue({"title": "something else"}, REF, REPORT))


if __name__ == "__main__":
    unittest.main()
