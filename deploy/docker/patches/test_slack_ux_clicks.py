"""Host tests for the KAGE_SLACK_UX button-click patch. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_slack_ux_clicks.py

The fixture carries the shape of upstream's ``_register_bolt_handlers`` around
the anchor (v2026.9.14). The tests apply the patch, exec both the patched and
the unpatched fixture, and compare the listeners each wires: with the flag off
they must be identical, which is the flag-off identity for this surface. The
runtime module is driven with a stub adapter.
"""

import asyncio
import importlib
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2] / "agents" / "platform" / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

import apply_slack_ux_clicks as applier
import slack_presenter as presenter
import slack_ux_clicks as runtime
import verify_slack_ux_clicks as verifier

UPSTREAM = '''\
"""Fixture standing in for plugins/platforms/slack/adapter.py."""
import re


class App:
    def __init__(self):
        self.listeners = []

    def action(self, matcher):
        def wire(handler):
            self.listeners.append(matcher)
            return handler

        return wire


class SlackAdapter:
    def __init__(self):
        self._app = App()

    def _handle_clarify_action(self, ack, body, action):
        return None

    def _register_plugin_action_handlers(self) -> None:
        """Wire plugin handlers."""
        self._app.action("plugin_action")(self._handle_clarify_action)

    def _wire_plugin_handlers(self, app):
        return None

    def _register_bolt_handlers(self) -> None:
        """Wire every Bolt listener onto ``self._app``; must run before Socket Mode starts."""
        self._app.action(re.compile(r"^hermes_clarify_choice_\\d+$"))(self._handle_clarify_action)
        self._register_plugin_action_handlers()
        # ctx.register_platform_handler("slack", ...) factories get the full
        # AsyncApp surface (event/action/command), wired before Socket Mode starts.
        self._wire_plugin_handlers(self._app)
'''

CHANNEL = "C1"
TEAM = "T1"
USER = "U1"
MESSAGE_TS = "222.000"
THREAD = "111.000"
ACTION_TS = "333.000"


def _run(coro):
    return asyncio.run(coro)


class _Root:
    """A throwaway Hermes root holding the fixture adapter and the runtime module."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        adapter = self.dir / applier.RELATIVE
        adapter.parent.mkdir(parents=True)
        adapter.write_text(UPSTREAM)
        gateway = self.dir / "gateway"
        gateway.mkdir()
        shutil.copy(HERE / "slack_ux_clicks.py", gateway / "slack_ux_clicks.py")
        (gateway / "__init__.py").write_text("")

    def load(self, name):
        """Exec the (possibly patched) fixture adapter with ``gateway`` importable."""
        sys.path.insert(0, str(self.dir))
        try:
            sys.modules.pop("gateway", None)
            sys.modules.pop("gateway.slack_ux_clicks", None)
            namespace = {"__name__": name}
            exec(compile((self.dir / applier.RELATIVE).read_text(), name, "exec"), namespace)  # noqa: S102
            return namespace
        finally:
            sys.path.remove(str(self.dir))

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def _listeners(namespace):
    adapter = namespace["SlackAdapter"]()
    adapter._register_bolt_handlers()
    return adapter._app.listeners


class ApplierTest(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)

    def test_applies_once(self):
        applier.apply(self.root.dir)
        text = (self.root.dir / applier.RELATIVE).read_text()
        self.assertEqual(text.count(applier.BUILD_MARKER), 1)
        with self.assertRaises(SystemExit):
            applier.apply(self.root.dir)

    def test_drifted_anchor_fails_loudly(self):
        path = self.root.dir / applier.RELATIVE
        drifted = UPSTREAM.replace("        self._register_plugin_action_handlers()\n", "")
        path.write_text(drifted)
        with self.assertRaises(SystemExit) as caught:
            applier.apply(self.root.dir)
        self.assertIn("plugin action handler wiring", str(caught.exception))
        self.assertEqual(path.read_text(), drifted)

    def test_verifier_passes_on_patched_tree(self):
        applier.apply(self.root.dir)
        verifier.main(self.root.dir)

    def test_verifier_refuses_unpatched_tree(self):
        with self.assertRaises(SystemExit):
            verifier.main(self.root.dir)


class FlagOffIdentityTest(unittest.TestCase):
    """With KAGE_SLACK_UX off the patched adapter wires exactly upstream's listeners."""

    def test_identical_to_upstream(self):
        root = _Root()
        self.addCleanup(root.cleanup)
        upstream = root.load("upstream_fixture")
        applier.apply(root.dir)
        patched = root.load("patched_fixture")
        for value in (None, "", "0", "false"):
            with self.subTest(flag=value), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("KAGE_SLACK_UX", None)
                if value is not None:
                    os.environ["KAGE_SLACK_UX"] = value
                self.assertEqual(_listeners(patched), _listeners(upstream))

    def test_flag_on_registers_after_the_plugin_handlers(self):
        root = _Root()
        self.addCleanup(root.cleanup)
        applier.apply(root.dir)
        patched = root.load("patched_fixture_on")
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"}):
            listeners = _listeners(patched)
        self.assertEqual(listeners[1], "plugin_action")
        self.assertEqual(
            listeners[2:],
            [presenter.CHOICE_ACTION_ID_PATTERN, presenter.STOP_ACTION_ID, presenter.LINK_ACTION_ID_PATTERN],
        )


class ActionIdTest(unittest.TestCase):
    def test_choice_pattern_matches_the_presenters_choice_buttons_only(self):
        blocks = presenter.blocks_answer("h", links=[("l", "https://l")], choices=["a", "b"], action_id_prefix="triage")
        ids = [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
        self.assertEqual(
            [bool(presenter.CHOICE_ACTION_ID_PATTERN.search(i)) for i in ids], [False, True, True]
        )
        for other in ("hermes_clarify_choice_0", "hermes_approve_once", presenter.STOP_ACTION_ID):
            self.assertIsNone(presenter.CHOICE_ACTION_ID_PATTERN.search(other))


class _Client:
    def __init__(self, log, fail=()):
        self.log = log
        self.fail = fail

    async def chat_update(self, **kwargs):
        if "chat_update" in self.fail:
            raise RuntimeError("update refused")
        self.log.append(("chat_update", kwargs))

    async def chat_postMessage(self, **kwargs):
        if "chat_postMessage" in self.fail:
            raise RuntimeError("post refused")
        self.log.append(("chat_postMessage", kwargs))


class _Adapter:
    def __init__(self, authorized=True, fail=()):
        self.authorized = authorized
        self.log = []
        self.acks = 0
        self.fail = fail

    async def _begin_interaction(self, ack, body, action, kind, *, team_scoped=True):
        await ack()
        self.kind = kind
        if not self.authorized:
            return None
        message = body.get("message", {})
        return (
            TEAM, action.get("action_id", ""), action.get("value", ""), message, message.get("ts", ""),
            CHANNEL, "someone", USER,
        )

    def _get_client(self, chat_id, team_id=None):
        return _Client(self.log, self.fail)

    async def _handle_slack_message(self, event, payload=None):
        self.log.append(("message", event))


def _message(thread=THREAD):
    message = {
        "ts": MESSAGE_TS,
        "text": "fallback",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Raise the limit?*"}},
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "action_id": "kage.link.0", "url": "https://x"},
                    {"type": "button", "action_id": "kage.choice.0", "value": "Raise to 512Mi"},
                    {"type": "button", "action_id": "kage.choice.1", "value": "Leave it"},
                ],
            },
            {"type": "actions", "block_id": "kage_stop", "elements": [{"type": "button", "action_id": "kage_stop"}]},
        ],
    }
    if thread:
        message["thread_ts"] = thread
    return message


def _choice(index=1, value="Leave it", **message_kwargs):
    action = {"action_id": f"kage.choice.{index}", "value": value, "action_ts": ACTION_TS}
    return {"message": _message(**message_kwargs)}, action


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        importlib.reload(runtime)
        patcher = mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ack(self, adapter):
        async def ack():
            adapter.acks += 1

        return ack

    def _answer(self, adapter, body, action, kind=runtime.CHOICE_KIND):
        _run(runtime.answer(adapter, self._ack(adapter), body, action, kind))

    def test_choice_is_the_clickers_turn_with_an_echo(self):
        adapter = _Adapter()
        self._answer(adapter, *_choice())
        self.assertEqual([entry[0] for entry in adapter.log], ["chat_update", "chat_postMessage", "message"])
        update, echo, turn = (entry[1] for entry in adapter.log)
        self.assertEqual((update["channel"], update["ts"]), (CHANNEL, MESSAGE_TS))
        actions = [b for b in update["blocks"] if b["type"] == "actions"]
        self.assertEqual(
            [[e["action_id"] for e in b["elements"]] for b in actions], [["kage.link.0"], ["kage_stop"]]
        )
        self.assertEqual(update["blocks"][-1]["elements"][0]["text"], "✓ <@U1>: Leave it")
        self.assertEqual(echo, {"channel": CHANNEL, "thread_ts": THREAD, "text": "↳ <@U1>: Leave it"})
        self.assertEqual(
            turn,
            {
                "type": "message", "user": USER, "text": "Leave it", "channel": CHANNEL, "ts": ACTION_TS,
                "thread_ts": THREAD, "_hermes_force_process": True, "team": TEAM,
                "_kage_click": {"action_id": "kage.choice.1", "message_ts": MESSAGE_TS},
            },
        )
        self.assertEqual(adapter.acks, 1)

    def test_unlisted_user_changes_nothing(self):
        adapter = _Adapter(authorized=False)
        self._answer(adapter, *_choice())
        self.assertEqual(adapter.log, [])
        self.assertEqual(adapter.acks, 1)
        # And the message is still answerable by someone who is listed.
        listed = _Adapter()
        self._answer(listed, *_choice())
        self.assertEqual(len(listed.log), 3)

    def test_a_message_is_answered_once(self):
        adapter = _Adapter()
        self._answer(adapter, *_choice(1, "Leave it"))
        self._answer(adapter, *_choice(0, "Raise to 512Mi"))
        turns = [entry[1]["text"] for entry in adapter.log if entry[0] == "message"]
        self.assertEqual(turns, ["Leave it"])

    def test_stop_is_the_clicker_typing_stop(self):
        adapter = _Adapter()
        body = {"message": _message()}
        self._answer(adapter, body, {"action_id": "kage_stop", "value": "stop"}, runtime.STOP_KIND)
        update, echo, turn = (entry[1] for entry in adapter.log)
        actions = [b for b in update["blocks"] if b["type"] == "actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(actions[0]["elements"]), 3)
        self.assertEqual(echo["text"], "↳ <@U1>: stop")
        self.assertEqual(turn["text"], "/stop")
        self.assertTrue(turn["ts"].startswith("kage-click-"))
        # A later choice on the same message is still its own answer.
        self._answer(adapter, *_choice())
        self.assertEqual(adapter.log[-1][1]["text"], "Leave it")

    def test_label_is_escaped_in_what_slack_shows_but_not_in_the_turn(self):
        adapter = _Adapter()
        self._answer(adapter, *_choice(value="<!channel> & go"))
        update, echo, turn = (entry[1] for entry in adapter.log)
        self.assertEqual(echo["text"], "↳ <@U1>: &lt;!channel&gt; &amp; go")
        self.assertNotIn("<!channel>", update["blocks"][-1]["elements"][0]["text"])
        self.assertEqual(turn["text"], "<!channel> & go")

    def test_top_level_message_threads_under_itself(self):
        adapter = _Adapter()
        self._answer(adapter, *_choice(thread=None))
        self.assertEqual(adapter.log[1][1]["thread_ts"], MESSAGE_TS)
        self.assertEqual(adapter.log[2][1]["thread_ts"], MESSAGE_TS)

    def test_failed_rewrite_and_echo_still_run_the_turn(self):
        adapter = _Adapter(fail=("chat_update", "chat_postMessage"))
        with self.assertLogs(runtime.logger, level="WARNING"):
            self._answer(adapter, *_choice())
        self.assertEqual([entry[0] for entry in adapter.log], ["message"])

    def test_empty_label_does_nothing(self):
        adapter = _Adapter()
        self._answer(adapter, *_choice(value="  "))
        self.assertEqual(adapter.log, [])

    def test_link_click_is_acked_only(self):
        acks = []

        async def ack():
            acks.append(True)

        _run(presenter.ack_link_click(ack, {}, {"action_id": "kage.link.0"}))
        self.assertEqual(acks, [True])

    def test_flag_off_is_disabled(self):
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "0"}):
            self.assertFalse(runtime.enabled())


class AnsweredBlocksTest(unittest.TestCase):
    def test_non_action_blocks_are_kept_in_order(self):
        blocks = _message()["blocks"]
        out = runtime.answered_blocks(blocks, lambda i: bool(re.search(r"\.choice\.", i)), "note")
        self.assertEqual(out[0], blocks[0])
        self.assertEqual(out[-1], {"type": "context", "elements": [{"type": "mrkdwn", "text": "note"}]})
        self.assertEqual(blocks[1]["elements"][1]["action_id"], "kage.choice.0")  # input untouched


if __name__ == "__main__":
    unittest.main()
