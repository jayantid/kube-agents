"""Tests for upgrade.sh's Slack manifest step, against a fake kubectl.

``check_slack_manifest`` reads the running image's manifest through
``kubectl exec … hermes slack manifest`` and compares it with the record in the
sources being upgraded to. The comparison itself is covered by
``deploy/docker/patches/test_slack_manifest.py``; these cover the shell around
it: when it runs, what it prints, and that nothing it meets stops the upgrade.
"""

import copy
import json
import os
import pathlib
import re
import shlex
import signal
import subprocess
import tempfile
import time
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_UPGRADE_SH = _REPO_ROOT / "upgrade.sh"
_COMMON_SH = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"
_RECORD = json.loads((_REPO_ROOT / "deploy" / "docker" / "patches" / "slack_manifest.json").read_text())

_FAKE_KUBECTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_KUBECTL_LOG"
[ -z "${FAKE_KUBECTL_PIDFILE:-}" ] || echo "$$" > "$FAKE_KUBECTL_PIDFILE"
[ -z "${FAKE_KUBECTL_SLEEP:-}" ] || exec sleep "$FAKE_KUBECTL_SLEEP"
case "$*" in
  *--agent-view*) out="${FAKE_KUBECTL_OUTPUT_agent:-}" ;;
  *--no-assistant*) out="${FAKE_KUBECTL_OUTPUT_none:-}" ;;
  *) out="${FAKE_KUBECTL_OUTPUT_assistant:-}" ;;
esac
[ -n "$out" ] || exit 1
cat "$out"
"""

_EXEC = ["exec", "deployment/platform-agent-gateway", "-c", "platform-agent", "-n", "agents-ns", "--", "hermes", "slack", "manifest"]


def _printed(normalized):
    """A manifest as ``hermes slack manifest`` prints it."""
    raw = copy.deepcopy(normalized)
    raw["display_information"] = {"name": "Hermes", "description": "Your Hermes agent on Slack"}
    raw["features"]["bot_user"] = {"display_name": "Hermes", "always_online": True}
    raw["features"]["slash_commands"] = [{"command": "/stop"}]
    return raw


def _all_printed():
    return {experience: _printed(manifest) for experience, manifest in _RECORD["manifests"].items()}


def _without_reactions(experience):
    older = copy.deepcopy(_RECORD["manifests"][experience])
    older["oauth_config"]["scopes"]["bot"].remove("reactions:write")
    return older


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


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

    def script(self, printed=None, repo_dir=_REPO_ROOT, timeout_seconds=None):
        """``printed`` maps an experience to the text the fake prints for it; a missing one fails."""
        for experience, text in (printed or {}).items():
            output = self.tmp / f"{experience}.json"
            output.write_text(text if isinstance(text, str) else json.dumps(text))
            self.env[f"FAKE_KUBECTL_OUTPUT_{experience}"] = str(output)
        override = "" if timeout_seconds is None else f"SLACK_MANIFEST_READ_TIMEOUT_SECONDS={timeout_seconds}\n"
        return (
            f"KUBE_AGENTS_SOURCE_ONLY=true source {shlex.quote(str(_UPGRADE_SH))}\n"
            f"source {shlex.quote(str(_COMMON_SH))}\n"
            f"{override}"
            f"check_slack_manifest agents-ns {shlex.quote(str(repo_dir))}\n"
            'echo "rc=$? changed=$SLACK_MANIFEST_CHANGED uncompared=$SLACK_MANIFEST_UNCOMPARED"\n'
        )

    def run_check(self, printed=None, repo_dir=_REPO_ROOT, env=None, timeout_seconds=None):
        script = self.script(printed, repo_dir, timeout_seconds)
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
        out = self.run_check(printed=_all_printed(), env={"SLACK_ENABLED": "false"})
        self.assertIn("rc=0 changed=false", out)
        self.assertEqual(self.log.read_text(), "")
        self.assertNotIn("Slack", out)

    def test_the_same_manifests_say_so(self):
        out = self.run_check(printed=_all_printed())
        self.assertIn("This version does not change the Slack app's manifest.", out)
        self.assertNotIn("not compared", out)
        self.assertIn("rc=0 changed=false", out)
        self.assertEqual(
            [line.split() for line in self.log.read_text().splitlines()],
            [_EXEC, [*_EXEC, "--agent-view"], [*_EXEC, "--no-assistant"]],
        )

    def test_a_changed_manifest_prints_the_diff_the_notes_and_the_steps(self):
        out = self.run_check(printed={**_all_printed(), "assistant": _printed(_without_reactions("assistant"))})
        self.assertIn("This version changes the Slack app's manifest.", out)
        self.assertIn("The 'assistant' experience's manifest differs", out)
        self.assertIn("      + oauth_config.scopes.bot: reactions:write", out)
        self.assertIn(_RECORD["changes"][-1]["note"], out)
        self.assertIn(
            "1. kubectl exec deploy/platform-agent-gateway -c platform-agent -n agents-ns -- hermes slack manifest", out
        )
        self.assertIn("--agent-view or --no-assistant", out)
        self.assertIn("Reinstall the app", out)
        self.assertIn("rc=0 changed=true", out)

    def test_a_change_to_another_experience_is_reported_too(self):
        out = self.run_check(printed={**_all_printed(), "agent": _printed(_without_reactions("agent"))})
        self.assertIn("The 'agent' experience's manifest differs", out)
        self.assertNotIn("The 'assistant' experience", out)
        self.assertIn("rc=0 changed=true", out)

    def test_an_image_that_cannot_print_the_other_experiences_compares_the_default(self):
        out = self.run_check(printed={"assistant": _printed(_RECORD["manifests"]["assistant"])})
        self.assertIn("did not print a readable --agent-view manifest, so that experience is not compared", out)
        self.assertIn("did not print a readable --no-assistant manifest, so that experience is not compared", out)
        self.assertNotIn("This version does not change the Slack app's manifest.", out)
        self.assertIn(
            "does not change the Slack manifests the running image printed, but an app created with "
            "--agent-view or --no-assistant was not compared. If yours was, re-apply its manifest.",
            out,
        )
        self.assertIn("Reinstall the app", out)
        self.assertIn("rc=0 changed=false uncompared=--agent-view or --no-assistant", out)

    def test_a_variant_that_prints_no_manifest_leaves_the_rest_compared(self):
        printed = {**_all_printed(), "assistant": _printed(_without_reactions("assistant")), "none": "Usage: hermes"}
        out = self.run_check(printed=printed)
        self.assertIn("did not print a readable --no-assistant manifest", out)
        self.assertNotIn("--agent-view manifest", out)
        self.assertIn("      + oauth_config.scopes.bot: reactions:write", out)
        self.assertIn("rc=0 changed=true", out)

    def test_an_interrupted_read_leaves_no_exec_or_temp_dir_behind(self):
        for name, sig in (("SIGINT", signal.SIGINT), ("SIGTERM", signal.SIGTERM)):
            with self.subTest(signal=name):
                tmpdir = self.tmp / name
                tmpdir.mkdir()
                pidfile = self.tmp / f"{name}.pid"
                proc = subprocess.Popen(
                    ["bash", "-c", self.script(printed=_all_printed())],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={**self.env, "TMPDIR": str(tmpdir), "FAKE_KUBECTL_SLEEP": "30",
                         "FAKE_KUBECTL_PIDFILE": str(pidfile)},
                    cwd=str(_REPO_ROOT),
                    start_new_session=True,
                )
                deadline = time.monotonic() + 10
                while not (pidfile.exists() and pidfile.read_text().strip()) and time.monotonic() < deadline:
                    time.sleep(0.05)
                kubectl_pid = int(pidfile.read_text())
                # Ctrl-C signals the whole foreground group; SIGTERM comes to the script alone.
                if sig == signal.SIGINT:
                    os.killpg(proc.pid, sig)
                else:
                    proc.send_signal(sig)
                proc.wait(timeout=10)
                deadline = time.monotonic() + 5
                while _alive(kubectl_pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(_alive(kubectl_pid), "the exec outlived the run")
                self.assertEqual(list(tmpdir.iterdir()), [])

    def test_a_pod_it_cannot_exec_into_does_not_stop_the_upgrade(self):
        out = self.run_check()
        self.assertIn("Could not read the running Slack manifest", out)
        self.assertIn("rc=0 changed=false", out)

    def test_a_wedged_exec_is_killed(self):
        started = time.monotonic()
        out = self.run_check(printed=_all_printed(), env={"FAKE_KUBECTL_SLEEP": "30"}, timeout_seconds=1)
        self.assertLess(time.monotonic() - started, 20)
        self.assertIn("Could not read the running Slack manifest", out)
        self.assertIn("rc=0 changed=false", out)

    def test_output_that_is_not_a_manifest_does_not_stop_the_upgrade(self):
        out = self.run_check(printed={"assistant": "error: unable to upgrade connection"})
        self.assertIn("Could not compare the Slack manifest: slack_manifest: stdin is not a manifest", out)
        self.assertIn("rc=0 changed=false", out)

    def test_sources_without_the_record_skip_the_comparison(self):
        out = self.run_check(printed=_all_printed(), repo_dir=self.tmp)
        self.assertIn("These sources carry no Slack manifest record", out)
        self.assertEqual(self.log.read_text(), "")
        self.assertIn("rc=0 changed=false", out)


class SlackManifestGatingTest(unittest.TestCase):
    """Where main() runs the check, read from the script source."""

    def test_the_check_runs_only_when_the_agent_image_moves_outside_operator_mode(self):
        main = _UPGRADE_SH.read_text().split("\nmain() {\n", 1)[1]
        moves = main.index('[ -n "$PARAM_IMAGE_TAG" ] || image_moves="false"')
        call = re.search(
            r'if \[ "\$image_moves" = "true" \] && \[ "\$PARAM_UPGRADE_MODE" != "operator" \]; then\n'
            r'    check_slack_manifest "\$target_namespace" "\$repo_dir"\n',
            main,
        )
        self.assertIsNotNone(call)
        # Before the plan branch exits, so a plan with a tag reports too.
        self.assertLess(moves, call.start())
        self.assertLess(call.start(), main.index('  if [ "$PARAM_PLAN" = "true" ]; then\n    # Both backfills PATCH'))

    def test_the_steps_are_repeated_after_the_upgrade_completes(self):
        main = _UPGRADE_SH.read_text().split("\nmain() {\n", 1)[1]
        self.assertIn(
            '  print_step "🎉 Upgrade Complete!"\n'
            '  if [ "$SLACK_MANIFEST_CHANGED" = "true" ]; then\n'
            "    print_warning \"The Slack app still has the previous version's manifest.\"\n"
            '    print_slack_manifest_steps "$target_namespace"\n'
            '  elif [ -n "$SLACK_MANIFEST_UNCOMPARED" ]; then\n'
            '    print_warning "The Slack manifest of an app created with ${SLACK_MANIFEST_UNCOMPARED} was not compared."\n'
            '    print_slack_manifest_steps "$target_namespace"\n',
            main,
        )


if __name__ == "__main__":
    unittest.main()
