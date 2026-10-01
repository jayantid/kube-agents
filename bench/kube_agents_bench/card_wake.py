"""Replay the wake a blocked, failed or retried card sends the front door.

The front door's reply to a card's wake is a turn the harness's own asks never
reach. Two replays produce one. A prompt whose first line is
:data:`QUESTION_DIRECTIVE` or :data:`FAILURE_DIRECTIVE` is a replay instead of
an ask; :func:`parse` reads it.

**A specialist's Slack question.** With ``KAGE_SLACK_UX`` on, a card that blocks on ``needs_input`` in a Slack
thread posts the specialist's question there itself
(``gateway/slack_ux_moments.py``, ``needs_you``), and the wake the notifier
then gives the conversation that filed the card says so. The front door is
expected to stay out of the way: reply ``[SILENT]`` to the wake, then carry the
user's answer to the card (``agents/chat/SOUL.md`` §1.5 and §5). None of that
is reachable from the harness. The API server never subscribes a card the way
a Slack thread does, and on an install with no Slack adapter the notifier skips
a Slack subscription before it builds any text.

For :data:`QUESTION_DIRECTIVE`, the in-pod script files a card on the agent's board and blocks it on
``needs_input`` with the case's question, posts the question through the
image's ``needs_you`` with a stub Slack client where the image has that
module, and builds the wake with the image's own notifier. The harness then
sends that wake as the first turn and the case's typed answer as the second,
on one conversation (:meth:`KubeAgentsHarness._execute_card_wake`). An
image without the module builds the wake it always did, which is what makes
the case red there.

**A card that blocked or gave up.** On the API server a ``blocked`` or
``gave_up`` card wakes the conversation that filed it through the notifier's
self-post (``deploy/docker/patches/kanban_notifier.py``, ``wake_kinds_for``: a
non-push adapter always wakes for the failure kinds), and the reply to that
wake is the user's only announcement of the failure (``agents/chat/SOUL.md``
§2, step 5). The harness never reads it: its own poll turns ask for a status
recital, and no prompt makes a specialist fail every time. For
:data:`FAILURE_DIRECTIVE` the in-pod script files the card assigned to
:data:`FAILURE_ASSIGNEE`, blocks it with the case's reason (``outcome:
blocked``) or trips its failure breaker with it as the error (``outcome:
gave_up``, the event the dispatcher records when its retries run out), and
builds the wake with the image's notifier through a non-push stub adapter, as
the API server's is. The harness sends that wake as the run's only turn. The
wake names the card but not the reason, so the front door reads the card
(``kanban_show``) as it would in a real thread.

**A worker that crashed or timed out.** The dispatcher retries a ``crashed``
or ``timed_out`` card until its failure breaker trips
(``hermes_cli/kanban_db_dispatch.py``, ``_record_task_failure``:
``DEFAULT_FAILURE_LIMIT`` consecutive failures, or the card's own
``max_retries``). Below the limit the card goes back to ``ready`` and the
event wakes the front door alone (``outcome: crashed`` or ``timed_out``). On
the attempt that trips it, the dispatcher appends ``gave_up`` straight after
the ``crashed`` or ``timed_out`` event and parks the card in ``blocked``, so
one wake carries both (``outcome: crashed_final`` or ``timed_out_final``) and
its status names both: gave up, and that the dispatcher will retry. The plant
records each attempt as the dispatcher does, the event and then
``_record_task_failure``, which counts it and trips on its own: one attempt
for a retry, two for a final one. The card is assigned to
:data:`WORKER_ASSIGNEE` before it fails, a profile no install has, so a
retrying card left ``ready`` never starts a worker.

What neither replay reproduces: the turns arrive on the run's own
``/v1/responses`` conversation rather than the session that filed the card, so
the front door has not seen the ask that led to it; for a question, the flag is
set in the script's process whatever the install's setting and no message
reaches Slack. The wake under test is the notifier's, built by the image's own
code.

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
    "FAILURE_DIRECTIVE",
    "QUESTION_DIRECTIVE",
    "Failure",
    "Planted",
    "Replay",
    "ReplayUnavailable",
    "archive",
    "merge",
    "parse",
    "plant",
    "tag",
]

# First lines of the two replay prompts. The ``key: value`` lines under each
# are its fields (:data:`_QUESTION_FIELDS`, :data:`_FAILURE_FIELDS`); anything
# else is ignored.
QUESTION_DIRECTIVE = "[bench:slack-question-wake]"
FAILURE_DIRECTIVE = "[bench:card-failure-wake]"
_QUESTION_FIELDS = ("title", "question", "options", "answer")
_FAILURE_FIELDS = ("title", "body", "outcome", "reason")
OPTION_SEPARATOR = "|"

# How the plant script ends the card. ``question`` is a ``needs_input`` block;
# the failure outcomes are a failure prompt's ``outcome:`` values.
OUTCOME_QUESTION = "question"
OUTCOME_BLOCKED = "blocked"
OUTCOME_GAVE_UP = "gave_up"
# A worker's crash or timeout the dispatcher will retry, and the same on the
# attempt that trips its failure breaker.
OUTCOME_CRASHED = "crashed"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_CRASHED_FINAL = "crashed_final"
OUTCOME_TIMED_OUT_FINAL = "timed_out_final"
WORKER_OUTCOMES = (
    OUTCOME_CRASHED,
    OUTCOME_TIMED_OUT,
    OUTCOME_CRASHED_FINAL,
    OUTCOME_TIMED_OUT_FINAL,
)
FAILURE_OUTCOMES = (OUTCOME_BLOCKED, OUTCOME_GAVE_UP, *WORKER_OUTCOMES)

# Line the in-pod scripts print before their JSON. A reply without it means
# the script never ran to completion.
REPLAY_PRESENT = "__BENCH_CARD_WAKE__"

# Where the image installs hermes, and the scripts directory holding
# slack_presenter.py and slack_moments.py, which slack_ux_moments imports.
HERMES_ROOT = "/opt/hermes"
SCRIPTS_DIR = "/opt/defaults/scripts"

# slack_presenter.FLAG_ENV and a value it reads as on.
SLACK_UX_FLAG = "KAGE_SLACK_UX"
FLAG_ON = "1"

# Who the card says filed it, who a failed card says was working it, and the
# stub thread a question is posted in. Slack never sees the thread.
CARD_CREATOR = "devops-bench"
FAILURE_ASSIGNEE = "platform"
# Who a crashed or timed-out card says was working it: shaped like a
# scaffolded cluster agent's profile (cluster_agent_profile.py, profile_name)
# but never scaffolded, so the dispatcher never takes a card assigned to it.
WORKER_ASSIGNEE = "cluster-bench-project-bench-sandbox-us-central1"
STUB_CHANNEL = "C0BENCHWAKE"
STUB_THREAD = "1700000000.000100"

# Runs inside the agent container. Positional arguments: the sentinel, the
# hermes root, the scripts directory, the flag, its on value, the creator,
# the stub channel, the stub thread, the card title, its body, the block
# reason or failure error, the outcome and the assignee (empty for none).
# The card is archived again if anything after filing it fails. The worker
# pid, elapsed time and runtime limit in a crash or timeout's payload are
# made up; only the front door reads them.
_PLANT_SCRIPT = r"""
import asyncio, json, os, sys

(SENTINEL, HERMES_ROOT, SCRIPTS, FLAG, ON, CREATOR,
 CHANNEL, THREAD, TITLE, BODY, REASON, OUTCOME, ASSIGNEE) = sys.argv[1:14]
STUB_TS = "1700000000.000200"
STUB_PID = 4242
STUB_ELAPSED, STUB_LIMIT = 1830, 1800
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


class _ApiServerAdapter:
    # The API server's adapter has no push channel: its wake is a self-post.
    supports_async_delivery = False

    def __init__(self):
        self.posts = []


conn = None
try:
    sys.path[:0] = [HERMES_ROOT, SCRIPTS]
    os.environ[FLAG] = ON
    from hermes_cli import kanban_db as kb
    # kanban_db.connect is a compat shim since the board split; the split
    # module is the opener's home.
    from hermes_cli.kanban_db_connect import connect
    from gateway import kanban_watchers_notifier as notifier
    try:
        from gateway import slack_ux_moments as moments
    except ImportError:
        moments = None
    conn = connect()
    card = kb.create_task(conn, title=TITLE, body=BODY, created_by=CREATOR)
    out["card"] = card
    batch = 1
    if OUTCOME in ("crashed", "timed_out", "crashed_final", "timed_out_final"):
        from hermes_cli import kanban_db_dispatch as dispatch
        trigger, final = OUTCOME.replace("_final", ""), OUTCOME.endswith("_final")
        # Assigned before it fails, as a real card is: the profile does not
        # exist, so no dispatcher ever takes it.
        if not kb.assign_task(conn, card, ASSIGNEE):
            raise RuntimeError("card %s would not take assignee %s" % (card, ASSIGNEE))
        ASSIGNEE = ""
        # The payloads detect_crashed_workers and enforce_max_runtime write.
        if trigger == "crashed":
            payload = {"pid": STUB_PID, "claimer": None, "retry_status": "ready"}
            extra = {"pid": STUB_PID, "claimer": None}
        else:
            payload = {"pid": STUB_PID, "elapsed_seconds": STUB_ELAPSED, "limit_seconds": STUB_LIMIT,
                       "sigkill": False, "retry_status": "ready"}
            extra = {"pid": STUB_PID, "sigkill": False, "retry_status": "ready"}
        # Each attempt as the dispatcher records it: the event, then the
        # breaker's count, which appends gave_up when it trips. A final
        # attempt is the second.
        for _attempt in range(2 if final else 1):
            with kb.write_txn(conn):
                kb._append_event(conn, card, trigger, payload)
            tripped = dispatch._record_task_failure(conn, card, REASON, outcome=trigger,
                                                    event_payload_extra=extra)
        if tripped != final:
            raise RuntimeError("card %s %s its failure breaker"
                               % (card, "tripped" if tripped else "did not trip"))
        kind = "gave_up" if final else trigger
        # The notifier claims every event since its cursor, so a final
        # attempt's crash or timeout reaches the wake with the gave_up after it.
        batch = 2 if final else 1
    elif OUTCOME == "gave_up":
        from hermes_cli import kanban_db_dispatch as dispatch
        # force_trip records gave_up and parks the card on its first failure,
        # where the dispatcher would after exhausting its retries.
        if not dispatch._record_task_failure(conn, card, REASON, outcome="crashed", force_trip=True):
            raise RuntimeError("card %s would not give up" % card)
        kind = "gave_up"
    else:
        block_kind = "needs_input" if OUTCOME == "question" else None
        if not kb.block_task(conn, card, reason=REASON, kind=block_kind):
            raise RuntimeError("card %s would not block" % card)
        kind = "blocked"
    # Assigned only once it can no longer be dispatched.
    if ASSIGNEE and not kb.assign_task(conn, card, ASSIGNEE):
        raise RuntimeError("card %s would not take assignee %s" % (card, ASSIGNEE))
    events = [e for e in kb.list_events(conn, card) if e.kind != "assigned"][-batch:]
    if not events or events[-1].kind != kind:
        raise RuntimeError("card %s has no %s event" % (card, kind))
    if OUTCOME == "question":
        sub = {"task_id": card, "platform": "slack", "chat_id": CHANNEL, "thread_id": THREAD,
               "delivery_mode": "notify+wake"}
        adapter = _Adapter()
        if moments is not None:
            asyncio.run(moments.needs_you(adapter, sub, events[0].payload or {}, events[0].id))
    else:
        sub = {"task_id": card, "platform": "api_server", "chat_id": CHANNEL, "thread_id": "",
               "delivery_mode": "notify+wake"}
        adapter = _ApiServerAdapter()
    wake = notifier._KanbanNotification(
        None, {"sub": sub, "task": kb.get_task(conn, card), "board": kb.DEFAULT_BOARD, "events": events},
        platform_cls=None, sub_fail_counts={})
    wake.adapter = adapter
    wake.is_push_adapter = OUTCOME == "question"
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
    from hermes_cli.kanban_db_connect import connect
    out["archived"] = bool(kb.archive_task(connect(), CARD))
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
class Failure:
    """A failure prompt's fields: the card, its body, how it ended and why."""

    title: str
    body: str
    outcome: str
    reason: str


@dataclass(frozen=True)
class Planted:
    """What the plant left on the board: the card, its wake, and how many posts the stub took."""

    card: str
    wake: str
    posted: int


def _fields(lines: list[str], names: tuple[str, ...]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition(":")
        if sep and key.strip() in names:
            fields[key.strip()] = value.strip()
    return fields


def parse(prompt: str) -> Replay | Failure | None:
    """The replay ``prompt`` asks for, or ``None`` when it is an ordinary ask.

    Raises :class:`ValueError` for a replay prompt missing a field or naming
    an unknown outcome: that is a case authoring error, not a run.
    """
    lines = prompt.strip().splitlines()
    directive = lines[0].strip() if lines else ""
    if directive == FAILURE_DIRECTIVE:
        fields = _fields(lines[1:], _FAILURE_FIELDS)
        missing = [f for f in _FAILURE_FIELDS if not fields.get(f)]
        if missing:
            raise ValueError(f"{directive} prompt is missing {', '.join(missing)}")
        if fields["outcome"] not in FAILURE_OUTCOMES:
            raise ValueError(
                f"{directive} outcome {fields['outcome']!r} is not one of {', '.join(FAILURE_OUTCOMES)}"
            )
        return Failure(fields["title"], fields["body"], fields["outcome"], fields["reason"])
    if directive != QUESTION_DIRECTIVE:
        return None
    fields = _fields(lines[1:], _QUESTION_FIELDS)
    options = tuple(
        o.strip() for o in fields.get("options", "").split(OPTION_SEPARATOR) if o.strip()
    )
    missing = [f for f in _QUESTION_FIELDS if not fields.get(f)] + ([] if options else ["options"])
    if missing:
        raise ValueError(f"{directive} prompt is missing {', '.join(dict.fromkeys(missing))}")
    return Replay(fields["title"], fields["question"], options, fields["answer"])


def _command(script: str, args: list[str]) -> str:
    quoted = " ".join(shlex.quote(a) for a in args)
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(script)} {quoted}'
    )


def plant_command(replay: Replay | Failure) -> str:
    """The ``sh -c`` line that files, parks and wakes the replay's card in the pod."""
    if isinstance(replay, Failure):
        body, outcome = replay.body, replay.outcome
        assignee = WORKER_ASSIGNEE if outcome in WORKER_OUTCOMES else FAILURE_ASSIGNEE
    else:
        body, outcome, assignee = replay.reason, OUTCOME_QUESTION, ""
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
            body,
            replay.reason,
            outcome,
            assignee,
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


def plant(shell: Callable[[str, float], str], replay: Replay | Failure, timeout: float) -> Planted:
    """File the replay's card, park it, and return the wake the image builds for it.

    ``shell`` is :func:`harness._agent_shell`. Raises
    :class:`ReplayUnavailable` when the script did not run, its reply is not
    JSON, or it reported an error; the script archives a card it filed
    before failing.
    """
    what = "failure wake" if isinstance(replay, Failure) else "question wake"
    payload = _reply(shell(plant_command(replay), timeout), what)
    card, wake = payload.get("card"), payload.get("wake")
    if not isinstance(card, str) or not card or not isinstance(wake, str) or not wake.strip():
        raise ReplayUnavailable(f"{what}: no card or no wake in {payload!r}")
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


def tag(planted: Planted, wake: AgentResult) -> AgentResult:
    """The failure replay's one turn, with the card and its wake kept in metadata.

    The reply to the wake is already the run's ``final_message``; nothing
    else about the result changes.
    """
    metadata = {
        **wake.metadata,
        "failure_wake": {"card": planted.card, "wake": planted.wake, "posted": planted.posted},
    }
    return AgentResult(
        output=wake.output,
        trajectory=wake.trajectory,
        tokens=wake.tokens,
        errors=wake.errors,
        metadata=metadata,
    )
