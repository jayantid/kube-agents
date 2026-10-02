#!/usr/bin/env python3
"""Build-time gate: the Slack manifest the image emits is the one ``slack_manifest.json`` records.

Run by ``deploy/docker/Dockerfile`` last in the message-surface chain, after every
``apply_slack_*.py`` patch, against the patched ``/opt/hermes`` tree. It calls the
real ``_build_full_manifest`` once per messaging experience, normalizes each the way
``slack_manifest.py`` does, and fails the build when:

- the result differs from the record's ``manifests``. An ``apply_slack_*.py``
  patch or a Hermes pin that changes the manifest lands only with the record
  updated, which is what lets ``upgrade.sh`` tell an operator to re-apply it;
- the record's newest ``changes`` entry does not carry the manifests' digests, or
  has no note. That entry is the manifest's release-note line, so a change to the
  manifest cannot merge without one;
- an entry repeats the digest of the entry before it, which would be a second
  note for a manifest that did not change (a return to an earlier manifest is
  fine);
- the interactivity and Socket Mode settings, or ``assistant:write`` on the two
  experiences with a view, are gone. Losing any of them breaks a kube-agents
  install without an error, and nothing else asserts them;
- the manifest depends on ``KAGE_SLACK_UX``. ``upgrade.sh`` compares one manifest
  per experience, and a flag-dependent one has no single right answer.

On a mismatch it prints the normalized manifests and the digests the new entry
needs, so updating the record is a paste plus a sentence.

The unit suite cannot cover this: the edit lives in Hermes' own module, and the
host never sees the tree that ships. ``test_slack_manifest.py`` covers the record
and the comparison logic on the host.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import slack_manifest

MANIFEST = "hermes_cli/slack_cli.py"
FLAG = "KAGE_SLACK_UX"
FLAG_STATES = (None, "1")
# The name and description the emitted manifests are built with; normalizing
# drops both, so any value compares the same.
BOT_NAME = "Hermes"
BOT_DESCRIPTION = "manifest verify"


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_manifest verify: {detail}")


def _load(root: Path):
    path = root / MANIFEST
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("slack_cli_manifest_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def emitted(module) -> dict:
    """Normalized manifests per experience, checked to be the same with the flag off and on."""
    saved = os.environ.get(FLAG)
    results = []
    try:
        for state in FLAG_STATES:
            if state is None:
                os.environ.pop(FLAG, None)
            else:
                os.environ[FLAG] = state
            results.append({
                experience: slack_manifest.normalize(
                    module._build_full_manifest(
                        BOT_NAME, BOT_DESCRIPTION, messaging_experience=experience
                    )
                )
                for experience in slack_manifest.EXPERIENCES
            })
    finally:
        if saved is None:
            os.environ.pop(FLAG, None)
        else:
            os.environ[FLAG] = saved
    if any(result != results[0] for result in results[1:]):
        raise _fail(
            f"the emitted manifest changes with {FLAG}, so upgrade.sh cannot compare "
            "a running install against one record; keep flag-dependent edits to the "
            "view descriptions, which normalizing drops"
        )
    return results[0]


def main(root: Path = Path("/opt/hermes"), record_path: Path = slack_manifest.RECORD) -> None:
    record = json.loads(record_path.read_text())
    manifests = emitted(_load(root))

    if manifests != record.get("manifests"):
        recorded = record.get("manifests") or {}
        diff = [
            f"  {experience}: {line}"
            for experience in slack_manifest.EXPERIENCES
            for line in slack_manifest.differences(recorded.get(experience, {}), manifests[experience])
        ]
        raise _fail(
            f"the manifest this image emits differs from {record_path.name}:\n"
            + "\n".join(diff)
            + f"\nReplace its manifests with:\n{json.dumps(manifests, indent=2, sort_keys=True)}\n"
            + "and append a changes entry carrying "
            + json.dumps(slack_manifest.expected_change(manifests), sort_keys=True)
            + " and a note saying what changed and what re-applying the manifest gains."
        )

    problems = slack_manifest.record_problems(record)
    if problems:
        raise _fail(f"{record_path.name}: " + "; ".join(problems))

    print(
        f"slack_manifest verify: all {len(slack_manifest.EXPERIENCES)} emitted manifests "
        f"match {record_path.name} with {FLAG} off and on; the newest changes entry "
        "carries their digests and a note"
    )


if __name__ == "__main__":
    main(
        Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/opt/hermes"),
        Path(sys.argv[2]) if len(sys.argv) > 2 else slack_manifest.RECORD,
    )
