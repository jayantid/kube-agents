"""The card-wake replays: their prompts, their in-pod scripts, and how the turns become one result.

The in-pod scripts run here under the test interpreter, the way
``test_board.py`` runs its sibling, against a stand-in hermes tree: a
``kanban_db`` that keeps its board in a JSON file, a dispatcher that records
a card giving up, and a notifier whose wake passes through
``slack_ux_moments.wake_text`` when the module is there, as the patched one
does. The module itself is the real
``deploy/docker/patches/slack_ux_moments.py``, with the real
``agents/platform/scripts`` beside it, so the note under test is the one the
image ships. The harness-side parsers are tested with canned replies.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from devops_bench.agents import AgentResult
from kube_agents_bench import card_wake

REPO = Path(__file__).resolve().parents[2]
MOMENTS = REPO / "deploy" / "docker" / "patches" / "slack_ux_moments.py"
SCRIPTS = REPO / "agents" / "platform" / "scripts"
FAILURE_CASE = REPO / "bench" / "tasks" / "chat-voice-failure-leads-with-fact" / "task.yaml"

PROMPT = """[bench:slack-question-wake]
title: Check checkout-gateway's restarts
question: Two clusters run checkout-gateway. Which should I look at?
options: seeded-a | seeded-b
answer: seeded-b
"""
REASON = "Two clusters run checkout-gateway. Which should I look at?\n- seeded-a\n- seeded-b"
FAILURE_PROMPT = """[bench:card-failure-wake]
title: Restart checkout-gateway on seeded-a
body: Roll the checkout-gateway Deployment on seeded-a.
outcome: blocked
reason: Permission denied: container.deployments.update on seeded-a.
"""
FAILURE_REASON = "Permission denied: container.deployments.update on seeded-a."
needs_moments = pytest.mark.skipif(
    not MOMENTS.exists(), reason="main has no deploy/docker/patches/slack_ux_moments.py"
)

_FAKE_KANBAN_DB = '''
import json, os
from dataclasses import dataclass
from types import SimpleNamespace

DEFAULT_BOARD = "default"
_PATH = os.environ["FAKE_BOARD"]


def _load():
    try:
        with open(_PATH) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"tasks": {}, "events": []}


def _save(state):
    with open(_PATH, "w") as fh:
        json.dump(state, fh)


@dataclass
class Task:
    id: str
    title: str
    assignee: object


def create_task(conn, *, title, body=None, created_by=None, max_retries=None):
    state = _load()
    task_id = "t_%08d" % (len(state["tasks"]) + 1)
    state["tasks"][task_id] = {"title": title, "body": body, "created_by": created_by,
                               "status": "ready", "assignee": None, "max_retries": max_retries}
    _save(state)
    return task_id


def assign_task(conn, task_id, profile):
    state = _load()
    state["tasks"][task_id]["assignee"] = profile
    state["events"].append({"id": len(state["events"]) + 1, "task_id": task_id, "kind": "assigned",
                            "payload": {"assignee": profile}})
    _save(state)
    return True


def block_task(conn, task_id, *, reason=None, kind=None):
    state = _load()
    state["tasks"][task_id]["status"] = "blocked"
    state["events"].append({"id": len(state["events"]) + 1, "task_id": task_id, "kind": "blocked",
                            "payload": {"reason": reason, "kind": kind}})
    _save(state)
    return True


def list_events(conn, task_id):
    return [SimpleNamespace(**e) for e in _load()["events"] if e["task_id"] == task_id]


def get_task(conn, task_id):
    row = _load()["tasks"][task_id]
    return Task(id=task_id, title=row["title"], assignee=row["assignee"])


def archive_task(conn, task_id):
    state = _load()
    state["tasks"][task_id]["status"] = "archived"
    _save(state)
    return True
'''

_FAKE_DISPATCH = '''
from hermes_cli.kanban_db import _load, _save


def _record_task_failure(conn, task_id, error, *, outcome, force_trip=False):
    state = _load()
    state["tasks"][task_id]["status"] = "blocked"
    state["events"].append({"id": len(state["events"]) + 1, "task_id": task_id, "kind": "gave_up",
                            "payload": {"error": error, "trigger_outcome": outcome,
                                        "force_trip": force_trip}})
    _save(state)
    return True
'''

_FAKE_NOTIFIER = '''
import os

try:
    from gateway.slack_ux_moments import wake_text as _kage_moments_wake_text
except ImportError:
    _kage_moments_wake_text = None


_STATUS = {"blocked": "blocked; needs attention", "gave_up": "gave up (retries exhausted)"}


class _KanbanNotification:
    def __init__(self, runner, d, *, platform_cls, sub_fail_counts):
        self.d, self.sub = d, d["sub"]
        self.adapter, self.synth, self.wake_kinds = None, "", set()
        self.is_push_adapter = True

    def format_event(self, ev):
        return None

    def build_wake_text(self):
        if os.environ.get("FAKE_WAKE_FAILS"):
            raise RuntimeError("notifier exploded")
        self.wake_kinds = {ev.kind for ev in self.d["events"]}
        self.synth = "[kanban] Task %s %s." % (self.sub["task_id"], _STATUS[self.d["events"][-1].kind])
        if self.d["task"].assignee:
            self.synth += "\\nAssignee: @%s" % self.d["task"].assignee
        if not self.is_push_adapter:
            self.synth += "\\nVia: %s" % self.sub["platform"]
        if _kage_moments_wake_text is not None:
            self.synth = _kage_moments_wake_text(self.sub, self.d["events"], self.wake_kinds, self.synth)
'''


@pytest.fixture
def hermes_root(tmp_path: Path) -> Path:
    """A stand-in for ``/opt/hermes`` without ``gateway/slack_ux_moments.py``, as on main."""
    root = tmp_path / "hermes"
    (root / "hermes_cli").mkdir(parents=True)
    (root / "gateway").mkdir()
    (root / "hermes_cli" / "__init__.py").write_text("")
    (root / "hermes_cli" / "kanban_db.py").write_text(_FAKE_KANBAN_DB)
    (root / "hermes_cli" / "kanban_db_dispatch.py").write_text(_FAKE_DISPATCH)
    # Only the split module opens the board: a script that reaches for
    # kanban_db.connect, the compat shim, fails here.
    (root / "hermes_cli" / "kanban_db_connect.py").write_text("def connect():\n    return object()\n")
    (root / "gateway" / "__init__.py").write_text("")
    (root / "gateway" / "kanban_watchers_notifier.py").write_text(_FAKE_NOTIFIER)
    return root


def _with_moments(root: Path) -> Path:
    shutil.copy(MOMENTS, root / "gateway" / "slack_ux_moments.py")
    return root


def _board(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "board.json").read_text())


def _run(script: str, args: list[str], tmp_path: Path, **env: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        check=False,
        env={"FAKE_BOARD": str(tmp_path / "board.json"), "PATH": "/usr/bin:/bin", **env},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _plant_args(root: Path, replay: card_wake.Replay | card_wake.Failure) -> list[str]:
    if isinstance(replay, card_wake.Failure):
        tail = [replay.body, replay.reason, replay.outcome, card_wake.FAILURE_ASSIGNEE]
    else:
        tail = [replay.reason, replay.reason, card_wake.OUTCOME_QUESTION, ""]
    return [
        card_wake.REPLAY_PRESENT,
        str(root),
        str(SCRIPTS),
        card_wake.SLACK_UX_FLAG,
        card_wake.FLAG_ON,
        card_wake.CARD_CREATOR,
        card_wake.STUB_CHANNEL,
        card_wake.STUB_THREAD,
        replay.title,
        *tail,
    ]


def _shell_for(root: Path, tmp_path: Path, prompt: str = PROMPT, **env: str):
    """A stand-in for ``harness._agent_shell`` that runs the scripts locally."""

    def shell(command: str, timeout: float) -> str:
        replay = card_wake.parse(prompt)
        assert replay is not None
        if command == card_wake.plant_command(replay):
            return _run(card_wake._PLANT_SCRIPT, _plant_args(root, replay), tmp_path, **env)
        card = command.rsplit(" ", 1)[1]
        assert command == card_wake.archive_command(card)
        return _run(
            card_wake._ARCHIVE_SCRIPT,
            [card_wake.REPLAY_PRESENT, str(root), card],
            tmp_path,
        )

    return shell


def _moments_note() -> str:
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("slack_ux_moments_note", MOMENTS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module.WAKE_NOTE


def test_parse_reads_the_replay_fields() -> None:
    replay = card_wake.parse(PROMPT)

    assert replay == card_wake.Replay(
        title="Check checkout-gateway's restarts",
        question="Two clusters run checkout-gateway. Which should I look at?",
        options=("seeded-a", "seeded-b"),
        answer="seeded-b",
    )
    # needs_you turns each trailing "- " line into a choice button.
    assert replay.reason == REASON


def test_an_ordinary_prompt_is_not_a_replay() -> None:
    assert card_wake.parse("is checkout-gateway restarting?") is None
    assert card_wake.parse(f"please run this:\n{PROMPT}") is None


@pytest.mark.parametrize("field", ["title", "question", "options", "answer"])
def test_a_replay_missing_a_field_is_an_authoring_error(field: str) -> None:
    prompt = "\n".join(line for line in PROMPT.splitlines() if not line.startswith(f"{field}:"))
    with pytest.raises(ValueError, match=field):
        card_wake.parse(prompt)


def test_a_replay_whose_options_are_blank_is_an_authoring_error() -> None:
    with pytest.raises(ValueError, match="options"):
        card_wake.parse(PROMPT.replace("seeded-a | seeded-b", " | "))


@needs_moments
def test_with_the_module_the_question_posts_once_and_the_wake_carries_the_note(
    hermes_root: Path, tmp_path: Path
) -> None:
    shell = _shell_for(_with_moments(hermes_root), tmp_path)

    planted = card_wake.plant(shell, card_wake.parse(PROMPT), timeout=30)

    assert planted.posted == 1
    assert planted.wake == (
        f"[kanban] Task {planted.card} blocked; needs attention.\n\n{_moments_note()}"
    )
    card = _board(tmp_path)["tasks"][planted.card]
    assert card["status"] == "blocked"
    assert card["created_by"] == card_wake.CARD_CREATOR
    assert _board(tmp_path)["events"][0]["payload"] == {"reason": REASON, "kind": "needs_input"}


def test_without_the_module_nothing_posts_and_the_wake_is_plain(
    hermes_root: Path, tmp_path: Path
) -> None:
    """The image on main: the case's red comes from this wake."""
    planted = card_wake.plant(
        _shell_for(hermes_root, tmp_path), card_wake.parse(PROMPT), timeout=30
    )

    assert planted.posted == 0
    assert planted.wake == f"[kanban] Task {planted.card} blocked; needs attention."


def test_a_failed_wake_archives_the_card_it_filed(hermes_root: Path, tmp_path: Path) -> None:
    shell = _shell_for(hermes_root, tmp_path, FAKE_WAKE_FAILS="1")

    with pytest.raises(card_wake.ReplayUnavailable, match="notifier exploded"):
        card_wake.plant(shell, card_wake.parse(PROMPT), timeout=30)

    [card] = _board(tmp_path)["tasks"].values()
    assert card["status"] == "archived"


def test_archive_archives_the_card(hermes_root: Path, tmp_path: Path) -> None:
    shell = _shell_for(hermes_root, tmp_path)
    planted = card_wake.plant(shell, card_wake.parse(PROMPT), timeout=30)

    assert card_wake.archive(shell, planted.card, timeout=30) is True
    assert _board(tmp_path)["tasks"][planted.card]["status"] == "archived"


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("", "did not run"),
        (f"{card_wake.REPLAY_PRESENT}\nnot json", "not JSON"),
        (f"{card_wake.REPLAY_PRESENT}\n[]", "not an object"),
        (f'{card_wake.REPLAY_PRESENT}\n{{"error": "ImportError: no hermes"}}', "no hermes"),
        (f'{card_wake.REPLAY_PRESENT}\n{{"card": "t_1", "wake": " "}}', "no wake"),
    ],
)
def test_plant_refuses_a_reply_it_cannot_trust(reply: str, reason: str) -> None:
    with pytest.raises(card_wake.ReplayUnavailable, match=reason):
        card_wake.plant(lambda command, timeout: reply, card_wake.parse(PROMPT), 30)


def test_archive_is_best_effort() -> None:
    assert card_wake.archive(lambda command, timeout: "", "t_1", 30) is False


def test_parse_reads_a_failure_prompt() -> None:
    assert card_wake.parse(FAILURE_PROMPT) == card_wake.Failure(
        title="Restart checkout-gateway on seeded-a",
        body="Roll the checkout-gateway Deployment on seeded-a.",
        outcome="blocked",
        reason=FAILURE_REASON,
    )


@pytest.mark.parametrize("field", ["title", "body", "outcome", "reason"])
def test_a_failure_prompt_missing_a_field_is_an_authoring_error(field: str) -> None:
    prompt = "\n".join(
        line for line in FAILURE_PROMPT.splitlines() if not line.startswith(f"{field}:")
    )
    with pytest.raises(ValueError, match=field):
        card_wake.parse(prompt)


def test_the_failure_case_prompt_is_a_blocked_replay() -> None:
    replay = card_wake.parse(yaml.safe_load(FAILURE_CASE.read_text())["prompt"])

    assert isinstance(replay, card_wake.Failure)
    assert replay.outcome == card_wake.OUTCOME_BLOCKED
    assert "invoice-renderer" in replay.title
    assert replay.reason.startswith("Permission denied.")


def test_the_failure_case_says_why_only_from_the_reason() -> None:
    case = yaml.safe_load(FAILURE_CASE.read_text())
    replay = card_wake.parse(case["prompt"])
    (says_why,) = [e for e in case["verification_spec"] if e["name"] == "the-reply-says-why"]
    phrases = says_why["check"]["any_of_phrases"]

    # The wake carries the title, so a phrase it also holds is answerable unread.
    assert all(p in replay.reason for p in phrases)
    assert not any(p in f"{replay.title} {replay.body}" for p in phrases)


def test_a_failure_prompt_with_an_unknown_outcome_is_an_authoring_error() -> None:
    with pytest.raises(ValueError, match="'crashed' is not one of blocked, gave_up"):
        card_wake.parse(FAILURE_PROMPT.replace("outcome: blocked", "outcome: crashed"))


def test_a_blocked_failure_wakes_through_the_api_server_with_the_card_assigned(
    hermes_root: Path, tmp_path: Path
) -> None:
    shell = _shell_for(hermes_root, tmp_path, FAILURE_PROMPT)

    planted = card_wake.plant(shell, card_wake.parse(FAILURE_PROMPT), timeout=30)

    assert planted.posted == 0
    assert planted.wake == (
        f"[kanban] Task {planted.card} blocked; needs attention.\n"
        f"Assignee: @{card_wake.FAILURE_ASSIGNEE}\nVia: api_server"
    )
    board = _board(tmp_path)
    card = board["tasks"][planted.card]
    assert card["status"] == "blocked"
    assert card["body"] == "Roll the checkout-gateway Deployment on seeded-a."
    assert card["assignee"] == card_wake.FAILURE_ASSIGNEE
    assert card["max_retries"] is None
    # Blocked first, assigned after, so the dispatcher never sees it ready.
    assert [e["kind"] for e in board["events"]] == ["blocked", "assigned"]
    assert board["events"][0]["payload"] == {"reason": FAILURE_REASON, "kind": None}


def test_a_gave_up_failure_trips_the_breaker_with_the_reason_as_its_error(
    hermes_root: Path, tmp_path: Path
) -> None:
    prompt = FAILURE_PROMPT.replace("outcome: blocked", "outcome: gave_up")
    shell = _shell_for(hermes_root, tmp_path, prompt)

    planted = card_wake.plant(shell, card_wake.parse(prompt), timeout=30)

    assert planted.wake == (
        f"[kanban] Task {planted.card} gave up (retries exhausted).\n"
        f"Assignee: @{card_wake.FAILURE_ASSIGNEE}\nVia: api_server"
    )
    board = _board(tmp_path)
    # Never assigned, and one failure is its limit: assigning would reset the
    # failure count and let recompute_ready hand it to a real worker.
    card = board["tasks"][planted.card]
    assert card["assignee"] is None
    assert card["max_retries"] == 1
    assert [e["kind"] for e in board["events"]] == ["gave_up"]
    assert board["events"][0]["payload"] == {
        "error": FAILURE_REASON,
        "trigger_outcome": "crashed",
        "force_trip": True,
    }


def test_a_failed_failure_wake_archives_the_card_it_filed(
    hermes_root: Path, tmp_path: Path
) -> None:
    shell = _shell_for(hermes_root, tmp_path, FAILURE_PROMPT, FAKE_WAKE_FAILS="1")

    with pytest.raises(card_wake.ReplayUnavailable, match="failure wake: .*notifier exploded"):
        card_wake.plant(shell, card_wake.parse(FAILURE_PROMPT), timeout=30)

    [card] = _board(tmp_path)["tasks"].values()
    assert card["status"] == "archived"


_PLANTED = card_wake.Planted(card="t_1", wake="[kanban] Task t_1 blocked.", posted=1)


def _result(output: str, names: list[str], tokens: dict, **metadata: object) -> AgentResult:
    return AgentResult(
        output=output,
        trajectory=[{"name": n, "args": {}, "result": "{}", "status": "completed"} for n in names],
        tokens=tokens,
        metadata=dict(metadata),
    )


def test_merge_grades_the_wake_reply_and_keeps_both_trajectories() -> None:
    wake = _result("[SILENT]", [], {"input": 10, "output": 1, "total": 11}, session_id="s")
    answer = _result(
        "Passed seeded-b to the card.",
        ["kanban_comment", "kanban_unblock"],
        {"input": 30, "output": 5, "total": 35},
        session_id="s",
    )

    merged = card_wake.merge(_PLANTED, wake, answer)

    assert merged.output == "[SILENT]"
    assert merged.metadata["final_message"] == "[SILENT]"
    assert [step["name"] for step in merged.trajectory] == ["kanban_comment", "kanban_unblock"]
    # One session: the answer turn's row already counts the wake turn.
    assert merged.tokens == {"input": 30, "output": 5, "total": 35}
    assert merged.metadata["question_wake"]["answer_output"] == "Passed seeded-b to the card."
    assert merged.metadata["question_wake"]["posted"] == 1


def test_merge_sums_tokens_across_sessions_and_keeps_an_empty_reply_empty() -> None:
    wake = _result("", ["kanban_show"], {"input": 10, "output": None, "total": 10})
    answer = _result("done", [], {"input": 30, "output": 5, "total": 35}, session_id="s2")

    merged = card_wake.merge(_PLANTED, wake, answer)

    assert merged.tokens == {"input": 40, "output": 5, "total": 45}
    assert merged.output == ""
    assert merged.metadata["final_message"] == ""


def test_merge_keeps_both_turns_errors() -> None:
    wake = AgentResult(output="a", trajectory=[], errors=["first"])
    answer = AgentResult(output="b", trajectory=[], errors=["second"])

    assert card_wake.merge(_PLANTED, wake, answer).errors == ["first", "second"]


def test_tag_keeps_the_wake_reply_and_records_the_card() -> None:
    wake = _result(
        "I couldn't restart checkout-gateway on seeded-a.",
        ["kanban_show"],
        {"input": 10, "output": 4, "total": 14},
        session_id="s",
        final_message="I couldn't restart checkout-gateway on seeded-a.",
    )

    tagged = card_wake.tag(_PLANTED, wake)

    assert tagged.output == wake.output
    assert tagged.metadata["final_message"] == "I couldn't restart checkout-gateway on seeded-a."
    assert tagged.trajectory == wake.trajectory
    assert tagged.tokens == wake.tokens
    assert tagged.metadata["failure_wake"] == {
        "card": "t_1",
        "wake": "[kanban] Task t_1 blocked.",
        "posted": 1,
    }
