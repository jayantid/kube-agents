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

PRESENTED = """**I scanned 3 clusters and 41 workloads.** Posture is mostly healthy.

Two worth fixing first:
\U0001f534 **critical**  seeded-b and seeded-c admit privileged pods
**Default service account is cluster-admin on seeded-c**

**2 more worth a look:**
- \U0001f7e0 **major**  Workload Identity is off on seeded-a
- payments-api has no PodDisruptionBudget

Also found: 18 more items, tracked in the findings queue — ask for the full list.

The full inventory is available — just ask.
"""


class PresentTest(unittest.TestCase):
    def test_headline_top_two_and_the_rest(self):
        self.assertEqual(inventory_presenter.present(REPORT), PRESENTED)

    def test_two_items_have_no_lead_or_rest(self):
        report = "# Scan\n\nAll quiet. One thing stands out.\n\n1. **A (minor)**\n   Fix it.\n2. **B**\n   Fix that.\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**All quiet.** One thing stands out.\n\n\U0001f7e1 **minor**  A\n**B**\n",
        )

    def test_one_item_of_three_top_reads_one(self):
        report = "Posture.\n\n1. **A**\n   x\n2. **B**\n   y\n3. **C**\n   z\n"
        out = inventory_presenter.present(report)
        self.assertIn("Two to look at first:\n**A**\n**B**", out)
        self.assertIn("**1 more worth a look:**\n- C", out)

    def test_severity_in_the_sentence_is_not_a_label(self):
        report = "Posture.\n\n1. **A**\n   This is not critical.\n"
        self.assertEqual(inventory_presenter.present(report), "**Posture.**\n\n**A**\n")

    def test_a_partial_bold_headline_keeps_the_whole_line(self):
        report = (
            "Posture.\n\n"
            "1. **Critical:** seeded-b and seeded-c admit privileged pods.\n   Enforce baseline.\n"
            "2. **seeded-c:** the default service account is cluster-admin.\n   Remove the binding.\n"
            "3. **No PDB** on payments-api\n   Add one.\n"
        )
        self.assertEqual(
            inventory_presenter.present(report),
            "**Posture.**\n\n"
            "Two worth fixing first:\n"
            "\U0001f534 **critical**  seeded-b and seeded-c admit privileged pods.\n"
            "**seeded-c: the default service account is cluster-admin.**\n\n"
            "**1 more worth a look:**\n- No PDB on payments-api\n",
        )

    def test_a_bold_sentence_is_the_headline_and_the_rest_its_sentence(self):
        report = "Posture.\n\n1. **Default SA is cluster-admin on seeded-c.** A compromised pod owns it.\n"
        self.assertEqual(
            inventory_presenter.present(report), "**Posture.**\n\n**Default SA is cluster-admin on seeded-c.**\n"
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
            "**Posture.**\n\nTwo to look at first:\n**A on seeded-b**\n**B on seeded-c**\n\n"
            "**1 more worth a look:**\n- C on payments-api\n\nThe full inventory is available.\n",
        )

    def test_criticals_are_never_rolled_up(self):
        report = "Posture.\n\n" + "".join(f"{i}. **[critical] problem {i}**\n   x\n" for i in range(1, 4))
        report += "4. **other**\n   y\n"
        out = inventory_presenter.present(report)
        rows = "\n".join(f"\U0001f534 **critical**  problem {i}" for i in range(1, 4))
        self.assertIn("Three worth fixing first:\n" + rows, out)
        self.assertIn("**1 more worth a look:**\n- other", out)
        self.assertNotIn("[critical]", out)

    def test_a_quiet_cluster_gets_the_neutral_lead(self):
        report = "No critical or major findings.\n\n"
        report += "".join(f"{i}. **item {i} (minor)**\n   x\n" for i in range(1, 4))
        out = inventory_presenter.present(report)
        self.assertIn("Two to look at first:", out)
        self.assertNotIn("worth fixing", out)

    def test_a_severity_word_inside_a_name_is_not_a_label(self):
        report = "Posture.\n\n1. **major-version skew on node pool `minor-pool`**\n   Upgrade.\n"
        self.assertEqual(
            inventory_presenter.present(report), "**Posture.**\n\n**major-version skew on node pool `minor-pool`**\n"
        )

    def test_an_abbreviation_does_not_end_the_headline(self):
        report = "Scanned 2 clusters, e.g. prod and staging. Posture is weak.\n\n1. **A**\n   x\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**Scanned 2 clusters, e.g. prod and staging.** Posture is weak.\n\n**A**\n",
        )

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

    def test_google_chat_is_verbatim_with_the_flag_on(self):
        self.assertEqual(self._run("1", platform="google_chat"), REPORT)

    def test_a_missing_origin_is_verbatim_with_the_flag_on(self):
        self.assertEqual(self._run("1", platform=None), REPORT)

    def test_presenter_failure_delivers_verbatim(self):
        boom = mock.patch.object(inventory_presenter, "present", side_effect=ValueError("boom"))
        with boom, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self._run("1"), REPORT)


if __name__ == "__main__":
    unittest.main()
