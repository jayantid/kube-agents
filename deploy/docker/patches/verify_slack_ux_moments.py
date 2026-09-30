"""Build-time behaviour gate for the KAGE_SLACK_UX moments module.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes`` once
``gateway/slack_ux_moments.py`` is installed, with ``slack_presenter.py`` and
``slack_moments.py`` staged beside this script (``/opt/defaults/scripts`` is
not populated yet at that point in the build).

Two things are checked:

1. ``gateway/kanban_progress_lines.py`` imports the module, since nothing else
   reaches it and an import that drifted would read as flag off.
2. The module, loaded by path and driven with a stub adapter: flag off it is
   inert; flag on, an opened PR posts once with its two url buttons, a PR
   that is only cited posts nothing, and a ``needs_input`` block posts its
   question with a choice per listed option while any other block posts
   nothing.

A refused post raises nothing and logs a warning, so the build is where a
broken layout is caught.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

RUNTIME = "gateway/slack_ux_moments.py"
CALLER = "gateway/kanban_progress_lines.py"
CALLER_IMPORT = "from gateway import slack_ux_moments"
FLAG_ENV = "KAGE_SLACK_UX"

CHANNEL = "C0KAGE"
THREAD = "1700000000.000100"
PR = "https://github.com/acme/fleet-config/pull/412"
OPENED = f"Opened PR {PR} raising the limit to 512Mi"
CITED = f"{PR} already covers this"
QUESTION = "Which checkout-gateway did you mean?\nTwo clusters run one.\n- seeded-reliability\n- seeded-debug"
CHOICES = ["seeded-reliability", "seeded-debug"]


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_ux_moments verify: {detail}")


class _StubClient:
    def __init__(self, posts: list) -> None:
        self.posts = posts

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ts": "1700000000.000300"}


class _StubAdapter:
    def __init__(self) -> None:
        self.posts: list = []

    def _get_client(self, chat_id, team_id=None):
        return _StubClient(self.posts)


def _buttons(blocks: list) -> list:
    return [e for b in blocks if b.get("type") == "actions" for e in b["elements"]]


def check_caller(root: Path) -> None:
    path = root / CALLER
    if not path.is_file() or CALLER_IMPORT not in path.read_text():
        raise _fail(f"{CALLER} does not import slack_ux_moments")


def _load_runtime(root: Path):
    path = root / RUNTIME
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("slack_ux_moments_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module._presenter is None or module._moments is None:
        raise _fail("slack_presenter or slack_moments did not import beside the runtime module")
    return module


async def _drive(module) -> None:
    os.environ.pop(FLAG_ENV, None)
    if module.enabled():
        raise _fail(f"enabled() is true with {FLAG_ENV} unset")
    os.environ[FLAG_ENV] = "1"
    sub = {"platform": "slack", "chat_id": CHANNEL, "thread_id": THREAD, "task_id": "t_verify"}

    adapter = _StubAdapter()
    await module.pr_opened(adapter, sub, CITED)
    await module.pr_opened(adapter, sub, OPENED)
    await module.pr_opened(adapter, sub, OPENED)
    if len(adapter.posts) != 1:
        raise _fail(f"the PR moment posted {len(adapter.posts)} times, expected once")
    urls = [b.get("url") for b in _buttons(adapter.posts[0]["blocks"])]
    if urls != [PR, PR + "/files"] or adapter.posts[0]["thread_ts"] != THREAD:
        raise _fail(f"the PR moment was {adapter.posts[0]!r}")

    adapter = _StubAdapter()
    await module.needs_you(adapter, sub, {"kind": "capability", "reason": QUESTION})
    await module.needs_you(adapter, sub, {"kind": "needs_input", "reason": QUESTION})
    if len(adapter.posts) != 1:
        raise _fail(f"the question posted {len(adapter.posts)} times, expected once")
    labels = [b["text"]["text"] for b in _buttons(adapter.posts[0]["blocks"])]
    if labels != CHOICES:
        raise _fail(f"the question's buttons were {labels!r}")
    os.environ.pop(FLAG_ENV, None)


def main(root: Path = Path("/opt/hermes")) -> None:
    check_caller(root)
    asyncio.run(_drive(_load_runtime(root)))
    print(
        "slack_ux_moments verify: reached from kanban_progress_lines; "
        "posts an opened PR once and a needs_input question with its choices"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
