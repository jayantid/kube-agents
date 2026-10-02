#!/usr/bin/env python3
"""The Slack app manifest kube-agents ships, as a record a build and an upgrade can compare.

``hermes slack manifest`` prints the JSON an operator pastes into the Slack App
Console, and nothing ever re-pastes it. An install that upgrades to an image whose
manifest asks for a new scope, event, or setting keeps running on the app it was
created with, and the feature that needed the change fails silently: Slack answers
``missing_scope`` or never delivers the event, and the agent looks slow rather than
misconfigured.

``slack_manifest.json`` beside this file is the record. ``manifests`` holds the
normalized manifest for each ``messaging_experience`` the image's CLI emits;
``changes`` is the ordered history, one entry per manifest the image has shipped,
each carrying its digests and the release-note line an upgrading operator is
shown. Two callers read it:

- ``verify_slack_manifest.py`` at image build: the manifest the patched CLI
  emits must equal ``manifests``, and the last ``changes`` entry must carry its
  digests and a note. A pull request that changes the manifest (an ``apply_slack_*.py``
  patch, or a Hermes pin whose ``slack_cli.py`` moved) therefore fails the build
  until it updates the record with the new digests. The build sees one tree, not
  its history, so appending an entry rather than rewriting the last one is held
  by review; ``changes`` is append-only by convention.
- ``upgrade.sh``, through ``compare``: the running image's manifests, one per
  experience, against the record in the checkout being upgraded to. The app was
  created from one of them and nothing records which, so every experience that
  differs is reported, labelled.

Normalizing drops what does not need the app re-applied: metadata, display
information (the name and description are the operator's), the bot user's
display name, the view descriptions (cosmetic, and one varies with a feature
flag), and the slash-command list, which ``slack_native_slashes`` builds from
whatever plugins are loaded and so is not a property of the image alone. Scope
and event lists are sorted; Slack treats them as sets.

Standard library only, and Python 3.9 syntax: ``upgrade.sh`` runs ``compare``
with the operator's ``python3``.

Usage::

    hermes slack manifest | slack_manifest.py normalize
    hermes slack manifest | slack_manifest.py compare slack_manifest.json
    echo '{"assistant": {...}, "agent": {...}, "none": {...}}' |
        slack_manifest.py compare slack_manifest.json

``compare`` reads either one manifest, taken as the default experience, or an
object holding a manifest per experience name.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

RECORD = Path(__file__).with_name("slack_manifest.json")

#: Every messaging experience ``_build_full_manifest`` can emit.
EXPERIENCES = ("assistant", "agent", "none")

#: What ``hermes slack manifest`` prints with no flag, and so what ``compare``
#: takes a bare manifest on stdin to be.
DEFAULT_EXPERIENCE = "assistant"

DROPPED_TOP_LEVEL = ("_metadata", "display_information")
DROPPED_FEATURES = ("bot_user", "slash_commands")
VIEW_FEATURE_SUFFIX = "_view"
BOT_SCOPES = ("oauth_config", "scopes", "bot")
BOT_EVENTS = ("settings", "event_subscriptions", "bot_events")
SET_VALUED_PATHS = (BOT_SCOPES, BOT_EVENTS)

#: Settings a kube-agents install cannot work without. Interactivity carries
#: every button click (approvals, needs-input choices, task cards), Socket Mode
#: is the only transport the gateway speaks, and assistant:write is what the
#: assistant and agent views post through. No other build gate asserts them, and
#: losing any one fails without an error.
REQUIRED_TRUE_SETTINGS = (
    ("settings", "interactivity", "is_enabled"),
    ("settings", "socket_mode_enabled"),
)
REQUIRED_VIEW_SCOPE = "assistant:write"
EXPERIENCES_WITH_VIEW = ("assistant", "agent")

DIGEST_LENGTH = 12

EXIT_SAME = 0
EXIT_DIFFERENT = 1
EXIT_USAGE = 2


def normalize(raw: dict) -> dict:
    """Return ``raw`` with everything that needs no re-apply removed, lists sorted."""
    manifest = copy.deepcopy(raw)
    for key in DROPPED_TOP_LEVEL:
        manifest.pop(key, None)
    features = manifest.get("features")
    if isinstance(features, dict):
        for key in DROPPED_FEATURES:
            features.pop(key, None)
        for key in list(features):
            if key.endswith(VIEW_FEATURE_SUFFIX):
                features[key] = {}
    for path in SET_VALUED_PATHS:
        parent = _lookup(manifest, path[:-1])
        if isinstance(parent, dict) and isinstance(parent.get(path[-1]), list):
            parent[path[-1]] = sorted(set(parent[path[-1]]))
    return manifest


def digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:DIGEST_LENGTH]


def expected_change(manifests: dict) -> dict:
    """The digests the newest ``changes`` entry must carry for ``manifests``."""
    return {
        "digest": digest(manifests),
        "assistant_digest": digest(manifests[DEFAULT_EXPERIENCE]),
    }


def missing_requirements(manifests: dict) -> list[str]:
    """Name every required setting a normalized set of manifests lacks."""
    problems = []
    for experience, manifest in sorted(manifests.items()):
        for path in REQUIRED_TRUE_SETTINGS:
            if _lookup(manifest, path) is not True:
                problems.append(f"{experience}: {'.'.join(path)} is not true")
        scopes = _lookup(manifest, BOT_SCOPES) or []
        if experience in EXPERIENCES_WITH_VIEW and REQUIRED_VIEW_SCOPE not in scopes:
            problems.append(f"{experience}: bot scopes lack {REQUIRED_VIEW_SCOPE}")
    return problems


def record_problems(record: dict) -> list[str]:
    """Check the record against itself: every manifest it holds has a release note."""
    manifests = record.get("manifests")
    changes = record.get("changes")
    if not isinstance(manifests, dict) or sorted(manifests) != sorted(EXPERIENCES):
        return [f"manifests must hold exactly {', '.join(EXPERIENCES)}"]
    if not isinstance(changes, list) or not changes:
        return ["changes must be a non-empty list"]
    problems = []
    for index, change in enumerate(changes):
        if not str(change.get("note", "")).strip():
            problems.append(f"changes[{index}] has no note")
        if not change.get("digest") or not change.get("assistant_digest"):
            problems.append(f"changes[{index}] lacks digest or assistant_digest")
    # A revert may return to an earlier manifest, so a digest can recur; two
    # adjacent entries with one digest record no change at all.
    for index in range(1, len(changes)):
        if changes[index].get("digest") == changes[index - 1].get("digest"):
            problems.append(f"changes[{index}] repeats the digest of the entry before it")
    for key, value in expected_change(manifests).items():
        if changes[-1].get(key) != value:
            problems.append(
                f"the last changes entry has {key} {changes[-1].get(key)!r}, but "
                f"manifests digest to {value!r}: append a changes entry carrying it "
                "and a note saying what changed and what re-applying the manifest gains"
            )
    return problems + missing_requirements(manifests)


def differences(installed: dict, target: dict) -> list[str]:
    """One line per scope, event, feature or setting the target adds, drops or changes."""
    before, after = _flatten(installed), _flatten(target)
    lines = []
    for key in sorted(set(before) | set(after), key=lambda k: (k[0], k[1] or "")):
        if key not in before:
            lines.append(f"+ {_describe(key)}")
        elif key not in after:
            lines.append(f"- {_describe(key)}")
        elif before[key] != after[key]:
            lines.append(f"~ {key[0]}: {before[key]} -> {after[key]}")
    return lines


def compare(installed_raw: dict, record: dict) -> tuple[int, str]:
    """Diff the running manifests against the record's, with the notes since them.

    ``installed_raw`` is one manifest (the default experience) or a dict of them
    keyed by experience. The running version is looked up by the digest of all
    three when all three are given, else by the default experience's alone.

    The newest entry is never the running version: a report means something
    differs from it. Without all three, entries sharing a default manifest
    cannot be told apart, so the notes start after the earliest of them and the
    heading says so; one manifest that changed without the default would
    otherwise match the newest entry and print no note at all.
    """
    if any(key in installed_raw for key in EXPERIENCES):
        unknown = sorted(set(installed_raw) - set(EXPERIENCES))
        if unknown or DEFAULT_EXPERIENCE not in installed_raw:
            raise ValueError(
                f"stdin must hold {DEFAULT_EXPERIENCE!r} and no key outside "
                f"{', '.join(EXPERIENCES)}"
            )
        raws = installed_raw
    else:
        raws = {DEFAULT_EXPERIENCE: installed_raw}
    installed = {experience: normalize(raw) for experience, raw in raws.items()}
    report = []
    for experience in EXPERIENCES:
        if experience not in installed:
            continue
        lines = differences(installed[experience], record["manifests"][experience])
        if lines:
            report.append(
                f"The {experience!r} experience's manifest differs from the running one "
                "(+ added, - removed, ~ changed):"
            )
            report.extend(f"  {line}" for line in lines)
    if not report:
        return EXIT_SAME, ""
    changes = record["changes"]
    full = sorted(installed) == sorted(EXPERIENCES)
    if full:
        key, installed_digest = "digest", digest(installed)
    else:
        key, installed_digest = "assistant_digest", digest(installed[DEFAULT_EXPERIENCE])
    seen = [
        index for index, change in enumerate(changes[:-1]) if change.get(key) == installed_digest
    ]
    if seen and (full or len(seen) == 1):
        notes = changes[seen[-1] + 1 :]
        report.append("What changed since the running version:")
    elif seen:
        notes = changes[seen[0] + 1 :]
        report.append(
            "Not every running manifest could be read, and several recorded versions "
            "share its default one, so the notes start after the earliest of them:"
        )
    else:
        notes = changes
        report.append("The running manifest matches no recorded version, so every note follows:")
    report.extend(f"  - {change['note']}" for change in notes)
    return EXIT_DIFFERENT, "\n".join(report)


def _lookup(value: object, path: tuple) -> object:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _flatten(value: object, prefix: str = "") -> dict:
    """Map every leaf to its value; a list becomes one key per element."""
    leaves: dict = {}
    if isinstance(value, dict) and value:
        for key, child in value.items():
            leaves.update(_flatten(child, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list):
        for item in value:
            leaves[(prefix, json.dumps(item, sort_keys=True))] = True
    else:
        leaves[(prefix, None)] = json.dumps(value, sort_keys=True)
    return leaves


def _describe(key: tuple) -> str:
    path, element = key
    return path if element is None else f"{path}: {json.loads(element)}"


def _read_manifest(stream) -> dict:
    try:
        value = json.load(stream)
    except json.JSONDecodeError as error:
        raise ValueError(f"stdin is not a manifest: {error}") from error
    if not isinstance(value, dict):
        raise TypeError("stdin is not a JSON object")
    return value


def main(argv: list[str]) -> int:
    command = argv[:1]
    try:
        if command == ["normalize"] and len(argv) == 1:
            print(json.dumps(normalize(_read_manifest(sys.stdin)), indent=2, sort_keys=True))
            return EXIT_SAME
        if command == ["compare"] and len(argv) == 2:
            record = json.loads(Path(argv[1]).read_text())
            status, report = compare(_read_manifest(sys.stdin), record)
            if report:
                print(report)
            return status
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"slack_manifest: {error}", file=sys.stderr)
        return EXIT_USAGE
    print(__doc__.rsplit("Usage::", 1)[-1].strip("\n"), file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
