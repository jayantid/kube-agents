"""Post the moments a Slack thread should not miss as messages of their own.

Installed into the image at ``/opt/hermes/gateway/slack_ux_moments.py``.
``gateway/kanban_progress_lines.py`` calls it for a Slack card when
``KAGE_SLACK_UX`` is on; with the flag off nothing reaches it. What the
messages look like is ``agents/platform/scripts/slack_moments.py``.

Upstream, and why it changes
----------------------------
**A pull request opened.** A worker that opens a PR says so in a progress note
or its report, so the link sits on a plan row or inside the report, where it
reads like any other step. With the flag on, :func:`pr_opened` also posts it
as its own message with "Open PR" and "Files changed" buttons, once per PR per
thread, since a note and the report often both carry it.

**A question the work waits on.** A card blocked with ``kind: needs_input``
reaches the thread as "⏸ <head> blocked: <reason>", clipped to 160
characters, and with the flag on that line is held for the Planning Agent's
wake to explain. With the flag on, :func:`needs_you` instead posts the whole
reason as a question: the first line in bold, the rest below, a choice button
per option the reason lists, and "waiting on you". The wake is untouched, so
the Planning Agent still learns the card is blocked and still takes the
answer, typed or clicked (``gateway/slack_ux_clicks.py``), to the card with
``kanban_comment`` and ``kanban_unblock``. Other block kinds keep the line.

Fail-soft: a moment that cannot be posted is logged, and the caller falls back
to what it did before.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

try:
    import slack_moments as _moments
    import slack_presenter as _presenter
except ImportError:  # the scripts directory is not on PYTHONPATH
    _moments = None
    _presenter = None

#: The flag, read here only to word the warning when the renderer is missing.
FLAG_ENV = "KAGE_SLACK_UX"

#: ``slack_presenter.FLAG_ON_VALUES``, copied because the warning below fires
#: exactly when that module cannot be imported.
FLAG_ON_VALUES = frozenset({"1", "true", "yes", "on"})

#: The block kind that asks the user something; the others are not questions.
NEEDS_INPUT = "needs_input"

#: Bound on the announced-PR map, oldest evicted first.
ANNOUNCED_MAX = 512

#: ``(channel, pr url)`` already posted.
_announced: OrderedDict[tuple, None] = OrderedDict()
_warned_missing = False


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on and the presenter and renderer are importable."""
    global _warned_missing
    if _presenter is not None and _moments is not None:
        return _presenter.enabled()
    if os.environ.get(FLAG_ENV, "").strip().lower() in FLAG_ON_VALUES and not _warned_missing:
        _warned_missing = True
        logger.warning(
            "slack_ux_moments: %s is set but slack_presenter or slack_moments is not "
            "importable; treating the flag as off", FLAG_ENV,
        )
    return False


async def _post(adapter: Any, sub: dict, blocks: list[dict], text: str) -> bool:
    chat_id = str(sub.get("chat_id") or "")
    if not (chat_id and hasattr(adapter, "_get_client")):
        return False
    try:
        client = adapter._get_client(chat_id, team_id=sub.get("team_id") or None)
        await client.chat_postMessage(
            channel=chat_id, thread_ts=sub.get("thread_id") or None, text=text, blocks=blocks,
        )
    except Exception as exc:  # noqa: BLE001 — the caller falls back
        logger.warning("slack_ux_moments: posting in %s failed: %s", chat_id, exc)
        return False
    return True


async def pr_opened(adapter: Any, sub: dict, text: str) -> bool:
    """Post the PR ``text`` says was opened, once per thread; True when posted."""
    found = _moments.opened_pr(text)
    if found is None:
        return False
    url, repo, number, line = found
    key = (str(sub.get("chat_id") or ""), url)
    if key in _announced:
        return False
    blocks, fallback = _moments.pr_opened(url, repo, number, line)
    if not await _post(adapter, sub, blocks, fallback):
        return False
    _announced[key] = None
    while len(_announced) > ANNOUNCED_MAX:
        _announced.popitem(last=False)
    return True


async def needs_you(adapter: Any, sub: dict, payload: Any) -> bool:
    """Post a ``needs_input`` block's reason as a question; True when posted.

    False for any other block, an empty reason, or a post that failed, and the
    caller delivers the blocked line as before.
    """
    if not isinstance(payload, dict) or payload.get("kind") != NEEDS_INPUT:
        return False
    moment = _moments.needs_you(str(payload.get("reason") or ""))
    if moment is None:
        return False
    return await _post(adapter, sub, *moment)
