"""Slack layout for the two moments that get a message of their own.

Pure functions only, like ``slack_presenter``, whose layout this reuses.
``gateway/slack_ux_moments.py`` decides when to post them.

* A pull request the work opened (:func:`pr_opened`): a bold headline naming
  the PR, the worker's own line below it as evidence, and "Open PR" and
  "Files changed" url buttons. :func:`opened_pr` finds it in a progress note or a report, and
  only where the line says it opened that PR, the verb in front of the url, so
  a note citing someone else's PR ("… pull/300, opened by bob") is not
  announced as ours.
* A question the work is waiting on (:func:`needs_you`): the worker's question
  in bold, the rest of its reason below it, a choice button for each option
  the reason lists, and a "waiting on you" line. A click is the clicker's
  answer in the thread (``gateway/slack_ux_clicks.py``), the same path as
  typing it. With no list, or with no thread for a click to answer in, the
  question is text only, and a typed reply is the answer.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import slack_presenter as _presenter

#: A pull request url: host, owner, repo and number, with any trailing path
#: (``/files``, ``#discussion``) left out of the match.
PR_URL = re.compile(r"https?://[^\s/<>|]+/([^\s/<>|]+)/([^\s/<>|]+)/pull/(\d+)")
#: What must sit right before the url for a line to announce the PR as ours:
#: the verb, then optionally "a"/"the", "new", and "PR"/"pull request".
OPENED_BEFORE_URL = re.compile(
    r"\b(?:opened|created|raised|filed|submitted)\s+(?:(?:an?|the)\s+)?(?:new\s+)?"
    r"(?:(?:PR|pull\s+request)\s*:?\s*)?<?$",
    re.IGNORECASE,
)

PR_HEADLINE = "I opened PR #{number} in {repo}. It's yours to review."
PR_REF = "PR #{number}"
#: The url in the worker's line, with a "PR" already in front of it, so
#: "Opened PR <url>" reads "Opened PR #412" rather than "Opened PR PR #412".
PR_REF_SPAN = r"(?:\bPR\s+)?{url}"
OPEN_PR = "Open PR ↗"
FILES_CHANGED = "Files changed ↗"
FILES_PATH = "/files"
PR_ACTION_PREFIX = "kage_pr"

#: An option line in a block reason: a bullet or a numbered item.
OPTION_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$")
#: Buttons only for two to five options, each short enough that Slack shows
#: all of it; otherwise the options stay in the text.
OPTIONS_MIN = 2
OPTIONS_MAX = _presenter.BUTTONS_PER_ROW
#: The reason below the question, clipped; ``kanban_block`` puts no bound on it.
DETAIL_MAX = 2000
NEEDS_YOU_ACTION_PREFIX = "kage_needs"
WAITING = "⏸ waiting on you"


def opened_pr(text: str) -> tuple[str, str, str, str] | None:
    """``(url, repo, number, line)`` for the first PR ``text`` says was opened, else None."""
    for line in str(text or "").splitlines():
        for match in PR_URL.finditer(line):
            if OPENED_BEFORE_URL.search(line[: match.start()]):
                return match.group(0), match.group(2), match.group(3), line.strip()
    return None


def _escape(text: str) -> str:
    for raw, escaped in _presenter.MRKDWN_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def _with_subline(blocks: list[dict], subline: str) -> list[dict]:
    """``blocks`` with ``subline`` as a context line under the headline, escaped so
    the worker's words cannot mention anyone."""
    if not subline:
        return blocks
    context = {"type": "context", "elements": [{"type": "mrkdwn", "text": _escape(subline)}]}
    return [*blocks[:1], context, *blocks[1:]]


def _text(headline_text: str, subline: str) -> str:
    return "\n".join(part for part in (headline_text, _escape(subline)) if part)


def pr_opened(url: str, repo: str, number: str, line: str) -> tuple[list[dict], str]:
    """Blocks and fallback text for an opened PR; ``line`` is the worker's, the url shortened."""
    headline = PR_HEADLINE.format(number=number, repo=repo)
    span = re.compile(PR_REF_SPAN.format(url=re.escape(url)), re.IGNORECASE)
    evidence = span.sub(PR_REF.format(number=number), line).strip()
    links = [(OPEN_PR, url), (FILES_CHANGED, url + FILES_PATH)]
    blocks = _presenter.blocks_answer(headline, links=links, action_id_prefix=PR_ACTION_PREFIX)
    first, *rest = _presenter.fallback_text(headline, links=links).split("\n")
    return _with_subline(blocks, evidence), "\n".join([_text(first, evidence), *rest])


def _question(reason: str, buttons: bool) -> tuple[str, list[str], list[str]]:
    """The reason's first line, the lines after it, and its options when they can be buttons."""
    lines = str(reason or "").strip().splitlines()
    if not lines:
        return "", [], []
    rest = lines[1:]
    options = [m.group(1) for m in (OPTION_LINE.match(line) for line in rest) if m]
    usable = buttons and OPTIONS_MIN <= len(options) <= OPTIONS_MAX and all(
        len(option) <= _presenter.BUTTON_TEXT_MAX for option in options
    )
    first = lines[0].strip()
    if len(first) > _presenter.HEADLINE_MAX:
        # The headline is clipped; the whole line goes below it too.
        rest = lines
    if not usable:
        return first, rest, []
    return first, [line for line in rest if not OPTION_LINE.match(line)], options


def _detail(lines: Sequence[str]) -> str:
    text = "\n".join(lines).strip()
    if len(text) > DETAIL_MAX:
        text = text[: DETAIL_MAX - len(_presenter.ELLIPSIS)].rstrip() + _presenter.ELLIPSIS
    return text


def needs_you(reason: str, buttons: bool = True) -> tuple[list[dict], str] | None:
    """Blocks and fallback text for a question the work waits on, or None for an empty reason.

    ``buttons`` False keeps the options in the text, as when there are too many.
    """
    headline, rest, options = _question(reason, buttons)
    if not headline:
        return None
    detail = _detail(rest)
    blocks = _with_subline(
        _presenter.blocks_answer(headline, choices=options, action_id_prefix=NEEDS_YOU_ACTION_PREFIX),
        detail,
    )
    blocks.append({
        "type": "context",
        "block_id": _presenter.WAITING_BLOCK_ID,
        "elements": [{"type": "mrkdwn", "text": WAITING}],
    })
    first, *more = _presenter.fallback_text(headline, choices=options).split("\n")
    return blocks, "\n".join([_text(first, detail), *more])


def needs_you_settled(blocks: Sequence[dict]) -> list[dict]:
    """A posted question's ``blocks`` once its card has moved on: no choice buttons, no waiting line."""
    out: list[dict] = []
    for block in blocks or ():
        if not isinstance(block, dict) or block.get("block_id") == _presenter.WAITING_BLOCK_ID:
            continue
        if block.get("type") == "actions":
            kept = [
                e for e in block.get("elements") or ()
                if not _presenter.CHOICE_ACTION_ID_PATTERN.search(str((e or {}).get("action_id") or ""))
            ]
            if not kept:
                continue
            block = {**block, "elements": kept}
        out.append(block)
    return out
