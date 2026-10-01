"""Replay a specialist's Slack question and the wake it sends the front door.

With ``KAGE_SLACK_UX`` on, a card that blocks on ``needs_input`` in a Slack
thread posts the specialist's question there itself
(``gateway/slack_ux_moments.py``, ``needs_you``), and the wake the notifier
then gives the conversation that filed the card says so. The front door is
expected to stay out of the way: reply ``[SILENT]`` to the wake, then carry the
user's answer to the card (``agents/chat/SOUL.md`` §1.5 and §5). None of that
is reachable from the harness. The API server never subscribes a card the way
a Slack thread does, and on an install with no Slack adapter the notifier skips
a Slack subscription before it builds any text.

A prompt whose first line is :data:`DIRECTIVE` is a replay instead of an ask.
The in-pod script files a card on the agent's board and blocks it on
``needs_input`` with the case's question, posts the question through the
image's ``needs_you`` with a stub Slack client where the image has that
module, and builds the wake with the image's own notifier. The harness then
sends that wake as the first turn and the case's typed answer as the second,
on one conversation (:meth:`KubeAgentsHarness._execute_question_wake`). An
image without the module builds the wake it always did, which is what makes
the case red there.

What this does not reproduce: the flag is set in the script's process
whatever the install's setting, the turns arrive on the run's own
``/v1/responses`` conversation rather than the Slack thread's session, and no
message reaches Slack. The text under test is the notifier's, built by the
image's own code.

Unlike :mod:`kube_agents_bench.board`, a failed plant is not best effort: a
run that never saw the wake grades nothing, so :func:`plant` raises and the
harness records the run as infrastructure.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from devops_bench.agents import AgentResult

from kube_agents_bench.worker_trajectory import FALLBACK_PYTHON, HERMES_PYTHON

__all__ = [
    "DIRECTIVE",
    "Planted",
    "Replay",
    "ReplayUnavailable",
    "archive",
    "merge",
    "parse",
    "plant",
]

# First line of a replay prompt. The ``key: value`` lines under it are
# :data:`_FIELDS`; anything else is ignored.
DIRECTIVE = "[bench:slack-question-wake]"
_FIELDS = ("title", "question", "options", "answer")
OPTION_SEPARATOR = "|"

# Line the in-pod scripts print before their JSON. A reply without it means
# the script never ran to completion.
REPLAY_PRESENT = "__SLACK_QUESTION_WAKE__"

# Where the image installs hermes, and the scripts directory holding
# slack_presenter.py and slack_moments.py, which slack_ux_moments imports.
HERMES_ROOT = "/opt/hermes"
SCRIPTS_DIR = "/opt/defaults/scripts"

# slack_presenter.FLAG_ENV and a value it reads as on.
SLACK_UX_FLAG = "KAGE_SLACK_UX"
FLAG_ON = "1"

# Who the card says filed it, and the stub thread its question is posted in.
# Slack never sees either.
CARD_CREATOR = "devops-bench"
STUB_CHANNEL = "C0BENCHWAKE"
STUB_THREAD = "1700000000.000100"

# Runs inside the agent container. Positional arguments: the sentinel, the
# hermes root, the scripts directory, the flag, its on value, the creator,
# the stub channel, the stub thread, the card title and the block reason.
# The card is archived again if anything after filing it fails.
_PLANT_SCRIPT = r"""
import asyncio, json, os, sys

(SENTINEL, HERMES_ROOT, SCRIPTS, FLAG, ON, CREATOR,
 CHANNEL, THREAD, TITLE, REASON) = sys.argv[1:11]
STUB_TS = "1700000000.000200"
out = {"card": None, "wake": None, "posted": 0, "error": None}


class _Client:
    def __init__(self, posts):
        self.posts = posts

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": STUB_TS}

    async def chat_update(self, **kwargs):
        return {"ok": True}


class _Adapter:
    def __init__(self):
        self.posts = []

    def _get_client(self, chat_id, team_id=None):
        return _Client(self.posts)


conn = None
try:
    sys.path[:0] = [HERMES_ROOT, SCRIPTS]
    os.environ[FLAG] = ON
    from hermes_cli import kanban_db as kb
    from gateway import kanban_watchers_notifier as notifier
    try:
        from gateway import slack_ux_moments as moments
    except ImportError:
        moments = None
    conn = kb.connect()
    card = kb.create_task(conn, title=TITLE, body=REASON, created_by=CREATOR)
    out["card"] = card
    if not kb.block_task(conn, card, reason=REASON, kind="needs_input"):
        raise RuntimeError("card %s would not block" % card)
    events = [e for e in kb.list_events(conn, card) if e.kind == "blocked"][-1:]
    if not events:
        raise RuntimeError("card %s has no blocked event" % card)
    sub = {"task_id": card, "platform": "slack", "chat_id": CHANNEL, "thread_id": THREAD,
           "delivery_mode": "notify+wake"}
    adapter = _Adapter()
    if moments is not None:
        asyncio.run(moments.needs_you(adapter, sub, events[0].payload or {}, events[0].id))
    wake = notifier._KanbanNotification(
        None, {"sub": sub, "task": kb.get_task(conn, card), "board": kb.DEFAULT_BOARD, "events": events},
        platform_cls=None, sub_fail_counts={})
    wake.adapter = adapter
    for ev in events:
        wake.format_event(ev)
    wake.build_wake_text()
    if not wake.synth:
        raise RuntimeError("the notifier built no wake for card %s" % card)
    out.update(wake=wake.synth, posted=len(adapter.posts))
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
    if conn is not None and out["card"]:
        try:
            kb.archive_task(conn, out["card"])
        except Exception:
            pass
print(SENTINEL)
print(json.dumps(out))
"""

# Archives the replay's card once the run is over. Positional arguments: the
# sentinel, the hermes root and the card id.
_ARCHIVE_SCRIPT = r"""
import json, sys

SENTINEL, HERMES_ROOT, CARD = sys.argv[1:4]
sys.path.insert(0, HERMES_ROOT)
out = {"archived": False, "error": None}
try:
    from hermes_cli import kanban_db as kb
    out["archived"] = bool(kb.archive_task(kb.connect(), CARD))
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(SENTINEL)
print(json.dumps(out))
"""


class ReplayUnavailable(RuntimeError):
    """The replay's card or wake could not be produced in the pod."""


@dataclass(frozen=True)
class Replay:
    """A replay prompt's fields: the card, its question and the user's answer."""

    title: str
    question: str
    options: tuple[str, ...]
    answer: str

    @property
    def reason(self) -> str:
        """The block reason as a specialist writes it: the question, then a ``- `` line per choice."""
        return "\n".join([self.question, *(f"- {option}" for option in self.options)])


@dataclass(frozen=True)
class Planted:
    """What the plant left on the board: the card, its wake, and how many posts the stub took."""

    card: str
    wake: str
    posted: int


def parse(prompt: str) -> Replay | None:
    """The replay ``prompt`` asks for, or ``None`` when it is an ordinary ask.

    Raises :class:`ValueError` for a replay prompt missing a field: that is a
    case authoring error, not a run.
    """
    lines = prompt.strip().splitlines()
    if not lines or lines[0].strip() != DIRECTIVE:
        return None
    fields: dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep and key.strip() in _FIELDS:
            fields[key.strip()] = value.strip()
    options = tuple(
        o.strip() for o in fields.get("options", "").split(OPTION_SEPARATOR) if o.strip()
    )
    missing = [f for f in _FIELDS if not fields.get(f)] + ([] if options else ["options"])
    if missing:
        raise ValueError(f"{DIRECTIVE} prompt is missing {', '.join(dict.fromkeys(missing))}")
    return Replay(fields["title"], fields["question"], options, fields["answer"])


def _command(script: str, args: list[str]) -> str:
    quoted = " ".join(shlex.quote(a) for a in args)
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(script)} {quoted}'
    )


def plant_command(replay: Replay) -> str:
    """The ``sh -c`` line that files, blocks and wakes the replay's card in the pod."""
    return _command(
        _PLANT_SCRIPT,
        [
            REPLAY_PRESENT,
            HERMES_ROOT,
            SCRIPTS_DIR,
            SLACK_UX_FLAG,
            FLAG_ON,
            CARD_CREATOR,
            STUB_CHANNEL,
            STUB_THREAD,
            replay.title,
            replay.reason,
        ],
    )


def archive_command(card: str) -> str:
    """The ``sh -c`` line that archives ``card`` in the pod."""
    return _command(_ARCHIVE_SCRIPT, [REPLAY_PRESENT, HERMES_ROOT, card])


def _reply(text: str, what: str) -> dict:
    marker = text.find(REPLAY_PRESENT)
    if marker < 0:
        raise ReplayUnavailable(f"{what}: the in-pod script did not run")
    try:
        payload = json.loads(text[marker + len(REPLAY_PRESENT) :].strip())
    except json.JSONDecodeError as exc:
        raise ReplayUnavailable(f"{what}: reply is not JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise ReplayUnavailable(f"{what}: reply is not an object")
    if payload.get("error"):
        raise ReplayUnavailable(f"{what}: {payload['error']}")
    return payload


def plant(shell: Callable[[str, float], str], replay: Replay, timeout: float) -> Planted:
    """File and block the replay's card, and return the wake the image builds for it.

    ``shell`` is :func:`harness._agent_shell`. Raises
    :class:`ReplayUnavailable` when the script did not run, its reply is not
    JSON, or it reported an error; the script archives a card it filed
    before failing.
    """
    payload = _reply(shell(plant_command(replay), timeout), "question wake")
    card, wake = payload.get("card"), payload.get("wake")
    if not isinstance(card, str) or not card or not isinstance(wake, str) or not wake.strip():
        raise ReplayUnavailable(f"question wake: no card or no wake in {payload!r}")
    posted = payload.get("posted")
    return Planted(card, wake, posted if isinstance(posted, int) else 0)


def archive(shell: Callable[[str, float], str], card: str, timeout: float) -> bool:
    """Archive the replay's card. Best effort: ``False`` when it could not be confirmed."""
    try:
        reply = _reply(shell(archive_command(card), timeout), f"archiving {card}")
    except ReplayUnavailable:
        return False
    return bool(reply.get("archived"))


def merge(planted: Planted, wake: AgentResult, answer: AgentResult) -> AgentResult:
    """One result for the two turns, graded on the wake turn's reply.

    ``output`` and ``final_message`` are the reply to the wake, the one turn
    the SOUL rule governs; the answer turn's text is kept in metadata. The
    trajectory and errors are both turns'. The answer turn's tokens supersede
    the wake turn's when both read the same session, whose row is cumulative
    over the conversation; otherwise they are summed.
    """
    session = wake.metadata.get("session_id")
    same_session = bool(session) and session == answer.metadata.get("session_id")
    tokens = dict(answer.tokens if same_session else wake.tokens)
    if not same_session:
        for bucket, value in answer.tokens.items():
            if value is not None:
                current = tokens.get(bucket)
                tokens[bucket] = value if current is None else current + value
    metadata = {**answer.metadata, **wake.metadata}
    metadata["final_message"] = str(wake.metadata.get("final_message") or wake.output)
    metadata["question_wake"] = {
        "card": planted.card,
        "wake": planted.wake,
        "posted": planted.posted,
        "answer_output": answer.output,
        "answer_final_message": str(answer.metadata.get("final_message") or answer.output),
    }
    return AgentResult(
        output=wake.output,
        trajectory=[*wake.trajectory, *answer.trajectory],
        tokens=tokens,
        errors=[*wake.errors, *answer.errors],
        metadata=metadata,
    )
