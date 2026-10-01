"""The question-wake replay: its prompt, its in-pod scripts, and how two turns become one result.

The in-pod scripts run here under the test interpreter, the way
``test_board.py`` runs its sibling, against a stand-in hermes tree: a
``kanban_db`` that keeps its board in a JSON file and a notifier whose wake
passes through ``slack_ux_moments.wake_text`` when the module is there, as
the patched one does. The module itself is the real
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
from devops_bench.agents import AgentResult
from kube_agents_bench import question_wake

REPO = Path(__file__).resolve().parents[2]
MOMENTS = REPO / "deploy" / "docker" / "patches" / "slack_ux_moments.py"
SCRIPTS = REPO / "agents" / "platform" / "scripts"

PROMPT = """[bench:slack-question-wake]
title: Check checkout-gateway's restarts
question: Two clusters run checkout-gateway. Which should I look at?
options: seeded-a | seeded-b
answer: seeded-b
"""
REASON = "Two clusters run checkout-gateway. Which should I look at?\n- seeded-a\n- seeded-b"

_FAKE_KANBAN_DB = '''
import json, os
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


def connect():
    return object()


def create_task(conn, *, title, body=None, created_by=None):
    state = _load()
    task_id = "t_%08d" % (len(state["tasks"]) + 1)
    state["tasks"][task_id] = {"title": title, "body": body, "created_by": created_by,
                               "status": "ready", "assignee": None}
    _save(state)
    return task_id


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
    return SimpleNamespace(id=task_id, title=row["title"], assignee=row["assignee"])


def archive_task(conn, task_id):
    state = _load()
    state["tasks"][task_id]["status"] = "archived"
    _save(state)
    return True
'''

_FAKE_NOTIFIER = '''
import os

try:
    from gateway.slack_ux_moments import wake_text as _kage_moments_wake_text
except ImportError:
    _kage_moments_wake_text = None


class _KanbanNotification:
    def __init__(self, runner, d, *, platform_cls, sub_fail_counts):
        self.d, self.sub = d, d["sub"]
        self.adapter, self.synth, self.wake_kinds = None, "", set()

    def format_event(self, ev):
        return None

    def build_wake_text(self):
        if os.environ.get("FAKE_WAKE_FAILS"):
            raise RuntimeError("notifier exploded")
        self.wake_kinds = {ev.kind for ev in self.d["events"]}
        self.synth = "[kanban] Task %s blocked; needs attention." % self.sub["task_id"]
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


def _plant_args(root: Path, replay: question_wake.Replay) -> list[str]:
    return [
        question_wake.REPLAY_PRESENT,
        str(root),
        str(SCRIPTS),
        question_wake.SLACK_UX_FLAG,
        question_wake.FLAG_ON,
        question_wake.CARD_CREATOR,
        question_wake.STUB_CHANNEL,
        question_wake.STUB_THREAD,
        replay.title,
        replay.reason,
    ]


def _shell_for(root: Path, tmp_path: Path, **env: str):
    """A stand-in for ``harness._agent_shell`` that runs the scripts locally."""

    def shell(command: str, timeout: float) -> str:
        replay = question_wake.parse(PROMPT)
        assert replay is not None
        if command == question_wake.plant_command(replay):
            return _run(question_wake._PLANT_SCRIPT, _plant_args(root, replay), tmp_path, **env)
        card = command.rsplit(" ", 1)[1]
        assert command == question_wake.archive_command(card)
        return _run(
            question_wake._ARCHIVE_SCRIPT,
            [question_wake.REPLAY_PRESENT, str(root), card],
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
    replay = question_wake.parse(PROMPT)

    assert replay == question_wake.Replay(
        title="Check checkout-gateway's restarts",
        question="Two clusters run checkout-gateway. Which should I look at?",
        options=("seeded-a", "seeded-b"),
        answer="seeded-b",
    )
    # needs_you turns each trailing "- " line into a choice button.
    assert replay.reason == REASON


def test_an_ordinary_prompt_is_not_a_replay() -> None:
    assert question_wake.parse("is checkout-gateway restarting?") is None
    assert question_wake.parse(f"please run this:\n{PROMPT}") is None


@pytest.mark.parametrize("field", ["title", "question", "options", "answer"])
def test_a_replay_missing_a_field_is_an_authoring_error(field: str) -> None:
    prompt = "\n".join(line for line in PROMPT.splitlines() if not line.startswith(f"{field}:"))
    with pytest.raises(ValueError, match=field):
        question_wake.parse(prompt)


def test_a_replay_whose_options_are_blank_is_an_authoring_error() -> None:
    with pytest.raises(ValueError, match="options"):
        question_wake.parse(PROMPT.replace("seeded-a | seeded-b", " | "))


def test_with_the_module_the_question_posts_once_and_the_wake_carries_the_note(
    hermes_root: Path, tmp_path: Path
) -> None:
    shell = _shell_for(_with_moments(hermes_root), tmp_path)

    planted = question_wake.plant(shell, question_wake.parse(PROMPT), timeout=30)

    assert planted.posted == 1
    assert planted.wake == (
        f"[kanban] Task {planted.card} blocked; needs attention.\n\n{_moments_note()}"
    )
    card = _board(tmp_path)["tasks"][planted.card]
    assert card["status"] == "blocked"
    assert card["created_by"] == question_wake.CARD_CREATOR
    assert _board(tmp_path)["events"][0]["payload"] == {"reason": REASON, "kind": "needs_input"}


def test_without_the_module_nothing_posts_and_the_wake_is_plain(
    hermes_root: Path, tmp_path: Path
) -> None:
    """The image on main: the case's red comes from this wake."""
    planted = question_wake.plant(
        _shell_for(hermes_root, tmp_path), question_wake.parse(PROMPT), timeout=30
    )

    assert planted.posted == 0
    assert planted.wake == f"[kanban] Task {planted.card} blocked; needs attention."


def test_a_failed_wake_archives_the_card_it_filed(hermes_root: Path, tmp_path: Path) -> None:
    shell = _shell_for(hermes_root, tmp_path, FAKE_WAKE_FAILS="1")

    with pytest.raises(question_wake.ReplayUnavailable, match="notifier exploded"):
        question_wake.plant(shell, question_wake.parse(PROMPT), timeout=30)

    [card] = _board(tmp_path)["tasks"].values()
    assert card["status"] == "archived"


def test_archive_archives_the_card(hermes_root: Path, tmp_path: Path) -> None:
    shell = _shell_for(hermes_root, tmp_path)
    planted = question_wake.plant(shell, question_wake.parse(PROMPT), timeout=30)

    assert question_wake.archive(shell, planted.card, timeout=30) is True
    assert _board(tmp_path)["tasks"][planted.card]["status"] == "archived"


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("", "did not run"),
        (f"{question_wake.REPLAY_PRESENT}\nnot json", "not JSON"),
        (f"{question_wake.REPLAY_PRESENT}\n[]", "not an object"),
        (f'{question_wake.REPLAY_PRESENT}\n{{"error": "ImportError: no hermes"}}', "no hermes"),
        (f'{question_wake.REPLAY_PRESENT}\n{{"card": "t_1", "wake": " "}}', "no wake"),
    ],
)
def test_plant_refuses_a_reply_it_cannot_trust(reply: str, reason: str) -> None:
    with pytest.raises(question_wake.ReplayUnavailable, match=reason):
        question_wake.plant(lambda command, timeout: reply, question_wake.parse(PROMPT), 30)


def test_archive_is_best_effort() -> None:
    assert question_wake.archive(lambda command, timeout: "", "t_1", 30) is False


_PLANTED = question_wake.Planted(card="t_1", wake="[kanban] Task t_1 blocked.", posted=1)


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

    merged = question_wake.merge(_PLANTED, wake, answer)

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

    merged = question_wake.merge(_PLANTED, wake, answer)

    assert merged.tokens == {"input": 40, "output": 5, "total": 45}
    assert merged.output == ""
    assert merged.metadata["final_message"] == ""


def test_merge_keeps_both_turns_errors() -> None:
    wake = AgentResult(output="a", trajectory=[], errors=["first"])
    answer = AgentResult(output="b", trajectory=[], errors=["second"])

    assert question_wake.merge(_PLANTED, wake, answer).errors == ["first", "second"]
