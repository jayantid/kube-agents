#!/usr/bin/env python3
"""Open the Slack DM on agent view, with suggested prompts, when ``KAGE_SLACK_UX`` is on.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree, after
``apply_slack_reactions_scope.py``, which edits the same manifest builder.

What upstream already does
--------------------------
``hermes slack manifest`` prints the only description of the Slack app an
installer writes. Its ``messaging_experience`` has three branches: ``assistant``
(the default), ``agent`` (``--agent-view``) and ``none`` (``--no-assistant``).
The ``agent`` branch adds ``features.agent_view``, ``assistant:write`` and the
``app_context_changed``/``app_home_opened`` events. The adapter answers
``app_home_opened`` on the Messages tab (and ``assistant_thread_started`` on
assistant view) with ``assistant.threads.setSuggestedPrompts``, reading the
prompts from ``platforms.slack.extra.suggested_prompts``. Nothing in this
repository sets that key, so no install shows a prompt.

What this changes, with the flag on
-----------------------------------
* ``hermes slack manifest`` with neither ``--agent-view`` nor ``--no-assistant``
  emits the ``agent`` branch instead of ``assistant``.
* Every branch subscribes ``agent_session_stopped``. Without it Slack shows no
  Stop on a working agent session and warns
  ``missing_agent_session_stopped_event_subscription`` on every
  ``agents.sessions.setStatus`` (the Slack probe, 2026-09-29). The event needs
  only ``chat:write``. Handling the stop request is the status work's; until a
  listener exists the adapter's catch-all acks it.
* The adapter falls back to :data:`SUGGESTED_PROMPTS` when
  ``suggested_prompts`` is unset. A configured value still wins.

With the flag off each edit is a branch that is not taken: the manifest and the
prompts are exactly upstream's.

The scopes the Slack UX work spends (``reactions:write``, ``users:read``,
``files:write``) are not added here: upstream's list carries the last two and
``apply_slack_reactions_scope.py`` adds the first. ``verify_slack_agent_view.py``
asserts all three in the emitted manifest.

Usage::

    python3 apply_slack_agent_view.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

MANIFEST = "hermes_cli/slack_cli.py"
ADAPTER = "plugins/platforms/slack/adapter.py"
PREFIX = "slack_agent_view"

FLAG_ENV = "KAGE_SLACK_UX"
FLAG_ON_VALUES = ("1", "true", "yes", "on")

STOP_EVENT = "agent_session_stopped"

#: Mock 01's three asks, without the dev fleet's cluster name: a static prompt
#: cannot know which clusters an install has. Slack sends the ``message`` as
#: the user when the prompt is tapped.
SUGGESTED_PROMPTS = (
    "is anything unhealthy in my clusters right now?",
    "what's on the board?",
    "which clusters are behind their release channel?",
)

#: The helper both files gain. Its name is the guard against a second run.
FLAG_HELPER = "_kage_slack_ux_on"
BUILD_MARKER = f"def {FLAG_HELPER}("

HELPER = f'''

# kube-agents patch: slack_agent_view (deploy/docker/patches/apply_slack_agent_view.py).
def {FLAG_HELPER}() -> bool:
    import os as _os

    return _os.environ.get({FLAG_ENV!r}, "").strip().lower() in {FLAG_ON_VALUES!r}
'''

DEFAULT_EXPERIENCE_ANCHOR = '''\
    else:
        messaging_experience = "assistant"
'''
DEFAULT_EXPERIENCE = f'''\
    else:
        messaging_experience = "agent" if {FLAG_HELPER}() else "assistant"
'''

SORT_ANCHOR = '''\
    bot_scopes.sort()
    bot_events.sort()
'''
SORT = f'''\
    if {FLAG_HELPER}():
        bot_events.append({STOP_EVENT!r})
{SORT_ANCHOR}'''

PROMPTS_ANCHOR = '''\
        raw = self.config.extra.get("suggested_prompts")
'''
PROMPTS = f'''\
{PROMPTS_ANCHOR}\
        if raw is None and {FLAG_HELPER}():
            raw = _KAGE_SUGGESTED_PROMPTS
'''
PROMPTS_CONSTANT = (
    "_KAGE_SUGGESTED_PROMPTS = "
    + repr([{"title": prompt, "message": prompt} for prompt in SUGGESTED_PROMPTS])
    + "\n"
)


def apply(root: Path) -> None:
    """Patch both files under ``root``, or raise SystemExit with the reason."""
    manifest = patchlib.Patch(root, MANIFEST, prefix=PREFIX)
    manifest.refuse_if_patched(BUILD_MARKER)
    manifest.find_def("slack_manifest_command", label="manifest command")
    manifest.find_def("_build_full_manifest", label="manifest builder")
    manifest.substitute(
        DEFAULT_EXPERIENCE_ANCHOR, DEFAULT_EXPERIENCE, label="default messaging experience"
    )
    manifest.substitute(SORT_ANCHOR, SORT, label="bot scope and event sort")
    manifest.append(HELPER)

    adapter = patchlib.Patch(root, ADAPTER, prefix=PREFIX)
    adapter.refuse_if_patched(BUILD_MARKER)
    adapter.substitute(PROMPTS_ANCHOR, PROMPTS, label="suggested prompts config read")
    adapter.append(HELPER + PROMPTS_CONSTANT)

    manifest.commit(f"agent view by default and {STOP_EVENT} when {FLAG_ENV} is on")
    adapter.commit(f"default suggested prompts when {FLAG_ENV} is on")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
