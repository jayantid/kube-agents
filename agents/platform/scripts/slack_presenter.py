"""Slack presentation for kube-agents: answer layout, buttons and reactions.

Pure functions, plus ``ack_link_click``, the no-op handler a caller registers
for link buttons. Nothing here imports the Hermes gateway, the Slack SDK or
the network, so any process that posts to Slack can use it, and it can move
with Slack ingress when it leaves the gateway. Its callers are the gateway
patches for reactions (``slack_ux_reactions``, which the kanban notifier
also reaches) and button clicks (``slack_ux_clicks``), which uses the action
ids and the link ack; and ``session_kv_server``'s cron relay, which gates the
fleet-audit report on :func:`enabled` and lays it out through
``slack_audit_report``. The answer layout has no caller yet.
Every caller reaches it through ``PYTHONPATH=/opt/defaults/scripts``, which the
operator sets on the agent container.

Everything a caller changes on screen is gated on :func:`enabled`, the
``KAGE_SLACK_UX`` environment variable, off by default. With it off, callers
take their upstream path unchanged; this module only answers questions.

Layout: :func:`split_answer` takes the headline off an agent's markdown
answer; url link buttons and choice buttons, whose value is the label, are
built by ``_button`` and wrapped into rows by ``_actions``; :func:`fallback_text`
is the headline, links and choices as plain mrkdwn, for the message's ``text``
field, with any report rows led by their severity as inline code (a bullet
when they have none).

Reports (:func:`blocks_report`): a headline, the top findings as one group
between dividers headed by their count ("2 critical"), the choice buttons then
the link buttons, and the rest in Slack's collapsible
``container`` block, which Slack opens and closes itself. Block Kit has no
bordered box a message can draw, so the dividers stand in for a border.
Where Slack refuses the container, ``fold_in_place=False`` leaves it out and
the caller posts the rest in the thread.

Reactions (:func:`arrival_reaction`, :func:`settle_reaction`): the first
reaction says what kind of ask arrived, chosen by keyword before any model
call; a second joins it when the work settles. The first is never removed; the
credential proxy refuses every Slack method ending in ``remove``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: The flag, and the values that turn it on. Anything else (unset included) is off.
FLAG_ENV = "KAGE_SLACK_UX"
FLAG_ON_VALUES = frozenset({"1", "true", "yes", "on"})

# --- reactions -------------------------------------------------------------

#: Slack emoji names (``reactions.add`` takes the name, not the glyph).
REACTION_QUESTION = "eyes"  # 👀
REACTION_CHANGE = "hammer_and_wrench"  # 🛠️
REACTION_BOARD = "clipboard"  # 📋
REACTION_INCIDENT = "rotating_light"  # 🚨
REACTION_DONE = "white_check_mark"  # ✅
REACTION_BLOCKED = "double_vertical_bar"  # ⏸️
REACTION_FAILED = "x"  # ❌

#: How work can settle, as the callers name it.
SETTLE_DONE = "done"
SETTLE_BLOCKED = "blocked"
SETTLE_FAILED = "failed"
SETTLE_REACTIONS = {
    SETTLE_DONE: REACTION_DONE,
    SETTLE_BLOCKED: REACTION_BLOCKED,
    SETTLE_FAILED: REACTION_FAILED,
}

#: Kanban notifier event kinds that settle delegated work. ``crashed`` and
#: ``timed_out`` are absent on purpose: the dispatcher retries both, so the work
#: has not settled. ``block_loop_detected`` sends the card back to triage, where
#: it can run again, so it settles provisionally, like ``blocked``. Bookkeeping
#: kinds (``status``, ``archived``, ``unblocked``) settle nothing.
SETTLE_BY_KANBAN_KIND = {
    "completed": SETTLE_DONE,
    "blocked": SETTLE_BLOCKED,
    "review_requested": SETTLE_BLOCKED,
    "changes_requested": SETTLE_BLOCKED,
    "gave_up": SETTLE_FAILED,
    "block_loop_detected": SETTLE_BLOCKED,
}

#: Settle outcomes after which a later settle can still follow on the same ask:
#: a blocked card is answered and runs on to done.
PROVISIONAL_SETTLES = frozenset({SETTLE_BLOCKED})

#: Keyword classes, tried in this order; the first match wins. A change request
#: outranks everything because it is the ask with consequences. A question
#: outranks the incident words, so "is checkout crashlooping?" is 👀 (a check),
#: while "checkout is down" is 🚨.
CHANGE_WORDS = re.compile(
    r"\bfix(es|ing)?\b"
    r"|\bbump\b"
    r"|\broll(ing)?[\s-]*back\b|\brollback\b"
    r"|\bopen\s+(a\s+|the\s+|an\s+)?(pr|pull\s+request)\b"
    r"|\bscale\b"
    r"|\bupgrade\b",
    re.IGNORECASE,
)
BOARD_WORDS = re.compile(
    r"\bboard\b|\bstatus\b|\bwhat'?s\s+running\b|\bwhat\s+is\s+running\b",
    re.IGNORECASE,
)
QUESTION_OPENERS = frozenset(
    {
        "is", "are", "was", "were", "why", "what", "whats", "what's", "how", "which",
        "who", "when", "where", "does", "do", "did", "can", "could", "should", "will",
        "would", "has", "have", "check", "show", "list", "tell", "explain", "describe",
    }
)
QUESTION_MARK = "?"
INCIDENT_WORDS = re.compile(
    r"\bdown\b|\bpag(e|ed|es|ing)\b|\boutage\b|\bsev\s?\d\b|\bsev\b|\bcrash\s?loop",
    re.IGNORECASE,
)
#: A Slack user or channel mention, stripped before the opener check so
#: "<@U123> is it down?" still reads as a question.
MENTION = re.compile(r"<[@#!][^>]*>")
FIRST_WORD = re.compile(r"[A-Za-z']+")

# --- layout ----------------------------------------------------------------

#: Slack's limits: button label, button value, buttons shown per actions block
#: before mobile wraps badly (Slack allows 25; Hermes uses 5).
BUTTON_TEXT_MAX = 75
BUTTON_VALUE_MAX = 2000
BUTTONS_PER_ROW = 5
#: Ours, not Slack's: the longest headline, and the mark a clip ends with.
HEADLINE_MAX = 150
ELLIPSIS = "…"

#: Action ids: ``<prefix>.link.<n>`` and ``<prefix>.choice.<n>``. Link buttons
#: open their url client-side and Slack still sends a block_actions request,
#: which the no-op handler acknowledges; a choice click is answered as the
#: clicker's reply (the gateway's ``slack_ux_clicks``).
LINK_ACTION = "link"
CHOICE_ACTION = "choice"
LINK_ACTION_ID_PATTERN = re.compile(r"\.link\.\d+$")
CHOICE_ACTION_ID_PATTERN = re.compile(r"\.choice\.\d+$")

CHOICES_LEAD = "Reply with one of: "
CHOICE_SEPARATOR = " · "
BULLET = "• "

#: A row's severity, as inline code ahead of its text: "`critical` text".
SEVERITY_TAG = "`{severity}` {text}"
BACKTICK = "`"

#: A report's group header: the shown rows counted per severity, in the order
#: first seen ("2 critical", "1 critical, 1 major"); rows with no severity are
#: counted as findings.
GROUP_COUNT = "{count} {severity}"
GROUP_SEPARATOR = ", "
UNTAGGED = "finding"
UNTAGGED_PLURAL = "findings"
#: Bounds on a report's fold: rows shown in it, and characters per row.
FOLD_ROWS_MAX = 50
ROW_TEXT_MAX = 300
PRIMARY_STYLE = "primary"
#: Slack's collapsible block, the report's fold, and its title when none is given.
FOLD_BLOCK = "container"
DEFAULT_FOLD_TITLE = "Why"

CODE_FENCE = "```"

#: A code fence line; nothing between an opener and its closer is a heading, a bullet or markup.
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
LIST_MARKER = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
#: Bold markers. An opener has no word character before it and a closer none after
#: it, so an in-word run such as ``DB__HOST`` or ``2**20`` is text.
BOLD_MARKERS = ("**", "__")
BOLD_OPEN = r"(?<!\w){0}(?=\S)"
BOLD_CLOSE = r"(?<=\S){0}(?!\w)"
#: As in CommonMark, ``**`` may also bold part of a word (``**Pod**s``, ``re**start**ed``),
#: but only a run without spaces when the opener is in-word, so ``2**20 and 2**30`` stays text.
#: ``__`` keeps the word-boundary rule.
STAR_BOLD = BOLD_OPEN.format(re.escape("**")) + r"(.+?)(?<=\S)\*\*"
STAR_BOLD_IN_WORD = r"(?<=\w)\*\*([^\s*]+)\*\*"
UNDERSCORE_BOLD = BOLD_OPEN.format(re.escape("__")) + "(.+?)" + BOLD_CLOSE.format(re.escape("__"))
MD_BOLD = re.compile(f"{STAR_BOLD}|{STAR_BOLD_IN_WORD}|{UNDERSCORE_BOLD}")
#: A markdown link; the url may hold balanced parentheses, as a Logs Explorer query does.
MD_LINK_URL = r"[^()\s]+(?:\([^()\s]*\)[^()\s]*)*"
MD_LINK = re.compile(rf"\[([^\[\]]+)\]\(({MD_LINK_URL})\)")
#: ``*italic*`` and ``_italic_``; a ``*`` inside a word (``2*3``) or unpaired (``*.tmp``) is text.
MD_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s.])([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=[^\s.])([^_\n]+?)(?<=\S)_(?![\w_])")
#: A code span: a backtick run, then content, then a run of the same length (CommonMark).
MD_CODE = re.compile(r"(?<!`)(`+)([^\n]+?)(?<!`)\1(?!`)")
#: Holds a code span's place while the other markup is stripped; input NULs are dropped first.
CODE_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")
MRKDWN_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))
#: A sentence ends at ``.``, ``!`` or ``?``, or just after the emphasis that closes on one,
#: then space, then anything but a lowercase letter ("in ns. prod" runs on).
SENTENCE_END = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][*_])|(?<=[.!?]\*\*)|(?<=[.!?]__))\s+(?=[^\sa-z])")
#: Per bold marker, its opener and closer, which a split can leave unpaired; the first
#: sentence closes one and the rest reopens it.
BOLD_EDGES = {
    marker: (re.compile(BOLD_OPEN.format(re.escape(marker))), re.compile(BOLD_CLOSE.format(re.escape(marker))))
    for marker in BOLD_MARKERS
}
#: A text ending in one of these abbreviations has not ended its sentence.
ABBREVIATION_END = re.compile(
    r"(?:^|[\s(\[])(?:e\.g|i\.e|vs|approx|incl|cf|etc|esp|fig|rev|ver|cont|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.$",
    re.IGNORECASE,
)
#: How far back from a candidate end an abbreviation can start; bounds the check to the tail.
ABBREVIATION_TAIL = 16
#: Slack refuses a button whose url is longer than this.
URL_MAX = 3000
#: A url a link button or ``<url|label>`` may carry: http(s) in any case, nothing that ends or
#: splits the link, and short enough for a button. Use ``fullmatch``: ``$`` would admit a trailing
#: newline.
SAFE_URL = re.compile(rf"https?://[^\s<>|]{{1,{URL_MAX - len('https://')}}}", re.IGNORECASE)
#: A space-aligned clip shorter than this share of the limit drops too much; it cuts hard instead.
CLIP_MIN_SHARE = 2
INLINE_CODE = re.compile(r"`[^`]*`")
#: :func:`to_mrkdwn` holds a code span's place with this; :data:`CODE_PLACEHOLDER` finds it again.
CODE_HOLD = "\x00{}\x00"
RICH_SPAN = re.compile(rf"`(?P<code>[^`]+)`|\*\*(?P<bold>.+?)\*\*|\[(?P<label>[^\[\]]+)\]\((?P<url>{MD_LINK_URL})\)")
#: A bare url in :func:`to_mrkdwn`'s input, held out of the bold pass like a code span. Its last
#: character is not markup or punctuation, so ``**https://x.io**`` still bolds the url.
BARE_URL = re.compile(r"https?://[^\s<>|\x00]*[^\s<>|\x00*_.,;:!?'\"]", re.IGNORECASE)


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on in this process's environment."""
    return os.environ.get(FLAG_ENV, "").strip().lower() in FLAG_ON_VALUES


# --- reactions -------------------------------------------------------------


def _is_question(text: str) -> bool:
    if text.rstrip().endswith(QUESTION_MARK):
        return True
    first = FIRST_WORD.search(text)
    return bool(first) and first.group(0).lower() in QUESTION_OPENERS


def arrival_reaction(text: str | None) -> str:
    """The emoji name for an ask, from its words alone (no model call)."""
    clean = MENTION.sub(" ", text or "").strip()
    if CHANGE_WORDS.search(clean):
        return REACTION_CHANGE
    if BOARD_WORDS.search(clean):
        return REACTION_BOARD
    if _is_question(clean):
        return REACTION_QUESTION
    if INCIDENT_WORDS.search(clean):
        return REACTION_INCIDENT
    return REACTION_QUESTION


def settle_reaction(outcome: str) -> str | None:
    """The emoji name for ``done``/``blocked``/``failed``; None for anything else."""
    return SETTLE_REACTIONS.get(outcome)


def settle_for_kanban_kind(kind: str) -> str | None:
    """``done``/``blocked``/``failed`` for a notifier event kind that settles work, else None."""
    return SETTLE_BY_KANBAN_KIND.get(kind)


# --- markdown --------------------------------------------------------------


def next_fence(line: str, fence: str | None) -> str | None:
    """The fence open after ``line``, given the one open before it.

    As in CommonMark, only a line of the opener's character, at least as long, closes it, an
    unclosed fence runs to the end, and a backtick run followed by another backtick is a code span.
    """
    match = FENCE.match(line)
    if not match:
        return fence
    mark = match.group(1)
    if fence is None:
        return None if mark[0] == "`" and "`" in line[match.end():] else mark
    closes = mark[0] == fence[0] and len(mark) >= len(fence) and not line.strip().strip(mark[0])
    return None if closes else fence


def _opens_fence(line: str) -> bool:
    return next_fence(line, None) is not None


def _paragraphs(markdown: str) -> list[str]:
    """Blank-line separated blocks, never splitting inside a fenced code block."""
    out: list[str] = []
    current: list[str] = []
    fence = None
    for line in markdown.split("\n"):
        fence = next_fence(line, fence)
        if fence is None and not line.strip():
            if current:
                out.append("\n".join(current).strip("\n"))
                current = []
            continue
        current.append(line)
    if current:
        out.append("\n".join(current).strip("\n"))
    return [p for p in out if p.strip()]


def _plain(markdown: str) -> str:
    """One line of markdown as plain text: no heading, list marker, emphasis or link syntax.

    Code spans are held out first, as a renderer resolves them first, so ``__init__`` in one stays.
    """
    text, spans = _hold_code(markdown.replace("\x00", "").strip())
    text = HEADING.sub("", text)
    text = LIST_MARKER.sub("", text)
    text = MD_LINK.sub(r"\1", text)
    text = MD_BOLD.sub(lambda m: next(g for g in m.groups() if g is not None), text)
    text = MD_ITALIC.sub(lambda m: m.group(1) or m.group(2), text)
    return CODE_PLACEHOLDER.sub(lambda m: spans[int(m.group(1))].group(2).strip(), text).strip()


def _hold_code(text: str) -> tuple[str, list[re.Match]]:
    """``text`` with each code span swapped for a placeholder, and the spans."""
    spans: list[re.Match] = []

    def hold(match: re.Match) -> str:
        spans.append(match)
        return f"\x00{len(spans) - 1}\x00"

    return MD_CODE.sub(hold, text), spans


def _restore_code(text: str, spans: list[re.Match]) -> str:
    """``text`` with each placeholder put back as its code span, backticks included."""
    return CODE_PLACEHOLDER.sub(lambda m: spans[int(m.group(1))].group(0), text)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    hard = text[: limit - len(ELLIPSIS)]
    cut = hard.rsplit(" ", 1)[0]
    # A long unbroken word would otherwise take everything after the last space with it.
    if len(cut) * CLIP_MIN_SHARE < len(hard):
        cut = hard
    return cut.rstrip() + ELLIPSIS


def _escape(text: str) -> str:
    for raw, escaped in MRKDWN_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def _first_sentence(line: str) -> tuple[str, str]:
    """``(first sentence, the rest)`` of ``line``, not cut after an abbreviation."""
    for match in SENTENCE_END.finditer(line):
        sentence = line[: match.start()]
        stem = sentence.rstrip("*_")
        if not ABBREVIATION_END.search(stem, max(0, len(stem) - ABBREVIATION_TAIL)):
            rest = line[match.end() :].strip()
            # "**One. Two.**" splits inside the bold, which would leave both halves unpaired.
            for marker, (opener, closer) in BOLD_EDGES.items():
                if sentence.count(marker) % 2 and opener.search(sentence) and closer.search(rest):
                    sentence, rest = sentence + marker, marker + rest
            return sentence, rest
    return line, ""


def split_answer(markdown: str) -> tuple[str, list[str]]:
    """``(headline, body_sections)`` for an agent's markdown answer.

    The headline is the first sentence of the first paragraph, as plain text
    capped at ``HEADLINE_MAX``. The rest of that paragraph and every later
    paragraph are the body sections, still markdown, in order. Empty input
    gives ``("", [])``.
    """
    paragraphs = _paragraphs((markdown or "").replace("\x00", ""))
    if not paragraphs:
        return "", []
    first, rest = paragraphs[0], paragraphs[1:]
    if _opens_fence(first.split("\n", 1)[0]):
        return "", paragraphs
    # A soft-wrapped sentence continues onto the next line; a list item, heading or fence does not,
    # and nothing continues a heading.
    lines = first.split("\n")
    wrapped = 1
    while wrapped < len(lines) and not HEADING.match(lines[0]) and not (
        LIST_MARKER.match(lines[wrapped]) or HEADING.match(lines[wrapped]) or _opens_fence(lines[wrapped])
    ):
        wrapped += 1
    joined = " ".join(line.strip() for line in lines[:wrapped])
    more_lines = "\n".join(lines[wrapped:])
    # The list marker goes first, or "1. Checkout is down." would end at "1.".
    # A period inside a code span does not end the sentence.
    held, spans = _hold_code(LIST_MARKER.sub("", joined))
    sentence, remainder = (_restore_code(part, spans) for part in _first_sentence(held))
    headline = _clip(_plain(sentence), HEADLINE_MAX)
    tail = "\n".join(part for part in (remainder, more_lines.strip("\n")) if part)
    body = ([tail] if tail.strip() else []) + rest
    return headline, body


# --- blocks ----------------------------------------------------------------


def _link_pairs(links: Iterable[Any]) -> list[tuple[str, str]]:
    """``(label, url)`` from tuples or ``{"text", "url"}`` mappings; entries without a ``SAFE_URL`` dropped."""
    pairs = []
    for link in links or ():
        if isinstance(link, Mapping):
            label, url = link.get("text") or link.get("label") or "", link.get("url") or ""
        else:
            label, url = link
        if url and SAFE_URL.fullmatch(str(url)):
            pairs.append((str(label or url), str(url)))
    return pairs


def _button(label: str, action_id: str, *, url: str | None = None, value: str | None = None) -> dict:
    button: dict = {
        "type": "button",
        "text": {"type": "plain_text", "text": _clip(label, BUTTON_TEXT_MAX), "emoji": True},
        "action_id": action_id,
    }
    if url is not None:
        button["url"] = url
    if value is not None:
        button["value"] = value[:BUTTON_VALUE_MAX]
    return button


def _actions(buttons: Sequence[dict]) -> list[dict]:
    return [
        {"type": "actions", "elements": list(buttons[i : i + BUTTONS_PER_ROW])}
        for i in range(0, len(buttons), BUTTONS_PER_ROW)
    ]


def to_mrkdwn(markdown: str) -> str:
    """Standard markdown to Slack mrkdwn: bold, links and headings; code and urls left alone.

    The input must already be escaped (``&``, ``<``, ``>``), as :func:`_row_line`
    does: this adds brackets but escapes nothing. Only a link to a ``SAFE_URL``
    is then written as ``<url|label>``; any other target, ``@U…``, ``!channel``,
    ``#C…`` or a url Slack would refuse, keeps its label alone.
    """
    spans: list[str] = []

    def keep(text: str) -> str:
        spans.append(text)
        return CODE_HOLD.format(len(spans) - 1)

    def hold(m: re.Match) -> str:
        return keep(m.group(0))

    def link(m: re.Match) -> str:
        return f"<{keep(m.group(2))}|{m.group(1)}>" if SAFE_URL.fullmatch(m.group(2)) else m.group(1)

    def restore(text: str) -> str:
        return CODE_PLACEHOLDER.sub(lambda m: restore(spans[int(m.group(1))]), text)

    lines = []
    in_fence = False
    for line in markdown.replace("\x00", "").split("\n"):
        if line.strip().startswith(CODE_FENCE):
            in_fence = not in_fence
            lines.append(line)
            continue
        if in_fence:
            lines.append(line)
            continue
        held = INLINE_CODE.sub(hold, line)
        heading = HEADING.match(held)
        if heading:
            held = "**" + held[heading.end():].strip() + "**"
        held = MD_LINK.sub(link, held)
        held = BARE_URL.sub(hold, held)
        held = MD_BOLD.sub(lambda m: "*" + next(g for g in m.groups() if g is not None) + "*", held)
        lines.append(restore(held))
    return "\n".join(lines)


def _row_parts(row: Any) -> tuple[str, str]:
    """``(severity, text)`` from a ``{"text", "severity"?}`` mapping or a plain string."""
    if isinstance(row, Mapping):
        text, severity = str(row.get("text") or ""), row.get("severity")
    else:
        text, severity = str(row), None
    return str(severity or "").replace(BACKTICK, "").strip().lower(), text


def _row_detail(row: Any) -> str:
    """A row's optional second line, from a mapping's ``detail``; empty for a plain string."""
    return str(row.get("detail") or "").strip() if isinstance(row, Mapping) else ""


def _shown(rows: Iterable[Any]) -> list[Any]:
    """``rows`` without those that have neither text nor a severity, which would render empty."""
    return [row for row in rows or () if any(part.strip() for part in _row_parts(row))]


def severity_row(severity: str, text: str) -> str:
    """``text`` led by ``severity`` as inline code; the same in markdown and mrkdwn."""
    return SEVERITY_TAG.format(severity=severity, text=text)


def _row_line(row: Any) -> str:
    """A row as one mrkdwn line, so a row can no more mention anyone than a label can.

    Escaping neutralises the brackets already there; ``to_mrkdwn`` then writes
    new ones only for a link to a ``SAFE_URL``.
    """
    severity, text = _row_parts(row)
    line = to_mrkdwn(_escape(_clip(text.strip(), ROW_TEXT_MAX)))
    line = severity_row(_escape(severity), line) if severity else f"{BULLET}{line}"
    detail = _row_detail(row)
    return f"{line}\n{to_mrkdwn(_escape(_clip(detail, ROW_TEXT_MAX)))}" if detail else line


def fallback_text(
    headline: str, links: Iterable[Any] = (), choices: Iterable[str] = (), rows: Sequence[Any] = ()
) -> str:
    """The same layout as plain mrkdwn, with no buttons.

    Used as the ``text`` of a blocks message (notifications, screen readers).
    ``headline`` is already plain, as :func:`split_answer` gives it; a second
    plain pass would strip markup a code span had kept. ``rows`` follow the
    headline, one line each, led by their severity as inline code or, with
    none, a bullet, and any ``detail`` on the line under its row, each clipped
    as the rich view clips it; links become inline ``<url|label>``; choices
    become one "Reply with one of:" line. Every label and row is escaped and every url,
    in a row or a link, is a ``SAFE_URL``, so none can mention anyone.
    """
    parts: list[str] = []
    title = _clip((headline or "").strip(), HEADLINE_MAX)
    if title:
        parts.append(f"*{_escape(title)}*")
    parts.extend(_row_line(row) for row in _shown(rows))
    pairs = _link_pairs(links)
    if pairs:
        parts.append(CHOICE_SEPARATOR.join(f"<{url}|{_escape(label)}>" for label, url in pairs))
    labels = [_escape(str(c)) for c in choices or () if str(c).strip()]
    if labels:
        parts.append(CHOICES_LEAD + CHOICE_SEPARATOR.join(labels))
    return "\n".join(parts)


def _rich_elements(markdown: str) -> list[dict]:
    """One line of markdown as rich_text elements: code spans, bold and links kept, the rest text."""
    elements: list[dict] = []
    at = 0
    for span in RICH_SPAN.finditer(markdown):
        if span.start() > at:
            elements.append({"type": "text", "text": markdown[at : span.start()]})
        if span.group("code") is not None:
            elements.append({"type": "text", "text": span.group("code"), "style": {"code": True}})
        elif span.group("bold") is not None:
            elements.append({"type": "text", "text": span.group("bold"), "style": {"bold": True}})
        elif SAFE_URL.fullmatch(span.group("url")):
            elements.append({"type": "link", "url": span.group("url"), "text": span.group("label")})
        else:
            elements.append({"type": "text", "text": span.group("label")})
        at = span.end()
    if at < len(markdown):
        elements.append({"type": "text", "text": markdown[at:]})
    return elements


def _rich_row(row: Any) -> dict:
    """A report row: its severity as inline code, then its text, with no bullet; any detail below it."""
    severity, text = _row_parts(row)
    elements = _rich_elements(_clip(text.strip(), ROW_TEXT_MAX))
    if severity:
        elements = [{"type": "text", "text": severity, "style": {"code": True}}, {"type": "text", "text": " "}, *elements]
    detail = _row_detail(row)
    if detail:
        elements += [{"type": "text", "text": "\n"}, *_rich_elements(_clip(detail, ROW_TEXT_MAX))]
    return {"type": "rich_text_section", "elements": elements}


def group_header(rows: Sequence[Any]) -> str:
    """The shown rows counted per severity, in the order first seen: "2 critical", "1 critical, 1 major"."""
    counts: dict[str, int] = {}
    for row in rows:
        severity, _ = _row_parts(row)
        counts[severity] = counts.get(severity, 0) + 1
    parts = []
    for severity, count in counts.items():
        if not severity:
            severity = UNTAGGED if count == 1 else UNTAGGED_PLURAL
        parts.append(GROUP_COUNT.format(count=count, severity=severity))
    return GROUP_SEPARATOR.join(parts)


def blocks_report(
    headline: str,
    note: str = "",
    rows: Sequence[Any] = (),
    choices: Iterable[str] = (),
    links: Iterable[Any] = (),
    fold_title: str | None = None,
    fold_rows: Sequence[Any] = (),
    action_id_prefix: str = "kage",
    fold_in_place: bool = True,
    fold_first: bool = False,
    detail: str = "",
) -> list[dict]:
    """Block Kit for a report: headline, the top rows between dividers, buttons, the fold.

    ``headline`` is bold and ``note`` follows it plain, both one line;
    ``detail``, when given, is one more plain line under them, clipped like a
    row. ``rows``
    and ``fold_rows`` are ``{"text": <markdown>, "severity"?: str, "detail"?:
    <markdown>}`` mappings or plain strings, a ``detail`` being a second line
    under its row, clipped like it; ``rows`` sit between two dividers under their
    :func:`group_header`. The first choice is the primary button and the links
    follow the choices. A choice is clipped to the button label before it
    becomes the value too, so a click posts only what the button showed. ``fold_rows`` go in a collapsed container titled
    ``fold_title``, or nowhere when ``fold_in_place`` is False, for the caller
    to post in the thread; ``fold_first`` puts it above the buttons, for a report
    whose buttons answer what the fold explains. Any part left empty, a row
    with no text and no severity included, is omitted.
    """
    rows, fold_rows = _shown(rows), _shown(fold_rows)
    blocks: list[dict] = []
    head: list[dict] = []
    title = _clip(_plain(headline or ""), HEADLINE_MAX)
    if title:
        head.append({"type": "text", "text": title, "style": {"bold": True}})
    if note and note.strip():
        head.append({"type": "text", "text": " " + _plain(note)})
    sections = [{"type": "rich_text_section", "elements": head}] if head else []
    detail_line = _clip(_plain(detail or "").strip(), ROW_TEXT_MAX)
    if detail_line:
        sections.append({"type": "rich_text_section", "elements": [{"type": "text", "text": detail_line}]})
    if sections:
        blocks.append({"type": "rich_text", "elements": sections})
    if rows:
        header = {"type": "rich_text_section", "elements": [{"type": "text", "text": group_header(rows), "style": {"bold": True}}]}
        blocks.append({"type": "divider"})
        blocks.append({"type": "rich_text", "elements": [header, *(_rich_row(row) for row in rows)]})
        blocks.append({"type": "divider"})
    choice_buttons = [
        _button(label, f"{action_id_prefix}.{CHOICE_ACTION}.{i}", value=label)
        for i, label in enumerate(_clip(str(c), BUTTON_TEXT_MAX) for c in choices or () if str(c).strip())
    ]
    if choice_buttons:
        choice_buttons[0]["style"] = PRIMARY_STYLE
    link_buttons = [
        _button(label, f"{action_id_prefix}.{LINK_ACTION}.{i}", url=url)
        for i, (label, url) in enumerate(_link_pairs(links))
    ]
    fold: list[dict] = []
    if fold_in_place and fold_rows:
        fold.append({
            "type": FOLD_BLOCK,
            "title": {"type": "plain_text", "text": _clip(_plain(fold_title or "").strip() or DEFAULT_FOLD_TITLE, BUTTON_TEXT_MAX)},
            "is_collapsible": True,
            "default_collapsed": True,
            "child_blocks": [
                {"type": "rich_text", "elements": [_rich_row(row) for row in fold_rows[:FOLD_ROWS_MAX]]}
            ],
        })
    actions = _actions(choice_buttons + link_buttons)
    blocks.extend(fold + actions if fold_first else actions + fold)
    return blocks


def has_fold(blocks: Sequence[dict]) -> bool:
    """Whether ``blocks`` fold in place, so leaving the fold out is worth a retry."""
    return any(block.get("type") == FOLD_BLOCK for block in blocks)


# --- link-button ack -------------------------------------------------------


async def ack_link_click(ack: Any, body: Any = None, action: Any = None) -> None:
    """Acknowledge a link-button click and do nothing else; Slack already opened the url."""
    await ack()
