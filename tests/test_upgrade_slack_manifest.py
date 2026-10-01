"""Tests for upgrade.sh's Slack manifest step, against a fake kubectl.

``check_slack_manifest`` reads the running image's manifest through
``kubectl exec … hermes slack manifest`` and compares it with the record in the
sources being upgraded to. The comparison itself is covered by
``deploy/docker/patches/test_slack_manifest.py``; these cover the shell around
it: when it runs, what it prints, and that nothing it meets stops the upgrade.
"""

import copy
import json
import pathlib
import shlex
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_UPGRADE_SH = _REPO_ROOT / "upgrade.sh"
_COMMON_SH = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"
_RECORD = json.loads((_REPO_ROOT / "deploy" / "docker" / "patches" / "slack_manifest.json").read_text())

_FAKE_KUBECTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_KUBECTL_LOG"
[ -n "${FAKE_KUBECTL_OUTPUT:-}" ] || exit 1
cat "$FAKE_KUBECTL_OUTPUT"
"""


def _printed(normalized):
    """A manifest as ``hermes slack manifest`` prints it."""
    raw = copy.deepcopy(normalized)
    raw["display_information"] = {"name": "Hermes", "description": "Your Hermes agent on Slack"}
    raw["features"]["bot_user"] = {"display_name": "Hermes", "always_online": True}
    raw["features"]["slash_commands"] = [{"command": "/stop"}]
    return raw


class CheckSlackManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)], check=False)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "kubectl").write_text(_FAKE_KUBECTL)
        (bin_dir / "kubectl").chmod(0o755)
        self.log = self.tmp / "kubectl.log"
        self.log.touch()
        self.env = get_isolated_test_env(
            overrides={"FAKE_KUBECTL_LOG": str(self.log), "SLACK_ENABLED": "true"}, bin_dir=bin_dir
        )

    def run_check(self, printed=None, output_text=None, repo_dir=_REPO_ROOT, env=None):
        if printed is not None:
            output_text = json.dumps(printed)
        if output_text is not None:
            output = self.tmp / "manifest.json"
            output.write_text(output_text)
            self.env["FAKE_KUBECTL_OUTPUT"] = str(output)
        script = (
            f"KUBE_AGENTS_SOURCE_ONLY=true source {shlex.quote(str(_UPGRADE_SH))}\n"
            f"source {shlex.quote(str(_COMMON_SH))}\n"
            f"check_slack_manifest agents-ns {shlex.quote(str(repo_dir))}\n"
            'echo "rc=$? changed=$SLACK_MANIFEST_CHANGED"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env={**self.env, **(env or {})},
            cwd=str(_REPO_ROOT),
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def test_an_install_without_slack_is_not_touched(self):
        out = self.run_check(printed=_printed(_RECORD["manifests"]["assistant"]), env={"SLACK_ENABLED": "false"})
        self.assertIn("rc=0 changed=false", out)
        self.assertEqual(self.log.read_text(), "")
        self.assertNotIn("Slack", out)

    def test_the_same_manifest_says_so(self):
        out = self.run_check(printed=_printed(_RECORD["manifests"]["assistant"]))
        self.assertIn("The Slack app's manifest is the same in this version.", out)
        self.assertIn("rc=0 changed=false", out)
        self.assertEqual(
            self.log.read_text().split(),
            ["exec", "deployment/platform-agent-gateway", "-c", "platform-agent", "-n", "agents-ns", "--request-timeout=60s", "--", "hermes", "slack", "manifest"],
        )

    def test_a_changed_manifest_prints_the_diff_the_notes_and_the_steps(self):
        older = copy.deepcopy(_RECORD["manifests"]["assistant"])
        older["oauth_config"]["scopes"]["bot"].remove("reactions:write")
        out = self.run_check(printed=_printed(older))
        self.assertIn("This version changes the Slack app's manifest.", out)
        self.assertIn("  + oauth_config.scopes.bot: reactions:write", out)
        self.assertIn(_RECORD["changes"][-1]["note"], out)
        self.assertIn(
            "1. kubectl exec deploy/platform-agent-gateway -c platform-agent -n agents-ns -- hermes slack manifest", out
        )
        self.assertIn("Reinstall the app", out)
        self.assertIn("rc=0 changed=true", out)

    def test_a_pod_it_cannot_exec_into_does_not_stop_the_upgrade(self):
        out = self.run_check()
        self.assertIn("Could not read the running Slack manifest", out)
        self.assertIn("rc=0 changed=false", out)

    def test_output_that_is_not_a_manifest_does_not_stop_the_upgrade(self):
        out = self.run_check(output_text="error: unable to upgrade connection")
        self.assertIn("Could not compare the Slack manifest: slack_manifest: stdin is not a manifest", out)
        self.assertIn("rc=0 changed=false", out)

    def test_sources_without_the_record_skip_the_comparison(self):
        out = self.run_check(printed=_printed(_RECORD["manifests"]["assistant"]), repo_dir=self.tmp)
        self.assertIn("These sources carry no Slack manifest record", out)
        self.assertEqual(self.log.read_text(), "")
        self.assertIn("rc=0 changed=false", out)


if __name__ == "__main__":
    unittest.main()
