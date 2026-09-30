"""Tests for inventory_presenter and its flag gate in bootstrap_delivery."""

import contextlib
import io
import os
import sys
import tempfile
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
- Workload Identity is off on seeded-a (major)
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
        self.assertIn("Two worth fixing first:\n**A**\n**B**", out)
        self.assertIn("**1 more worth a look:**\n- C", out)

    def test_severity_in_the_sentence_is_not_a_label(self):
        report = "Posture.\n\n1. **A**\n   This is not critical.\n"
        self.assertEqual(inventory_presenter.present(report), "**Posture.**\n\n**A**\n")

    def test_unparseable_reports_are_unchanged(self):
        for report in (
            "",
            "# Report\n\n| Cluster | ... |\n",
            "# Scan\n\n1. **A**\n   x\n",  # no posture
            "Posture.\n\n1. plain item, no bold headline\n",
            "Posture.\n\n1. **A**\n   x\n\n## Another section\n",
        ):
            with self.subTest(report=report):
                self.assertEqual(inventory_presenter.present(report), report)


class DeliveryFlagTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        (self.d / "INVENTORY.md").write_text(REPORT, encoding="utf-8")
        (self.d / ".user_aligned").touch()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, flag):
        env = {k: v for k, v in os.environ.items() if k != "KAGE_SLACK_UX"}
        if flag is not None:
            env["KAGE_SLACK_UX"] = flag
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(buf):
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

    def test_presenter_failure_delivers_verbatim(self):
        boom = mock.patch.object(inventory_presenter, "present", side_effect=ValueError("boom"))
        with boom, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self._run("1"), REPORT)


if __name__ == "__main__":
    unittest.main()
