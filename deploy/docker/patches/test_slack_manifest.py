#!/usr/bin/env python3
"""Host tests for the Slack manifest record and its comparison. No Hermes install required.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py'

The in-image gate runs ``verify_slack_manifest.py`` against the real
``_build_full_manifest``; it is the authority on whether the record matches what
ships. These cover the record's own consistency (the release-note rule a pull
request editing the record meets here, before any image build), the normalizer,
the diff ``upgrade.sh`` prints, and the verifier's failure modes against a
fixture ``slack_cli.py`` that emits whatever manifest a test hands it.
"""

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import slack_manifest
import verify_slack_manifest

RECORD = json.loads(slack_manifest.RECORD.read_text())

FIXTURE = '''\
"""Fixture standing in for hermes_cli/slack_cli.py."""
import json
import os

MANIFESTS = json.loads({manifests!r})
FLAG_EXTRA_EVENT = {flag_extra_event!r}


def _build_full_manifest(bot_name, bot_description, messaging_experience=None):
    manifest = json.loads(json.dumps(MANIFESTS[messaging_experience]))
    manifest["_metadata"] = {{"major_version": 1, "minor_version": 1}}
    manifest["display_information"] = {{"name": bot_name, "description": bot_description}}
    manifest["features"]["bot_user"] = {{"display_name": bot_name, "always_online": True}}
    manifest["features"]["slash_commands"] = [{{"command": "/stop"}}]
    for key in list(manifest["features"]):
        if key.endswith("_view"):
            on = os.environ.get("KAGE_SLACK_UX") == "1"
            manifest["features"][key] = {{"description": "kube-agents" if on else "Hermes"}}
    manifest["oauth_config"]["scopes"]["bot"].reverse()
    if FLAG_EXTRA_EVENT and os.environ.get("KAGE_SLACK_UX") == "1":
        manifest["settings"]["event_subscriptions"]["bot_events"].append(FLAG_EXTRA_EVENT)
    return manifest
'''


def raw_from(normalized: dict) -> dict:
    """A manifest as the CLI prints it, for a normalized one."""
    raw = copy.deepcopy(normalized)
    raw["display_information"] = {"name": "Kubeagents", "description": "ours"}
    raw["features"]["bot_user"] = {"display_name": "Kubeagents"}
    raw["features"]["slash_commands"] = [{"command": "/btw"}]
    raw["oauth_config"]["scopes"]["bot"] = list(reversed(raw["oauth_config"]["scopes"]["bot"]))
    return raw


def with_scope(normalized: dict, scope: str) -> dict:
    changed = copy.deepcopy(normalized)
    changed["oauth_config"]["scopes"]["bot"] = sorted({*changed["oauth_config"]["scopes"]["bot"], scope})
    return changed


class RecordTest(unittest.TestCase):
    """The checked-in record holds every rule the build gate applies to it."""

    def test_the_record_is_consistent(self):
        self.assertEqual(slack_manifest.record_problems(RECORD), [])

    def test_the_record_holds_the_settings_an_install_needs(self):
        for experience, manifest in RECORD["manifests"].items():
            self.assertIs(manifest["settings"]["interactivity"]["is_enabled"], True, experience)
            self.assertIs(manifest["settings"]["socket_mode_enabled"], True, experience)
        for experience in ("assistant", "agent"):
            self.assertIn("assistant:write", RECORD["manifests"][experience]["oauth_config"]["scopes"]["bot"])

    def test_the_recorded_manifests_are_already_normalized(self):
        for manifest in RECORD["manifests"].values():
            self.assertEqual(slack_manifest.normalize(manifest), manifest)

    def test_a_manifest_change_without_a_note_is_refused(self):
        record = copy.deepcopy(RECORD)
        record["manifests"]["agent"] = with_scope(record["manifests"]["agent"], "canvases:write")
        problems = slack_manifest.record_problems(record)
        self.assertTrue(any("the last changes entry has digest" in p for p in problems), problems)

    def test_an_entry_with_an_empty_note_is_refused(self):
        record = copy.deepcopy(RECORD)
        record["manifests"]["agent"] = with_scope(record["manifests"]["agent"], "canvases:write")
        record["changes"].append({**slack_manifest.expected_change(record["manifests"]), "note": "  "})
        self.assertIn(f"changes[{len(record['changes']) - 1}] has no note", slack_manifest.record_problems(record))

    def test_a_noted_change_is_accepted(self):
        record = copy.deepcopy(RECORD)
        record["manifests"]["agent"] = with_scope(record["manifests"]["agent"], "canvases:write")
        record["changes"].append({**slack_manifest.expected_change(record["manifests"]), "note": "Adds canvases."})
        self.assertEqual(slack_manifest.record_problems(record), [])

    def test_a_revert_to_an_earlier_manifest_is_accepted(self):
        record = copy.deepcopy(RECORD)
        record["manifests"]["agent"] = with_scope(record["manifests"]["agent"], "canvases:write")
        record["changes"].append({**slack_manifest.expected_change(record["manifests"]), "note": "Adds canvases."})
        record["manifests"] = copy.deepcopy(RECORD["manifests"])
        record["changes"].append({**slack_manifest.expected_change(record["manifests"]), "note": "Drops canvases."})
        self.assertEqual(slack_manifest.record_problems(record), [])

    def test_an_entry_repeating_the_one_before_is_refused(self):
        record = copy.deepcopy(RECORD)
        record["changes"].append({**record["changes"][-1], "note": "Nothing changed."})
        problems = slack_manifest.record_problems(record)
        self.assertIn(f"changes[{len(record['changes']) - 1}] repeats the digest of the entry before it", problems)

    def test_losing_a_required_setting_is_refused(self):
        record = copy.deepcopy(RECORD)
        record["manifests"]["none"]["settings"]["interactivity"]["is_enabled"] = False
        record["manifests"]["agent"]["oauth_config"]["scopes"]["bot"].remove("assistant:write")
        record["changes"].append({**slack_manifest.expected_change(record["manifests"]), "note": "Broken."})
        self.assertEqual(
            slack_manifest.record_problems(record),
            [
                "agent: bot scopes lack assistant:write",
                "none: settings.interactivity.is_enabled is not true",
            ],
        )


class CompareTest(unittest.TestCase):
    """What ``upgrade.sh`` prints for the running image's manifest."""

    def test_the_same_manifest_reports_nothing(self):
        status, report = slack_manifest.compare(raw_from(RECORD["manifests"]["assistant"]), RECORD)
        self.assertEqual((status, report), (slack_manifest.EXIT_SAME, ""))

    def test_a_recorded_older_manifest_gets_the_diff_and_only_the_later_notes(self):
        older = copy.deepcopy(RECORD["manifests"]["assistant"])
        older["oauth_config"]["scopes"]["bot"].remove("reactions:write")
        record = copy.deepcopy(RECORD)
        record["changes"][0]["assistant_digest"] = slack_manifest.digest(older)
        status, report = slack_manifest.compare(raw_from(older), record)
        self.assertEqual(status, slack_manifest.EXIT_DIFFERENT)
        self.assertIn("  + oauth_config.scopes.bot: reactions:write", report)
        self.assertIn("What changed since the running version:", report)
        self.assertNotIn(record["changes"][0]["note"], report)
        self.assertIn(record["changes"][-1]["note"], report)

    def test_an_unrecorded_manifest_gets_every_note(self):
        unknown = copy.deepcopy(RECORD["manifests"]["assistant"])
        unknown["settings"]["interactivity"]["is_enabled"] = False
        unknown["settings"]["event_subscriptions"]["bot_events"].append("team_join")
        status, report = slack_manifest.compare(raw_from(unknown), RECORD)
        self.assertEqual(status, slack_manifest.EXIT_DIFFERENT)
        self.assertIn("  ~ settings.interactivity.is_enabled: false -> true", report)
        self.assertIn("  - settings.event_subscriptions.bot_events: team_join", report)
        self.assertIn("matches no recorded version", report)
        for change in RECORD["changes"]:
            self.assertIn(change["note"], report)

    def test_a_view_appearing_is_named(self):
        installed = copy.deepcopy(RECORD["manifests"]["assistant"])
        del installed["features"]["assistant_view"]
        _, report = slack_manifest.compare(raw_from(installed), RECORD)
        self.assertIn("  + features.assistant_view", report)

    def test_every_experience_is_compared_and_labelled(self):
        installed = {experience: raw_from(manifest) for experience, manifest in RECORD["manifests"].items()}
        status, report = slack_manifest.compare(installed, RECORD)
        self.assertEqual((status, report), (slack_manifest.EXIT_SAME, ""))
        older = copy.deepcopy(RECORD["manifests"]["agent"])
        older["oauth_config"]["scopes"]["bot"].remove("reactions:write")
        installed["agent"] = raw_from(older)
        status, report = slack_manifest.compare(installed, RECORD)
        self.assertEqual(status, slack_manifest.EXIT_DIFFERENT)
        self.assertIn("The 'agent' experience's manifest differs", report)
        self.assertNotIn("'assistant'", report)
        self.assertIn("  + oauth_config.scopes.bot: reactions:write", report)

    def test_the_full_set_finds_the_running_version_by_its_digest(self):
        record = copy.deepcopy(RECORD)
        older = copy.deepcopy(record["manifests"])
        older["agent"]["oauth_config"]["scopes"]["bot"].remove("reactions:write")
        record["changes"][0]["digest"] = slack_manifest.digest(older)
        installed = {experience: raw_from(manifest) for experience, manifest in older.items()}
        _, report = slack_manifest.compare(installed, record)
        self.assertIn("What changed since the running version:", report)
        self.assertNotIn(record["changes"][0]["note"], report)
        self.assertIn(record["changes"][-1]["note"], report)

    def test_a_partial_set_whose_default_is_current_gets_every_note(self):
        older = copy.deepcopy(RECORD["manifests"]["agent"])
        older["oauth_config"]["scopes"]["bot"].remove("reactions:write")
        installed = {"assistant": raw_from(RECORD["manifests"]["assistant"]), "agent": raw_from(older)}
        _, report = slack_manifest.compare(installed, RECORD)
        self.assertIn("The 'agent' experience's manifest differs", report)
        self.assertIn("matches no recorded version", report)
        for change in RECORD["changes"]:
            self.assertIn(change["note"], report)

    def test_a_partial_set_matching_several_versions_starts_at_the_earliest(self):
        older = copy.deepcopy(RECORD["manifests"]["assistant"])
        older["oauth_config"]["scopes"]["bot"].remove("reactions:write")
        record = copy.deepcopy(RECORD)
        record["changes"][0]["assistant_digest"] = slack_manifest.digest(older)
        record["changes"].insert(
            1, {"digest": "0" * 12, "assistant_digest": slack_manifest.digest(older), "note": "Agent view only."}
        )
        _, report = slack_manifest.compare(raw_from(older), record)
        self.assertIn("several recorded versions share its default one", report)
        self.assertNotIn(record["changes"][0]["note"], report)
        self.assertIn("Agent view only.", report)
        self.assertIn(record["changes"][-1]["note"], report)

    def test_a_set_without_the_default_experience_is_refused(self):
        with self.assertRaises(ValueError):
            slack_manifest.compare({"agent": raw_from(RECORD["manifests"]["agent"])}, RECORD)
        with self.assertRaises(ValueError):
            slack_manifest.compare(
                {"assistant": raw_from(RECORD["manifests"]["assistant"]), "extra": {}}, RECORD
            )

    def test_the_cli_exit_codes(self):
        record_path = str(slack_manifest.RECORD)
        cases = [
            (json.dumps(raw_from(RECORD["manifests"]["assistant"])), slack_manifest.EXIT_SAME),
            (json.dumps(raw_from(RECORD["manifests"]["none"])), slack_manifest.EXIT_DIFFERENT),
            ("Error: deployment not found", slack_manifest.EXIT_USAGE),
            ("[]", slack_manifest.EXIT_USAGE),
        ]
        for stdin, expected in cases:
            with self.subTest(stdin=stdin[:30]):
                sys.stdin = io.StringIO(stdin)
                try:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        status = slack_manifest.main(["compare", record_path])
                finally:
                    sys.stdin = sys.__stdin__
                self.assertEqual(status, expected)


class VerifyTest(unittest.TestCase):
    """The build gate's failure modes, against a fixture CLI."""

    def run_verify(self, manifests: dict, flag_extra_event: str = "", record: dict = RECORD) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hermes_cli").mkdir()
            (root / verify_slack_manifest.MANIFEST).write_text(
                FIXTURE.format(manifests=json.dumps(manifests), flag_extra_event=flag_extra_event)
            )
            record_path = root / "slack_manifest.json"
            record_path.write_text(json.dumps(record))
            out = io.StringIO()
            with redirect_stdout(out):
                verify_slack_manifest.main(root, record_path)
            return out.getvalue()

    def test_a_matching_image_passes(self):
        self.assertIn("match slack_manifest.json", self.run_verify(RECORD["manifests"]))

    def test_an_unrecorded_scope_fails_with_what_to_paste(self):
        manifests = copy.deepcopy(RECORD["manifests"])
        manifests["agent"] = with_scope(manifests["agent"], "canvases:write")
        with self.assertRaises(SystemExit) as caught:
            self.run_verify(manifests)
        message = str(caught.exception)
        self.assertIn("agent: + oauth_config.scopes.bot: canvases:write", message)
        self.assertIn(slack_manifest.expected_change(manifests)["digest"], message)

    def test_a_flag_dependent_manifest_fails(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_verify(RECORD["manifests"], flag_extra_event="app_home_opened")
        self.assertIn("changes with KAGE_SLACK_UX", str(caught.exception))

    def test_a_record_without_the_note_fails(self):
        record = copy.deepcopy(RECORD)
        record["changes"].pop()
        with self.assertRaises(SystemExit) as caught:
            self.run_verify(RECORD["manifests"], record=record)
        self.assertIn("the last changes entry has digest", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
