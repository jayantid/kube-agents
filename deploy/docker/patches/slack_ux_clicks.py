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

With the flag on, :func:`register` adds two listeners:

* A choice button (``<prefix>.choice.<n>``) is the clicker answering in the
  thread with the button's text as Slack showed it. Not its ``value``: the
  presenter clips the text to Slack's 75 characters but keeps up to 2000 in
  the value, and a click must not send words the clicker did not see.
  The one exception keeps that rule: a value that is the label, ": ", and a
  line the message itself shows ("Fix the first one: <the first row>") is
  the turn, so a session that never read the message is told what the click
  is about. The echo and the answered line still show the label. A
  label that starts like a command (``/`` or ``!``) is sent as text, since a
  choice is an answer. The click goes through the adapter's own interactive
  authorization; an unlisted user's click is logged and changes nothing, and
  so does one in a channel or DM the adapter would ignore a typed message in.
  Then the message is rewritten with the choice buttons replaced by
  a line naming who chose what, a short echo ("↳ @user: label") is posted in
  the thread, since a bot token cannot post as the user, and the label is fed
  to the adapter's message handler as that user's message in that thread, the
  path a reaction trigger already takes. That path applies the channel and user checks a typed message gets, so a click
  can do nothing its clicker could not do by typing the label.
* A link button (``<prefix>.link.<n>``) is acknowledged and nothing else;
  Slack has already opened the url.

When the clicked message is a card's question (``gateway/slack_ux_moments.py``),
the turn also names the card, after the label or the shown line its value
names, so with two cards blocked in one thread the
answer reaches the right one, and the rewrite drops its "waiting on you" line.

A message is answered once: the first authorized click wins, and a second
click on the same message, before the rewrite lands, is dropped in this
process and logged. If the rewrite fails, the buttons stay on the message but
the click still counts: its turn runs, and a later click on them is dropped and
logged rather than running a second apply. That memory is this process's and
holds the last ``ANSWERED_MAX`` answers, so only a failed rewrite followed by a
gateway restart, or by that many later answers, lets the leftover buttons run
again.
:func:`answered` reports a message only once its rewrite landed, so a question
whose rewrite failed is still settled when its card moves on.

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

#: The echo posted in the thread, and the line that replaces the answered buttons.
ECHO = "↳ <@{user}>: {label}"
ANSWERED = "✓ <@{user}>: {label}"

#: Joins a label to the shown line its value names.
TURN_JOIN = ": "

#: Slack mrkdwn control characters in a label, escaped before it is echoed so a
#: label cannot mention a user or a channel.
MRKDWN_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))

#: A choice label starting with one of these would run as a gateway command;
#: the guard in front keeps it an answer. Zero-width, so the agent reads the label.
COMMAND_PREFIXES = ("/", "!")
COMMAND_GUARD = "\u200b"

#: Added to the turn when the clicked message is a card's question.
CARD_NOTE = "(Clicked on the question from card {card}.)"

#: A DM channel id's first letter, as the adapter's message handler reads it.
DM_CHANNEL_PREFIX = "D"

#: A synthetic message's ts when the payload carries no ``action_ts``.
FALLBACK_TS = "kage-click-{ts}-{action}-{user}"

#: Bound on the answered-message map, oldest evicted first.
ANSWERED_MAX = 512

#: ``(channel, ts, kind)`` a click answered.
_answered: OrderedDict[tuple, None] = OrderedDict()
#: The keys of ``_answered`` whose rewrite landed.
_rewritten: OrderedDict[tuple, None] = OrderedDict()
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
    """Add the choice and link listeners to ``adapter._app``.

    A choice click's turn is the label Slack showed, or the value when it is
    that label naming a line the message shows; either way, a click on a
    card's question then names the card.
    """

    async def on_choice(ack, body, action):
        await answer(adapter, ack, body, action, CHOICE_KIND)

    adapter._app.action(_presenter.CHOICE_ACTION_ID_PATTERN)(on_choice)
    adapter._app.action(_presenter.LINK_ACTION_ID_PATTERN)(_presenter.ack_link_click)
    logger.info("slack_ux_clicks: choice and link button handlers registered")


def _escape(text: str) -> str:
    for raw, escaped in MRKDWN_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def answered(channel_id: str, msg_ts: str) -> bool:
    """Whether a choice click in this process answered the message and rewrote it."""
    return (str(channel_id), str(msg_ts), CHOICE_KIND) in _rewritten


def _answered_by(other: str) -> bool:
    """Whether ``other`` is one of the buttons a choice click answers: every choice."""
    return bool(_presenter.CHOICE_ACTION_ID_PATTERN.search(other))


def answered_blocks(blocks: Any, answered: Any, note: str) -> list[dict]:
    """``blocks`` with the answered buttons dropped, and ``note`` as a context line after them.

    An actions block left with no buttons is dropped; one that still holds a
    link keeps it.
    So is the "waiting on you" line, now that it is answered.
    """
    out: list[dict] = []
    for block in blocks or ():
        if isinstance(block, dict) and block.get("block_id") == _presenter.WAITING_BLOCK_ID:
            continue
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


def _shown_text(action: dict) -> str:
    """The clicked button's text as Slack displayed it."""
    text = action.get("text") or {}
    return str(text.get("text") or "").strip() if isinstance(text, dict) else ""


def _shown_lines(blocks: Any) -> list[str]:
    """Each rich_text line ``blocks`` show, its elements' text joined."""
    lines: list[str] = []
    for block in blocks or ():
        if not (isinstance(block, dict) and block.get("type") == "rich_text"):
            continue
        for section in block.get("elements") or ():
            elements = (section or {}).get("elements") or () if isinstance(section, dict) else ()
            lines.append("".join(str((e or {}).get("text") or "") for e in elements if isinstance(e, dict)))
    return lines


def _turn(label: str, value: Any, message: dict) -> str:
    """The clicker's turn: ``value`` when it is ``label`` naming a line the message shows, else ``label``."""
    prefix = label + TURN_JOIN
    if not (isinstance(value, str) and value.startswith(prefix)):
        return label
    named = value[len(prefix):].strip()
    if named and "\n" not in named and any(named in line for line in _shown_lines(message.get("blocks"))):
        return label + TURN_JOIN + named
    return label


def _gated_out(adapter: Any, channel_id: str) -> bool:
    """Whether the adapter would ignore a typed message in ``channel_id``: outside
    ``allowed_channels``, or a DM with DMs disabled. Checked before anything is shown."""
    allowed = adapter._slack_allowed_channels()
    if allowed and channel_id not in allowed:
        return True
    return channel_id.startswith(DM_CHANNEL_PREFIX) and bool(adapter._slack_disable_dms())


def _as_answer(label: str) -> str:
    """``label`` as message text that cannot be parsed as a gateway command."""
    return COMMAND_GUARD + label if label.startswith(COMMAND_PREFIXES) else label


def _question_card(channel_id: str, msg_ts: str) -> str:
    """The card whose question this process posted as the message, else ""."""
    try:
        from gateway import slack_ux_moments
    except ImportError:
        return ""
    try:
        return str(slack_ux_moments.question_card(channel_id, msg_ts) or "")
    except Exception:  # noqa: BLE001 — the label alone still answers
        return ""


def _turn_text(label: str, card: str) -> str:
    text = _as_answer(label)
    return f"{text}\n\n{CARD_NOTE.format(card=card)}" if card else text


def _thread_ts(body: dict, message: dict, msg_ts: str) -> str:
    container = body.get("container") or {}
    return str(message.get("thread_ts") or container.get("thread_ts") or msg_ts)


async def answer(adapter: Any, ack: Any, body: dict, action: dict, kind: str) -> None:
    """Authorize a choice click, mark it answered, echo it, and run it as the clicker's turn."""
    started = await adapter._begin_interaction(ack, body, action, kind)
    if started is None:
        return
    team_id, action_id, value, message, msg_ts, channel_id, _user_name, user_id = started
    label = _shown_text(action)
    if not (label and msg_ts and channel_id and user_id):
        return
    if _gated_out(adapter, channel_id):
        logger.info("slack_ux_clicks: ignoring a %s click in %s, which the adapter ignores", kind, channel_id)
        return
    key = (channel_id, msg_ts, kind)
    if key in _answered:
        logger.info("slack_ux_clicks: dropping a second %s click on %s, already answered", kind, msg_ts)
        return
    _answered[key] = None
    while len(_answered) > ANSWERED_MAX:
        _answered.popitem(last=False)

    thread_ts = _thread_ts(body, message, msg_ts)
    shown = _escape(label)
    client = adapter._get_client(channel_id, team_id=team_id)
    note = ANSWERED.format(user=user_id, label=shown)
    # Before the awaits: a card that moves on in between settles its question and forgets it.
    card = _question_card(channel_id, msg_ts)
    try:
        await client.chat_update(
            channel=channel_id, ts=msg_ts, text=note,
            blocks=answered_blocks(message.get("blocks"), _answered_by, note),
        )
        if key in _answered:
            _rewritten[key] = None
            while len(_rewritten) > ANSWERED_MAX:
                _rewritten.popitem(last=False)
    except Exception as exc:  # noqa: BLE001 — the click still answers
        logger.warning(
            "slack_ux_clicks: could not mark %s answered; its buttons stay but further clicks are dropped: %s",
            msg_ts, exc,
        )
    try:
        await client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text=ECHO.format(user=user_id, label=shown),
        )
    except Exception as exc:  # noqa: BLE001 — the click still answers
        logger.warning("slack_ux_clicks: could not echo the click on %s: %s", msg_ts, exc)

    synthetic = {
        "type": "message",
        "user": user_id,
        "text": _turn_text(_turn(label, value, message), card),
        "channel": channel_id,
        # The click's own ts keeps the deduplicator from conflating this turn
        # with the echo or the clicked message, as a reaction trigger's does.
        "ts": str(action.get("action_ts") or FALLBACK_TS.format(ts=msg_ts, action=action_id, user=user_id)),
        "thread_ts": thread_ts,
        # Skips the mention requirement only; channel and user checks still apply.
        "_hermes_force_process": True,
    }
    if team_id:
        synthetic["team"] = team_id
    await adapter._handle_slack_message(synthetic)
