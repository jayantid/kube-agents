#!/usr/bin/env python3
"""Build-time behaviour gate for the KAGE_SLACK_UX reactions patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_slack_ux_reactions.py``, with ``slack_presenter.py``
staged beside this script (``/opt/defaults/scripts`` is not populated yet at
that point in the build).

Three things are checked:

1. The adapter. Each hook's first statement after its docstring is the flag
   guard handing over to ``_kage_slack_ux``, and upstream's body still follows
   it — so with the flag off the hook is upstream's. That the body still
   reacts is ``verify_slack_reactions_scope.py``'s check, run just before this
   one. The import the guard names is bound at module level. The
   members the runtime calls on the adapter are still there in the shape it
   calls them: ``_reacting_target(event)`` returning a 3-tuple,
   ``_react(channel, ts, emoji, team_id, *, remove)``,
   ``_reacting_message_ids`` as a set, ``_reactions_enabled()``, and
   ``_track_reacting_message(team_id, ts)``. And what the runtime reads to
   tell an admitted inbound message from the rest: ``_build_message_event``
   passes the Slack event as ``raw_message`` and its ts as ``message_id``,
   ``_handle_slack_message_impl`` hands it that event's own ``ts``, and
   ``_synthetic_reaction_event`` marks a reaction trigger with the key the
   runtime skips.
2. The board read. The runtime's own kanban query runs against real boards
   made with the built tree's ``hermes_cli``: an open card subscribed to the
   thread on the default board and on a second board is found, and a finished
   card, a card in another thread and a card for another platform are not.
   A card another card's worker created is found through the subscription
   Hermes copies onto it, with that card as its creator.
3. The runtime module, loaded by path from ``gateway/`` and driven with a stub
   adapter: flag off it is inert; flag on, an ask gets the arrival reaction for
   its kind, a direct answer settles at once, a delegated one waits for the
   notifier, and no call anywhere is a removal. An admitted ask upstream did
   not track is reacted to the same way; a reaction trigger is not.

Every failure here is silent in production — a reaction the runtime never
attempts raises and logs nothing — so the build is where it is caught.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ADAPTER = "plugins/platforms/slack/adapter.py"
RUNTIME = "gateway/slack_ux_reactions.py"
FLAG_ENV = "KAGE_SLACK_UX"

#: The adapter hooks the patch guards; each guard calls the runtime's function
#: of the same name.
HOOKS = ("on_processing_start", "on_processing_complete")
GUARD_ALIAS = "_kage_slack_ux"
IMPORT_MODULE = "gateway"
IMPORT_NAME = "slack_ux_reactions"
ADAPTER_CLASS = "SlackAdapter"
UPSTREAM_HELPER = "_react"
TARGET_HELPER = "_reacting_target"
TRACKED_SET = "_reacting_message_ids"
#: ``_react``'s positional parameters after ``self``, in the order the runtime
#: passes them, and its keyword-only one.
REACT_POSITIONAL = ("channel", "timestamp", "emoji", "team_id")
REACT_KEYWORD = "remove"
#: ``_reacting_target``'s return, in the order the runtime unpacks it.
TARGET_RETURN = ("ts", "team_id", "marker")
ENABLED_HELPER = "_reactions_enabled"
TRACK_HELPER = "_track_reacting_message"
#: ``_track_reacting_message``'s parameters after ``self``, as the runtime passes them.
TRACK_POSITIONAL = ("team_id", "ts")
#: Where upstream builds an inbound message's ``MessageEvent``, and the keywords
#: the runtime reads to know it for one: the Slack event, and that event's ts.
BUILD_EVENT = "_build_message_event"
BUILD_EVENT_KEYWORDS = {"raw_message": "event", "message_id": "ts"}
#: Where upstream calls ``_build_message_event`` for an inbound message, and
#: the Slack event's key it reads the ``ts`` it passes from.
INBOUND_HANDLER = "_handle_slack_message_impl"
EVENT_TS_KEY = "ts"
SYNTHETIC_REACTION = "_synthetic_reaction_event"
#: The runtime's ``FORCED_EVENT_KEY``.
FORCED_EVENT_KEY = "_hermes_force_process"

#: Environment the board check pins, as the sibling kanban verifiers do.
KANBAN_HOME_ENV = "HERMES_KANBAN_HOME"
KANBAN_UNSET_ENV = ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK")
SECOND_BOARD = "verify-second"
OTHER_THREAD = "1700000000.000200"
OTHER_PLATFORM = "telegram"

ASK_TS = "1700000000.000100"
CHANNEL = "C0KAGE"
THREAD = "1700000000.000100"
TEAM = "T0KAGE"
MARKER = "marker"
CARD = "t_verify"


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_ux_reactions verify: {detail}")


def _is_guard(stmt: ast.stmt, target: str) -> bool:
    """``if _kage_slack_ux.enabled(): return await _kage_slack_ux.<target>(self, ...)``."""
    if not isinstance(stmt, ast.If) or stmt.orelse:
        return False
    test = stmt.test
    if not (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Attribute)
        and test.func.attr == "enabled"
        and isinstance(test.func.value, ast.Name)
        and test.func.value.id == GUARD_ALIAS
    ):
        return False
    if len(stmt.body) != 1 or not isinstance(stmt.body[0], ast.Return):
        return False
    value = stmt.body[0].value
    if not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
        return False
    call = value.value
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == target
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == GUARD_ALIAS
        and bool(call.args)
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "self"
    )


def check_adapter(root: Path) -> None:
    path = root / ADAPTER
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    tree = ast.parse(path.read_text())
    hooks = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name in HOOKS
    }
    for name in HOOKS:
        node = hooks.get(name)
        if node is None:
            raise _fail(f"{ADAPTER} has no async def {name}()")
        body = node.body
        if len(body) < 3 or not _is_guard(body[1], name):
            raise _fail(f"{name}() does not open with the {FLAG_ENV} guard after its docstring")
    bound = any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == IMPORT_MODULE
        and any(a.name == IMPORT_NAME and a.asname == GUARD_ALIAS for a in stmt.names)
        for stmt in tree.body
    )
    if not bound:
        raise _fail(f"{ADAPTER} does not import {IMPORT_MODULE}.{IMPORT_NAME} as {GUARD_ALIAS}")
    _check_members(tree)


def _method(adapter: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    # The adapter class's own definition: one elsewhere in the module, or on a
    # mixin, is not what the runtime calls through the adapter.
    for node in adapter.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise _fail(f"{ADAPTER_CLASS} has no def {name}() for the runtime to call")


def _check_members(tree: ast.Module) -> None:
    """The adapter members ``slack_ux_reactions`` calls, in the shape it calls them."""
    adapter = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == ADAPTER_CLASS), None,
    )
    if adapter is None:
        raise _fail(f"{ADAPTER} has no class {ADAPTER_CLASS}")
    react = _method(adapter, UPSTREAM_HELPER)
    if not isinstance(react, ast.AsyncFunctionDef):
        raise _fail(f"{UPSTREAM_HELPER}() is no longer async")
    positional = tuple(a.arg for a in react.args.posonlyargs + react.args.args)
    if positional[1:] != REACT_POSITIONAL or [a.arg for a in react.args.kwonlyargs] != [REACT_KEYWORD]:
        raise _fail(
            f"{UPSTREAM_HELPER}() is not (self, {', '.join(REACT_POSITIONAL)}, *, {REACT_KEYWORD}): {positional}"
        )
    target = _method(adapter, TARGET_HELPER)
    if isinstance(target, ast.AsyncFunctionDef) or len(target.args.args) != 2:
        raise _fail(f"{TARGET_HELPER}() is not a plain (self, event) method")
    # Every value it can return, both arms of a conditional included: the
    # runtime unpacks whatever is not None into three names, so one other
    # tuple on any path raises there.
    values = [node.value for node in ast.walk(target) if isinstance(node, ast.Return)]
    shapes = []
    while values:
        value = values.pop()
        if isinstance(value, ast.IfExp):
            values += [value.body, value.orelse]
        elif value is None or (isinstance(value, ast.Constant) and value.value is None):
            continue
        elif isinstance(value, ast.Tuple):
            shapes.append(tuple(e.id if isinstance(e, ast.Name) else None for e in value.elts))
        else:
            shapes.append(None)
    if not shapes or any(shape != TARGET_RETURN for shape in shapes):
        raise _fail(f"{TARGET_HELPER}() does not return only ({', '.join(TARGET_RETURN)}) or None: {shapes}")
    # Every assignment in the adapter class, not one anywhere in the module:
    # the runtime calls .discard() on the attribute the adapter holds.
    kinds = []
    for node in ast.walk(adapter):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(tg, ast.Attribute) and tg.attr == TRACKED_SET for tg in targets):
                continue
            value = node.value
            kinds.append(
                isinstance(value, ast.Set)
                or (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "set")
            )
    if not kinds or not all(kinds):
        raise _fail(f"{ADAPTER_CLASS} does not only ever assign self.{TRACKED_SET} a set")
    enabled = _method(adapter, ENABLED_HELPER)
    if isinstance(enabled, ast.AsyncFunctionDef) or len(enabled.args.args) != 1:
        raise _fail(f"{ENABLED_HELPER}() is not a plain (self) method")
    track = _method(adapter, TRACK_HELPER)
    if isinstance(track, ast.AsyncFunctionDef) or tuple(a.arg for a in track.args.args[1:]) != TRACK_POSITIONAL:
        raise _fail(f"{TRACK_HELPER}() is not a plain (self, {', '.join(TRACK_POSITIONAL)}) method")
    _check_inbound(adapter)


def _check_inbound(adapter: ast.ClassDef) -> None:
    """What the runtime reads to tell an admitted inbound message from a trigger or a command.

    Drift here is silent: a renamed keyword drops admitted thread replies back
    to no reaction at all, and a renamed key reacts to a reaction's own ts.
    """
    build = _method(adapter, BUILD_EVENT)
    calls = [
        node for node in ast.walk(build)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "MessageEvent"
    ]
    if len(calls) != 1:
        raise _fail(f"{BUILD_EVENT}() does not build exactly one MessageEvent: {len(calls)}")
    passed = {k.arg: k.value.id for k in calls[0].keywords if isinstance(k.value, ast.Name)}
    if any(passed.get(key) != name for key, name in BUILD_EVENT_KEYWORDS.items()):
        raise _fail(f"{BUILD_EVENT}() no longer passes {BUILD_EVENT_KEYWORDS} to MessageEvent: {passed}")
    handler = _method(adapter, INBOUND_HANDLER)
    builds = [
        node for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == BUILD_EVENT
    ]
    ts_args = [
        k.value.id for call in builds for k in call.keywords
        if k.arg == BUILD_EVENT_KEYWORDS["message_id"] and isinstance(k.value, ast.Name)
    ]
    if len(builds) != 1 or len(ts_args) != 1:
        raise _fail(f"{INBOUND_HANDLER}() no longer calls {BUILD_EVENT}() once with ts=<name>")
    sources = [
        node.value for node in ast.walk(handler)
        if isinstance(node, ast.Assign)
        and any(isinstance(tg, ast.Name) and tg.id == ts_args[0] for tg in node.targets)
    ]
    if not sources or not all(
        isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "get"
        and isinstance(v.func.value, ast.Name) and v.func.value.id == BUILD_EVENT_KEYWORDS["raw_message"]
        and v.args and isinstance(v.args[0], ast.Constant) and v.args[0].value == EVENT_TS_KEY
        for v in sources
    ):
        raise _fail(f"{INBOUND_HANDLER}() no longer takes the ts it builds with from event.get({EVENT_TS_KEY!r})")
    synthetic = _method(adapter, SYNTHETIC_REACTION)
    keys = {
        key.value for node in ast.walk(synthetic) if isinstance(node, ast.Dict)
        for key in node.keys if isinstance(key, ast.Constant)
    }
    if FORCED_EVENT_KEY not in keys:
        raise _fail(f"{SYNTHETIC_REACTION}() no longer marks its event with {FORCED_EVENT_KEY!r}")


def check_board_read(module, root: Path) -> None:
    """Run the runtime's kanban query against real boards built with ``hermes_cli``."""
    added = str(root) not in sys.path
    if added:
        sys.path.insert(0, str(root))
    saved = {name: os.environ.get(name) for name in (KANBAN_HOME_ENV, *KANBAN_UNSET_ENV)}
    home = Path(tempfile.mkdtemp())
    os.environ[KANBAN_HOME_ENV] = str(home)
    for name in KANBAN_UNSET_ENV:
        os.environ.pop(name, None)
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kc
        from hermes_cli import kanban_db_notify as kn

        kb.create_board(SECOND_BOARD)
        expected = set()

        def card(
            board: str, title: str, thread: str = THREAD, platform: str = "slack", done: bool = False,
        ) -> tuple[str, str]:
            conn = kc.connect(board=board)
            try:
                task = kb.create_task(conn, title=title, assignee="platform")
                kn.add_notify_sub(conn, task_id=task, platform=platform, chat_id=CHANNEL, thread_id=thread)
                if done:
                    kb.complete_task(conn, task, result="verified")
                elif thread == THREAD and platform == "slack":
                    expected.add((board, task))
            finally:
                conn.close()
            return board, task

        _, fresh = card(kb.DEFAULT_BOARD, "open on default")
        _, creator = card(kb.DEFAULT_BOARD, "creator")
        conn = kc.connect(board=kb.DEFAULT_BOARD)
        try:
            # No subscription of its own: Hermes copies its creator's.
            child = kb.create_task(
                conn, title="spawned", assignee="platform", parents=(fresh,), creator_task_id=creator,
            )
        finally:
            conn.close()
        expected.add((kb.DEFAULT_BOARD, child))
        card(SECOND_BOARD, "open on the second board")
        _, asking = card(kb.DEFAULT_BOARD, "blocked on the user")
        _, parked = card(kb.DEFAULT_BOARD, "gave up")
        _, revived = card(kb.DEFAULT_BOARD, "gave up, then unblocked")
        conn = kc.connect(board=kb.DEFAULT_BOARD)
        try:
            kb.block_task(conn, asking, reason="which cluster?")
            kb.block_task(conn, parked, reason="first stop")
            # The dispatcher's give-up writes this event; reached here directly.
            kb._append_event(conn, parked, "gave_up", {"failures": 2})
            kb.block_task(conn, revived, reason="first stop")
            kb._append_event(conn, revived, "gave_up", {"failures": 2})
            kb.unblock_task(conn, revived)
        finally:
            conn.close()
        _, finished = card(kb.DEFAULT_BOARD, "finished", done=True)
        card(kb.DEFAULT_BOARD, "another thread", thread=OTHER_THREAD)
        card(SECOND_BOARD, "another platform", platform=OTHER_PLATFORM)
        # The query itself, not open_cards(), so a drift raises here instead
        # of being logged at debug and read as "no cards".
        read = module._query_open_cards(CHANNEL, THREAD)
        found = set(read)
        if found != expected:
            raise _fail(f"the kanban read found {sorted(found)!r}, expected {sorted(expected)!r}")
        made_by = read[(kb.DEFAULT_BOARD, child)].creator
        if made_by != creator or read[(kb.DEFAULT_BOARD, fresh)].creator is not None:
            raise _fail(f"the kanban read does not see which card created a card: {made_by!r}")
        stops = [read[(kb.DEFAULT_BOARD, task)] for task in (asking, parked, revived)]
        if [c.gave_up for c in stops] != [False, True, False] or stops[1].status != "blocked":
            raise _fail(f"the kanban read does not tell a give-up from a block on the user: {stops!r}")
        lineage = module._query_thread_lineage(CHANNEL, THREAD)
        if lineage.get((kb.DEFAULT_BOARD, child)) != creator or (kb.DEFAULT_BOARD, finished) not in lineage:
            raise _fail(f"the lineage read misses a creator or a completed card: {lineage!r}")
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(home, ignore_errors=True)
        if added:
            sys.path.remove(str(root))


class _StubAdapter:
    def __init__(self, tracked: bool = True) -> None:
        self.calls: list[tuple] = []
        self._reacting_message_ids = {MARKER} if tracked else set()

    def _reacting_target(self, event):
        return (ASK_TS, TEAM, MARKER) if MARKER in self._reacting_message_ids else None

    def _reactions_enabled(self):
        return True

    def _track_reacting_message(self, team_id, ts):
        if (team_id, ts) == (TEAM, ASK_TS):
            self._reacting_message_ids.add(MARKER)

    async def _react(self, channel, ts, emoji, team_id, *, remove):
        self.calls.append((channel, ts, emoji, team_id, remove))
        return True


def _event(text: str, raw: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        text=text, message_id=ASK_TS, raw_message=raw,
        source=SimpleNamespace(chat_id=CHANNEL, thread_id=THREAD, scope_id=TEAM),
    )


def _load_runtime(root: Path):
    path = root / RUNTIME
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("slack_ux_reactions_verify", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if module._presenter is None:
        raise _fail("slack_presenter did not import beside the runtime module")
    return module


async def _drive(module) -> None:
    success = SimpleNamespace(value="success")

    os.environ.pop(FLAG_ENV, None)
    if module.enabled():
        raise _fail(f"enabled() is true with {FLAG_ENV} unset")

    os.environ[FLAG_ENV] = "1"
    boards: list[dict] = []

    async def open_cards(chat_id, thread_id):
        return boards.pop(0)

    async def thread_lineage(chat_id, thread_id):
        return {}

    module.open_cards = open_cards
    module.thread_lineage = thread_lineage

    # A direct answer: arrival by kind, then the settle at once.
    adapter = _StubAdapter()
    boards[:] = [{}, {}]
    await module.on_processing_start(adapter, _event("fix it"))
    await module.on_processing_complete(adapter, _event("fix it"), success)
    expected = [
        (CHANNEL, ASK_TS, "hammer_and_wrench", TEAM, False),
        (CHANNEL, ASK_TS, "white_check_mark", TEAM, False),
    ]
    if adapter.calls != expected:
        raise _fail(f"direct answer reacted {adapter.calls!r}, expected {expected!r}")

    # A thread reply admitted without a mention, which upstream does not track.
    adapter = _StubAdapter(tracked=False)
    boards[:] = [{}, {}]
    await module.on_processing_start(adapter, _event("fix it", {"ts": ASK_TS}))
    await module.on_processing_complete(adapter, _event("fix it", {"ts": ASK_TS}), success)
    if adapter.calls != expected:
        raise _fail(f"admitted untracked ask reacted {adapter.calls!r}, expected {expected!r}")
    # A reaction trigger: its ts is the reaction's, so nothing.
    adapter = _StubAdapter(tracked=False)
    trigger = {"ts": ASK_TS, FORCED_EVENT_KEY: True}
    await module.on_processing_start(adapter, _event("reaction:added:+1", trigger))
    await module.on_processing_complete(adapter, _event("reaction:added:+1", trigger), success)
    if adapter.calls:
        raise _fail(f"reaction trigger reacted {adapter.calls!r}")

    # A delegated answer: nothing at completion; the notifier's terminal event settles it.
    adapter = _StubAdapter()
    # The third read is the settle's look for follow-up cards: none.
    boards[:] = [{}, {(module.DEFAULT_BOARD, CARD): module._Card("running")}, {}]
    await module.on_processing_start(adapter, _event("is seeded-a healthy?"))
    await module.on_processing_complete(adapter, _event("is seeded-a healthy?"), success)
    if adapter.calls != [(CHANNEL, ASK_TS, "eyes", TEAM, False)]:
        raise _fail(f"delegated answer settled before the notifier: {adapter.calls!r}")
    sub = {"platform": "slack", "chat_id": CHANNEL, "thread_id": THREAD, "task_id": CARD}
    await module.settle_delegated(adapter, sub, "completed")
    expected = [
        (CHANNEL, ASK_TS, "eyes", TEAM, False),
        (CHANNEL, ASK_TS, "white_check_mark", TEAM, False),
    ]
    if adapter.calls != expected:
        raise _fail(f"delegated answer reacted {adapter.calls!r}, expected {expected!r}")
    os.environ.pop(FLAG_ENV, None)


def main(root: Path = Path("/opt/hermes"), *, board_read: bool = True) -> None:
    check_adapter(root)
    module = _load_runtime(root)
    if board_read:
        check_board_read(module, root)
    asyncio.run(_drive(module))
    print(
        "slack_ux_reactions verify: both hooks guarded ahead of upstream's body; "
        "adapter members in the shape the runtime calls; board read finds open cards on every board; "
        "runtime reacts by kind, to admitted asks upstream did not track too, settles direct answers, defers delegated ones, never removes"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
