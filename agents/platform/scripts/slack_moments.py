"""Slack layout for the two moments that get a message of their own.

Pure functions only, like ``slack_presenter``, whose layout this reuses.
``gateway/slack_ux_moments.py`` decides when to post them.

* A pull request the work opened (:func:`pr_opened`): a bold headline naming
  the PR, the worker's own line below it as evidence, and "Open PR" and
  "Files changed" url buttons. :func:`opened_pr` finds it in a progress note or a report, and
  only where the line says it opened that PR, the verb in front of the url
  with no one else as its subject, so a note citing someone else's PR
  ("… pull/300, opened by bob", "Dependabot opened …", "dependabot[bot]
  opened …", "Kelly opened …") is not announced as ours.
* A question the work is waiting on (:func:`needs_you`): the worker's question
  in bold (plain, when bold would cost it a ``*`` or a ``__name__``), the rest
  of its reason below it, a choice button for each option
  the reason ends with, and a "waiting on you" line. A click is the clicker's
  answer in the thread (``gateway/slack_ux_clicks.py``), the same path as
  typing it. The options are buttons only when there are two to five, each
  fits on a button, they end the reason, and the line right before them is a
  question; otherwise, and with no thread for a click to answer in, the
  question is text only and a typed reply is the answer.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import slack_presenter as _presenter

#: A pull request url: host, owner, repo and number, with any trailing path
#: (``/files``, ``#discussion``) left out of the match.
PR_URL = re.compile(r"https?://[^\s/<>|]+/([^\s/<>|]+)/([^\s/<>|]+)/pull/(\d+)")
#: What must sit right before the url for a line to announce the PR as ours:
#: the verb, then optionally "a"/"the", "new", "PR"/"pull request" with its
#: number, a colon, dash or "(", and the opening of a ``[label](`` or ``<`` link.
OPENED_BEFORE_URL = re.compile(
    r"\b(?:opened|created|raised|filed|submitted)\s+(?:(?:an?|the)\s+)?(?:new\s+)?"
    r"(?:(?:PR|pull\s+request)(?:\s*#\d+)?\s*[:(—–-]?\s*)?(?:\[[^\]]*\]\(|<)?$",
    re.IGNORECASE,
)
#: A negation just before the verb: "not opened", "haven't yet opened".
NEGATED_VERB = re.compile(r"(?:\bnot|\bnever|n't)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)
#: Adverbs that can sit before the verb; a list, since "-ly" alone would
#: take "Kelly opened" as ours.
ADVERB = (
    r"(?:successfully|finally|quickly|eventually|subsequently|immediately|promptly|"
    r"manually|automatically|separately|additionally|accordingly|initially|lastly)"
)
#: Who opened it: nothing (or punctuation, a bullet, an emoji) before the verb,
#: "I"/"we", or "and"/"then" joining it to the worker's earlier steps, with at
#: most three of "have", "just", an adverb and the like between;
#: "Dependabot opened <url>" is someone else's PR, and so is a bracketed
#: login, "dependabot[bot] opened <url>", whose "]" is not punctuation here.
OUR_SUBJECT = re.compile(
    r"(?:^|[^\w\s'’\]]|\b(?:I|we)\b|\b(?P<joined>and|then)\b)\s*"
    r"(?:(?:have|[’']ve|also|just|then|now|already|" + ADVERB + r")\s+){0,3}$",
    re.IGNORECASE,
)
#: Where the clause before a joining "and"/"then" starts: after the last of these.
CLAUSE_BREAK = re.compile(r"[.!?;:]")
#: A clause's words, a mention's "@" and a hyphenated verb's "-" kept.
CLAUSE_WORD = re.compile(r"[\w@'’-]+")
#: Words skipped at the start of that clause: "Just fixed and opened".
CLAUSE_FILLER = re.compile(r"also|just|now|already|then|" + ADVERB, re.IGNORECASE)
#: The clause is the worker's own step when it starts with "I"/"we" or a
#: past-tense verb, regular or one of these.
OUR_CLAUSE_LEAD = re.compile(r"I|we|we[’']ve|I[’']ve", re.IGNORECASE)
PAST_VERB = re.compile(r"[\w-]+ed", re.IGNORECASE)
IRREGULAR_PAST = frozenset({
    "ran", "re-ran", "reran", "made", "wrote", "rewrote", "built", "rebuilt", "found", "took",
    "did", "put", "set", "got", "sent", "split", "kept", "began", "brought", "gave",
})
#: After a leading "-ed" word, one of these makes that word a name doing the
#: step, "Fred reviewed and opened", not the worker's verb.
NAME_THEN_VERB = re.compile(r"has|had|was|[\w-]+ed", re.IGNORECASE)
#: The worker's line under the headline, clipped: a note can be one long
#: paragraph, and a Slack context element holds at most 3,000 characters.
EVIDENCE_MAX = 300
#: Markup a button cannot show, stripped from an option with its pair only, so
#: a glob (``app=web-*``) or a dunder name keeps its characters.
OPTION_MARKUP = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*")
#: The same for the question's headline, plus a paired ``*emphasis*``.
HEADLINE_MARKUP = re.compile(
    r"`([^`]+)`|\*\*([^*]+)\*\*|(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])"
)

PR_HEADLINE = "I opened PR #{number} in {repo}. It's yours to review."
PR_REF = "PR #{number}"
#: The url in the worker's line, with a "PR" or "PR #412:" already in front of
#: it, so "Opened PR <url>" reads "Opened PR #412" rather than "Opened PR PR
#: #412". A Slack ``<url|label>``, markdown ``[label](url)`` or ``(url)``
#: around it goes too, and so does a tail after the number (``/files``).
PR_URL_TAIL = r"(?:[/?#][^\s<>|()\[\]]*)?"
PR_REF_URL = r"(?:<{url}{tail}(?:\|[^>]*)?>|\[[^\]]*\]\({url}{tail}\)|{url}{tail})"
PR_REF_SPAN = r"(?:\bPR\s+(?:#{number}\s*[:—–-]?\s*)?)?(?:\(\s*{ref}\s*\)|{ref})"
OPEN_PR = "Open PR ↗"
FILES_CHANGED = "Files changed ↗"
FILES_PATH = "/files"
PR_ACTION_PREFIX = "kage_pr"

#: An option line in a block reason: a bullet or a numbered item.
OPTION_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$")
#: How the line right before the options must end for them to be choices; a
#: list after "I found:" is evidence, not answers.
QUESTION_END = "?"
#: Buttons only for two to five options that end the reason, right after a
#: question, each short enough that Slack shows all of it; otherwise the
#: options stay in the text.
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
            verb = OPENED_BEFORE_URL.search(line[: match.start()])
            before = line[: verb.start()] if verb else ""
            subject = OUR_SUBJECT.search(before) if verb else None
            if not subject or NEGATED_VERB.search(before):
                continue
            joined = subject.group("joined")
            if joined and not _our_step(before[: subject.start("joined")]):
                continue
            return match.group(0), match.group(2), match.group(3), line.strip()
    return None


def _our_step(before_join: str) -> bool:
    """Whether the clause before a joining "and"/"then" is the worker's own step.

    "Fixed and opened" and "I reviewed and opened" are; "Dependabot then
    opened", "bob reviewed and opened" and "Kube Agents Robot then opened"
    are not, since a clause that does not start with "I"/"we" or a past-tense
    verb names someone else.
    """
    words = CLAUSE_WORD.findall(CLAUSE_BREAK.split(before_join)[-1])
    while words and CLAUSE_FILLER.fullmatch(words[0]):
        words.pop(0)
    if not words or OUR_CLAUSE_LEAD.fullmatch(words[0]):
        return True
    if not (PAST_VERB.fullmatch(words[0]) or words[0].lower() in IRREGULAR_PAST):
        return False
    return not (len(words) > 1 and NAME_THEN_VERB.fullmatch(words[1]))


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
    ref = PR_REF_URL.format(url=re.escape(url), tail=PR_URL_TAIL)
    span = re.compile(PR_REF_SPAN.format(number=number, ref=ref), re.IGNORECASE)
    evidence = _presenter._clip(span.sub(PR_REF.format(number=number), line).strip(), EVIDENCE_MAX)
    links = [(OPEN_PR, url), (FILES_CHANGED, url + FILES_PATH)]
    blocks = _presenter.blocks_answer(headline, links=links, action_id_prefix=PR_ACTION_PREFIX)
    first, *rest = _presenter.fallback_text(headline, links=links).split("\n")
    return _with_subline(blocks, evidence), "\n".join([_text(first, evidence), *rest])


def _unmarked(option: str) -> str:
    return OPTION_MARKUP.sub(lambda m: m.group(1) or m.group(2), option).strip()


def _headline_text(headline: str) -> str:
    """``headline`` as plain text, stripped of paired markup only, so a glob
    (``app=web-*``) or a dunder name keeps its characters; the presenter's
    ``_plain`` drops every ``*`` and collapses ``__name__``."""
    text = _presenter.HEADING.sub("", headline.strip())
    text = _presenter.LIST_MARKER.sub("", text)
    text = _presenter.MD_LINK.sub(r"\1", text)
    return HEADLINE_MARKUP.sub(lambda m: m.group(1) or m.group(2) or m.group(3), text).strip()


def _trailing_options(lines: Sequence[str]) -> tuple[int, list[str]]:
    """Where the option lines that end ``lines`` start, and their text, when a question
    comes right before them; ``(len(lines), [])`` otherwise."""
    start = len(lines)
    while start > 1 and (not lines[start - 1].strip() or OPTION_LINE.match(lines[start - 1])):
        start -= 1
    # A button is plain text: `code` and **bold** would show their markup.
    options = [_unmarked(m.group(1)) for m in (OPTION_LINE.match(line) for line in lines[start:]) if m]
    if not options or not lines[start - 1].rstrip().endswith(QUESTION_END):
        return len(lines), []
    return start, options


def _question(reason: str, buttons: bool) -> tuple[str, list[str], list[str]]:
    """The reason's first line, the lines after it, and its options when they can be buttons."""
    lines = str(reason or "").strip().splitlines()
    # A line of markup alone (a bare "```") has no text to head the question.
    while lines and not _presenter._plain(lines[0]):
        lines.pop(0)
    if not lines:
        return "", [], []
    start, options = _trailing_options(lines)
    usable = buttons and OPTIONS_MIN <= len(options) <= OPTIONS_MAX and all(
        len(option) <= _presenter.BUTTON_TEXT_MAX for option in options
    )
    if not usable:
        start, options = len(lines), []
    first = lines[0].strip()
    # A clipped headline keeps its whole line below it too.
    below = 0 if len(first) > _presenter.HEADLINE_MAX else 1
    return first, lines[below:start], options


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
    title = _headline_text(headline)
    if title != _presenter._plain(headline):
        # Slack mrkdwn has no escape for "*", so a headline the presenter would
        # change goes out as plain text, unbolded but with its characters.
        title = _presenter._clip(title, _presenter.HEADLINE_MAX)
        blocks[0] = {"type": "section", "text": {"type": "plain_text", "text": title, "emoji": True}}
        first = _escape(title)
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
