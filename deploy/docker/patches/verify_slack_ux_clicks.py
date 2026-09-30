#!/usr/bin/env python3
"""Build-time behaviour gate for the KAGE_SLACK_UX button-click patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_slack_ux_clicks.py``, with ``slack_presenter.py``
staged beside this script (``/opt/defaults/scripts`` is not populated yet at
that point in the build).

Two things are checked:

1. The adapter. ``_register_bolt_handlers`` still wires the plugin action
   handlers, and the flag guard calling ``_kage_slack_clicks.register(self)``
   follows that call directly. The import the guard names is bound at module
   level.
2. The runtime module, loaded by path from ``gateway/`` and driven with a stub
   adapter: flag off it registers nothing; flag on, an authorized choice click
   rewrites the message, echoes, and reaches the message handler as the
   clicker's message in the thread; an unauthorized one does none of that.

A click that reaches nothing raises nothing: Slack shows the button as
clicked and the gateway logs at debug. The build is where it is caught.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

ADAPTER = "plugins/platforms/slack/adapter.py"
RUNTIME = "gateway/slack_ux_clicks.py"
FLAG_ENV = "KAGE_SLACK_UX"

METHOD = "_register_bolt_handlers"
PLUGIN_WIRING = "_register_plugin_action_handlers"
GUARD_ALIAS = "_kage_slack_clicks"
IMPORT_MODULE = "gateway"
IMPORT_NAME = "slack_ux_clicks"

CHANNEL = "C0KAGE"
TEAM = "T0KAGE"
USER = "U0KAGE"
MESSAGE_TS = "1700000000.000200"
THREAD = "1700000000.000100"
ACTION_TS = "1700000001.000300"
LABEL = "Raise to 512Mi"
CHOICE_ID = "kage.choice.0"


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_ux_clicks verify: {detail}")


def _is_self_call(stmt: ast.stmt, name: str) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Attribute)
        and stmt.value.func.attr == name
        and isinstance(stmt.value.func.value, ast.Name)
        and stmt.value.func.value.id == "self"
    )


def _is_guard(stmt: ast.stmt) -> bool:
    """``if _kage_slack_clicks.enabled(): _kage_slack_clicks.register(self)``."""
    if not isinstance(stmt, ast.If) or stmt.orelse or len(stmt.body) != 1:
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
    body = stmt.body[0]
    if not (isinstance(body, ast.Expr) and isinstance(body.value, ast.Call)):
        return False
    call = body.value
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "register"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == GUARD_ALIAS
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "self"
    )


def check_adapter(root: Path) -> None:
    path = root / ADAPTER
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    tree = ast.parse(path.read_text())
    methods = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == METHOD]
    if len(methods) != 1:
        raise _fail(f"{ADAPTER} has {len(methods)} def {METHOD}(), expected 1")
    body = methods[0].body
    wired = [i for i, stmt in enumerate(body) if _is_self_call(stmt, PLUGIN_WIRING)]
    if len(wired) != 1:
        raise _fail(f"{METHOD}() no longer calls self.{PLUGIN_WIRING}() once")
    after = wired[0] + 1
    if after >= len(body) or not _is_guard(body[after]):
        raise _fail(f"{METHOD}() does not run the {FLAG_ENV} guard right after self.{PLUGIN_WIRING}()")
    bound = any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == IMPORT_MODULE
        and any(a.name == IMPORT_NAME and a.asname == GUARD_ALIAS for a in stmt.names)
        for stmt in tree.body
    )
    if not bound:
        raise _fail(f"{ADAPTER} does not import {IMPORT_MODULE}.{IMPORT_NAME} as {GUARD_ALIAS}")


class _App:
    def __init__(self) -> None:
        self.listeners: list[tuple] = []

    def action(self, matcher):
        def wire(handler):
            self.listeners.append((matcher, handler))
            return handler

        return wire


class _Client:
    def __init__(self, log: list) -> None:
        self.log = log

    async def chat_update(self, **kwargs):
        self.log.append(("chat_update", kwargs))

    async def chat_postMessage(self, **kwargs):
        self.log.append(("chat_postMessage", kwargs))


class _StubAdapter:
    def __init__(self, authorized: bool) -> None:
        self.authorized = authorized
        self.log: list[tuple] = []
        self._app = _App()

    async def _begin_interaction(self, ack, body, action, kind, *, team_scoped=True):
        await ack()
        if not self.authorized:
            return None
        message = body["message"]
        return (TEAM, action["action_id"], action["value"], message, message["ts"], CHANNEL, "someone", USER)

    def _slack_allowed_channels(self):
        return set()

    def _slack_disable_dms(self):
        return False

    def _get_client(self, chat_id, team_id=None):
        return _Client(self.log)

    async def _handle_slack_message(self, event, payload=None):
        self.log.append(("message", event))


def _load_runtime(root: Path):
    path = root / RUNTIME
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("slack_ux_clicks_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module._presenter is None:
        raise _fail("slack_presenter did not import beside the runtime module")
    return module


async def _ack() -> None:
    return None


def _click():
    body = {
        "message": {
            "ts": MESSAGE_TS,
            "thread_ts": THREAD,
            "text": "fallback",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "*pick one*"}},
                {"type": "actions", "elements": [{"type": "button", "action_id": CHOICE_ID, "value": LABEL}]},
            ],
        }
    }
    return body, {
        "action_id": CHOICE_ID, "text": {"type": "plain_text", "text": LABEL}, "value": LABEL,
        "action_ts": ACTION_TS,
    }


async def _drive(module) -> None:
    os.environ.pop(FLAG_ENV, None)
    if module.enabled():
        raise _fail(f"enabled() is true with {FLAG_ENV} unset")
    os.environ[FLAG_ENV] = "1"
    try:
        adapter = _StubAdapter(authorized=True)
        module.register(adapter)
        if len(adapter._app.listeners) != 3:
            raise _fail(f"register() wired {len(adapter._app.listeners)} listeners, expected 3")
        await module.answer(adapter, _ack, *_click(), module.CHOICE_KIND)
        kinds = [entry[0] for entry in adapter.log]
        if kinds != ["chat_update", "chat_postMessage", "message"]:
            raise _fail(f"an authorized choice click made {kinds!r}")
        update, echo, turn = (entry[1] for entry in adapter.log)
        if any(b.get("type") == "actions" for b in update["blocks"]):
            raise _fail("the answered choice buttons are still on the message")
        if echo["thread_ts"] != THREAD or f"<@{USER}>" not in echo["text"]:
            raise _fail(f"the echo was {echo!r}")
        expected = {"user": USER, "text": LABEL, "channel": CHANNEL, "thread_ts": THREAD, "ts": ACTION_TS}
        if {k: turn.get(k) for k in expected} != expected:
            raise _fail(f"the turn was {turn!r}")

        module._answered.clear()
        stranger = _StubAdapter(authorized=False)
        await module.answer(stranger, _ack, *_click(), module.CHOICE_KIND)
        if stranger.log:
            raise _fail(f"an unauthorized click made {stranger.log!r}")
    finally:
        os.environ.pop(FLAG_ENV, None)


def main(root: Path = Path("/opt/hermes")) -> None:
    check_adapter(root)
    asyncio.run(_drive(_load_runtime(root)))
    print(
        "slack_ux_clicks verify: registration guarded after the plugin handlers; "
        "an authorized choice click is echoed and runs as the clicker's turn, an unauthorized one does nothing"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
