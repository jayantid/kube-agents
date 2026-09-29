"""Answer clicks on kube-agents' own Slack buttons.

Installed into the image at ``/opt/hermes/gateway/slack_ux_clicks.py``.
``apply_slack_ux_clicks.py`` makes the Slack adapter call :func:`register`
when it wires its Bolt listeners, only when ``KAGE_SLACK_UX`` is on. With the
flag off nothing here is registered and the adapter's listeners are upstream's.

Upstream, and why it changes
----------------------------
Hermes handles clicks on its own buttons (approvals, clarify, the model
picker) and on buttons a plugin registered. Nothing handles the buttons
``slack_presenter`` lays out, so Slack delivers the ``block_actions`` and the
click does nothing.

With the flag on, :func:`register` adds three listeners:

* A choice button (``<prefix>.choice.<n>``, whose value is its label) is the
  clicker answering in the thread. The click goes through the adapter's own
  interactive authorization; an unlisted user's click is logged and changes
  nothing. Then the message is rewritten with the choice buttons replaced by
  a line naming who chose what, a short echo ("↳ @user: label") is posted in
  the thread, since a bot token cannot post as the user, and the label is fed
  to the adapter's message handler as that user's message in that thread, the
  path a reaction trigger already takes. That path applies the channel and
  user checks a typed message gets, so a click can do nothing its clicker
  could not do by typing the label.
* The Stop button (``kage_stop``) is the same, with ``/stop`` as the text:
  the clicker typing ``/stop`` in the thread.
* A link button (``<prefix>.link.<n>``) is acknowledged and nothing else;
  Slack has already opened the url.

A message is answered once: the first authorized click wins, and a second
click on the same message, before the rewrite lands, is dropped in this
process. The fold needs no handler; it is a collapsible container that Slack
opens and closes itself.

Fail-soft throughout: a rewrite or echo that fails is logged and the turn
still runs, because the click was the user's answer.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

try:
    import slack_presenter as _presenter
except ImportError:  # the scripts directory is not on PYTHONPATH
    _presenter = None

#: The flag, read here only to word the warning when the presenter is missing.
FLAG_ENV = "KAGE_SLACK_UX"

#: ``slack_presenter.FLAG_ON_VALUES``, copied because the warning below fires
#: exactly when that module cannot be imported.
FLAG_ON_VALUES = frozenset({"1", "true", "yes", "on"})

#: What the adapter's authorization log calls each kind of click.
CHOICE_KIND = "kage choice"
STOP_KIND = "kage stop"

#: The text a Stop click feeds the thread, as if typed.
STOP_TEXT = "/stop"
#: The label a Stop click echoes.
STOP_LABEL = "stop"

#: The echo posted in the thread, and the line that replaces the answered buttons.
ECHO = "↳ <@{user}>: {label}"
ANSWERED = "✓ <@{user}>: {label}"

#: Slack mrkdwn control characters in a label, escaped before it is echoed so a
#: label cannot mention a user or a channel.
MRKDWN_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))

#: A synthetic message's ts when the payload carries no ``action_ts``.
FALLBACK_TS = "kage-click-{ts}-{action}-{user}"

#: Bound on the answered-message map, oldest evicted first.
ANSWERED_MAX = 512

_answered: OrderedDict[tuple, None] = OrderedDict()
_warned_missing = False


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on and the presenter is importable."""
    global _warned_missing
    if _presenter is not None:
        return _presenter.enabled()
    if os.environ.get(FLAG_ENV, "").strip().lower() in FLAG_ON_VALUES and not _warned_missing:
        _warned_missing = True
        logger.warning(
            "slack_ux_clicks: %s is set but slack_presenter is not importable; "
            "treating the flag as off", FLAG_ENV,
        )
    return False


def register(adapter: Any) -> None:
    """Add the choice, Stop and link listeners to ``adapter._app``."""

    async def on_choice(ack, body, action):
        await answer(adapter, ack, body, action, CHOICE_KIND)

    async def on_stop(ack, body, action):
        await answer(adapter, ack, body, action, STOP_KIND)

    adapter._app.action(_presenter.CHOICE_ACTION_ID_PATTERN)(on_choice)
    adapter._app.action(_presenter.STOP_ACTION_ID)(on_stop)
    adapter._app.action(_presenter.LINK_ACTION_ID_PATTERN)(_presenter.ack_link_click)
    logger.info("slack_ux_clicks: choice, stop and link button handlers registered")


def _escape(text: str) -> str:
    for raw, escaped in MRKDWN_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def _answered_by(action_id: str, kind: str) -> Any:
    """The predicate for the buttons one click answers: every choice, or the Stop."""
    if kind == STOP_KIND:
        return lambda other: other == _presenter.STOP_ACTION_ID
    return lambda other: bool(_presenter.CHOICE_ACTION_ID_PATTERN.search(other))


def answered_blocks(blocks: Any, answered: Any, note: str) -> list[dict]:
    """``blocks`` with the answered buttons dropped, and ``note`` as a context line after them.

    An actions block left with no buttons is dropped; one that still holds a
    link keeps it.
    """
    out: list[dict] = []
    for block in blocks or ():
        if isinstance(block, dict) and block.get("type") == "actions":
            elements = block.get("elements") or []
            kept = [e for e in elements if not answered(str((e or {}).get("action_id") or ""))]
            if not kept:
                continue
            if len(kept) != len(elements):
                block = {**block, "elements": kept}
        out.append(block)
    out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    return out


def _thread_ts(body: dict, message: dict, msg_ts: str) -> str:
    container = body.get("container") or {}
    return str(message.get("thread_ts") or container.get("thread_ts") or msg_ts)


async def answer(adapter: Any, ack: Any, body: dict, action: dict, kind: str) -> None:
    """Authorize a choice or Stop click, mark it answered, echo it, and run it as the clicker's turn."""
    started = await adapter._begin_interaction(ack, body, action, kind)
    if started is None:
        return
    team_id, action_id, value, message, msg_ts, channel_id, _user_name, user_id = started
    label = STOP_LABEL if kind == STOP_KIND else str(value or "").strip()
    if not (label and msg_ts and channel_id and user_id):
        return
    key = (channel_id, msg_ts, kind)
    if key in _answered:
        return
    _answered[key] = None
    while len(_answered) > ANSWERED_MAX:
        _answered.popitem(last=False)

    thread_ts = _thread_ts(body, message, msg_ts)
    shown = _escape(label)
    client = adapter._get_client(channel_id, team_id=team_id)
    note = ANSWERED.format(user=user_id, label=shown)
    try:
        await client.chat_update(
            channel=channel_id, ts=msg_ts, text=message.get("text") or note,
            blocks=answered_blocks(message.get("blocks"), _answered_by(action_id, kind), note),
        )
    except Exception as exc:  # noqa: BLE001 — the click still answers
        logger.warning("slack_ux_clicks: could not mark %s answered: %s", msg_ts, exc)
    try:
        await client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text=ECHO.format(user=user_id, label=shown),
        )
    except Exception as exc:  # noqa: BLE001 — the click still answers
        logger.warning("slack_ux_clicks: could not echo the click on %s: %s", msg_ts, exc)

    synthetic = {
        "type": "message",
        "user": user_id,
        "text": STOP_TEXT if kind == STOP_KIND else label,
        "channel": channel_id,
        # The click's own ts keeps the deduplicator from conflating this turn
        # with the echo or the clicked message, as a reaction trigger's does.
        "ts": str(action.get("action_ts") or FALLBACK_TS.format(ts=msg_ts, action=action_id, user=user_id)),
        "thread_ts": thread_ts,
        # Skips the mention requirement only; channel and user checks still apply.
        "_hermes_force_process": True,
        "_kage_click": {"action_id": action_id, "message_ts": msg_ts},
    }
    if team_id:
        synthetic["team"] = team_id
    await adapter._handle_slack_message(synthetic)
