"""Reshape the first inventory report for chat behind ``KAGE_SLACK_UX``.

``bootstrap_delivery.py`` prints ``INVENTORY.md`` as one cron message, which
reaches Slack as text: the only Block Kit it carries is what the adapter's
markdown renderer draws, so there are no buttons and no collapsible container.
``present`` gives a shorter text layout instead: the first posture sentence
as a bold headline (a bold title line above it is dropped, as a heading
is), the top two findings and every finding labelled critical a row each,
led by its severity as inline code when the report labels a finding's
severity, and the remaining findings listed under a "more worth a look" line.
Each finding keeps its headline and, on the line under it, the sentence the
report wrote there, unchanged. The roll-up and closing lines are kept as written.

``blocks`` is the same report as Block Kit, which ``bootstrap_delivery``
posts itself when it can: the top rows as
:func:`slack_presenter.blocks_report`'s group, the rest folded (each row with
its sentence as a second line), a primary
"start with" button naming the top finding and, when the report counts more
findings than the rows shown, a "show all N" button, both answered as the
clicker's turn. N is the larger of the posture's own "<n> findings" and the
listed items plus the roll-up's count ("Also found: <n>" or "<n> more"). The
roll-up stays as a plain line under the headline, from its counting sentence
to the end of that line, since a click
takes both buttons off the message and the line is then what still says how
many more there are and how to ask for them; the closing lines are left out,
even one written on the line below it.

The report is model-written to the format in
``agents/platform/governance/inventory_prioritize_sop.md`` (Step 6). A report
that does not parse to that shape is returned unchanged.
"""

import re

from slack_presenter import BUTTON_TEXT_MAX, _clip, _plain, blocks_report, fallback_text, severity_row

TOP_COUNT = 2
TOP_COUNT_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}
LEAD_TEMPLATE = "{count} worth fixing first:"
#: The lead when no top row is labelled critical or major, so a quiet cluster
#: reads like one (SOP Step 5).
NEUTRAL_LEAD_TEMPLATE = "{count} to look at first:"
REST_TEMPLATE = "**{count} more worth a look:**"
REST_BULLET = "- "
BOLD_MARK = "**"
PARAGRAPH_BREAK = "\n\n"
#: Severities that earn the "worth fixing" lead; criticals are never rolled up.
CRITICAL = "critical"
URGENT_SEVERITIES = frozenset({CRITICAL, "major"})
#: A bold span ending in one of these is a whole headline; the text after it
#: on the line is the sentence.
SENTENCE_PUNCTUATION = ".!?"

HEADING = re.compile(r"^#{1,6}\s")
ITEM_START = re.compile(r"^\d+[.)]\s+(.*)$")
#: The shape check for an item line: it opens with a bold span.
BOLD_LEAD = re.compile(r"^\*\*(.+?)\*\*\s*(.*)$")
#: A numbered bold item inside a paragraph: the list did not parse as one.
INLINE_ITEM = re.compile(r"\d+[.)]\s+\*\*")
#: A sentence end, but not the dot of "e.g.", "i.e." or "vs.".
SENTENCE_END = re.compile(r"(?<!\b[a-z]\.[a-z]\.)(?<!\bvs\.)(?<=[.!?])\s+")

#: A leading "Critical:" label, bold or plain.
SEVERITY_LABEL = re.compile(r"^(critical|major|minor)\s*:\s*", re.IGNORECASE)
#: "[critical]" or "(critical)" anywhere in the headline.
SEVERITY_TAG = re.compile(r"\s*[\[(](critical|major|minor)[\])]\s*", re.IGNORECASE)

#: Indents a rest finding's sentence under its bullet, so it stays in the item.
REST_INDENT = "  "

#: The Block Kit report.
ACTION_ID_PREFIX = "kage_inventory"
START_WITH = "start with: {finding}"
SHOW_ALL = "show all {count}"
FOLD_TITLE = "{count} more worth a look"
#: The total: the larger of the posture's own "<n> findings" and the listed
#: items plus the roll-up's count.
POSTURE_TOTAL = re.compile(r"\b(\d+)\s+findings\b", re.IGNORECASE)
#: The roll-up's count: "Also found: <n> items" (the SOP's wording) or "<n> more".
ROLLUP_COUNT = re.compile(r"\balso found\s*:?\s*(\d+)\b|\b(\d+)\s+more\b", re.IGNORECASE)
#: A paragraph that is one bold span: a title written without a "#".
BOLD_PARAGRAPH = re.compile(r"^\*\*([^*]+)\*\*$")


def _paragraphs(lines: list[str]) -> list[str]:
    """Blank-line-separated paragraphs, each joined onto one line."""
    out: list[str] = []
    current: list[str] = []
    for line in lines + [""]:
        if line.strip():
            current.append(line.strip())
        elif current:
            out.append(" ".join(current))
            current = []
    return out


def _rollup_count(text: str) -> int | None:
    """The roll-up's count in ``text``, or None when it has none."""
    match = ROLLUP_COUNT.search(text)
    if not match:
        return None
    return int(next(group for group in match.groups() if group is not None))


def _is_title(paragraph: str) -> bool:
    """True for a paragraph that is entirely bold with no sentence end: a title, not posture."""
    bold = BOLD_PARAGRAPH.match(paragraph)
    return bool(bold) and bold.group(1).strip()[-1:] not in SENTENCE_PUNCTUATION


Item = tuple[str | None, str, str]


def _item(first: str, more: list[str]) -> Item | None:
    """``(severity, headline, sentence)`` from an item's lines, or None if it has no bold lead.

    ``more`` is the item's lines under its first, joined into the sentence.

    The headline is the whole line, so a bold span covering only part of it
    (``**seeded-c:** the default SA is cluster-admin``) keeps the rest. Two
    shapes take less: a bold span ending a sentence is the headline and what
    follows it is the item's sentence, and a bold span that is only a severity
    label (``**Critical:**``) is the severity.
    """
    bold = BOLD_LEAD.match(first)
    if not bold:
        return None
    span, after = bold.group(1).strip(), bold.group(2).strip()
    sentence = ""
    if span[-1:] in SENTENCE_PUNCTUATION and after:
        headline, sentence = span, after
    else:
        headline = first.replace(BOLD_MARK, "").strip()
    sentence = " ".join(part for part in [sentence, *more] if part)
    severity = None
    label = SEVERITY_LABEL.match(headline)
    if label:
        severity, headline = label.group(1).lower(), headline[label.end() :]
    else:
        tag = SEVERITY_TAG.search(headline)
        if tag:
            severity = tag.group(1).lower()
            headline = SEVERITY_TAG.sub(" ", headline, count=1).strip()
    if not headline:
        return None
    return severity, headline, sentence


def _parse(report: str) -> tuple[str, list[Item], list[str], str] | None:
    """Posture, (severity, headline, sentence) items, trailing paragraphs and the roll-up, or None."""
    lines = report.splitlines()
    starts = [i for i, line in enumerate(lines) if ITEM_START.match(line)]
    if not starts:
        return None
    posture = _paragraphs([line for line in lines[: starts[0]] if not HEADING.match(line)])
    posture = [paragraph for paragraph in posture if not _is_title(paragraph)]
    if not posture:
        return None

    items: list[tuple[str, list[str]]] = []
    index = starts[0]
    while index < len(lines):
        line = lines[index]
        match = ITEM_START.match(line)
        if match:
            items.append((match.group(1).strip(), []))
        elif line.strip() and not line[:1].isspace() and not ITEM_START.match(lines[index - 1]):
            # An unindented line ends the list, unless it directly follows an
            # item line: CommonMark reads that as the item's lazy continuation.
            break
        elif line.strip():
            items[-1][1].append(line.strip())
        index += 1

    parsed = [_item(first, more) for first, more in items]
    if None in parsed:
        return None

    tail = _paragraphs(lines[index:])
    if any(HEADING.match(p) or ITEM_START.match(p) or INLINE_ITEM.search(p) for p in tail):
        return None
    return " ".join(posture), parsed, tail, _rollup(lines[index:])


def _unwrapped(lines: list[str]) -> list[str]:
    """``lines`` stripped, each line with no sentence end joined to a lower-case next line."""
    out: list[str] = []
    for line in (line.strip() for line in lines):
        if out and out[-1] and out[-1][-1:] not in SENTENCE_PUNCTUATION and line[:1].islower():
            out[-1] += " " + line
        else:
            out.append(line)
    return out


def _rollup(lines: list[str]) -> str:
    """The first line carrying a roll-up count, from that sentence to the line's end.

    Read by line, not by joined paragraph, so a closing line written directly
    under it stays out even when the roll-up has no full stop. A hard-wrapped
    sentence is one line: a line with no sentence end runs on into a next line
    that opens in lower case.
    """
    for line in _unwrapped(lines):
        sentences = SENTENCE_END.split(line.strip())
        for i, sentence in enumerate(sentences):
            if _rollup_count(sentence) is not None:
                return " ".join(sentences[i:])
    return ""


def _row(severity: str | None, headline: str, bold: bool = True) -> str:
    """A finding's headline line, led by its severity as inline code when the headline was labelled."""
    if severity:
        return severity_row(severity, headline)
    return f"{BOLD_MARK}{headline}{BOLD_MARK}" if bold else headline


def _with_sentence(row: str, sentence: str, indent: str = "") -> str:
    """``row`` with its finding's sentence on the line under it."""
    return f"{row}\n{indent}{sentence}" if sentence else row


class _Shape:
    """A parsed report split into its headline, top rows and the rest."""

    def __init__(self, posture: str, items: list[Item], tail: list[str], rollup: str):
        sentences = SENTENCE_END.split(posture.replace(BOLD_MARK, ""), maxsplit=1)
        self.headline = sentences[0]
        self.note = sentences[1] if len(sentences) > 1 else ""
        self.top = [item for i, item in enumerate(items) if i < TOP_COUNT or item[0] == CRITICAL]
        self.rest = [item for i, item in enumerate(items) if not (i < TOP_COUNT or item[0] == CRITICAL)]
        self.lead = ""
        if self.rest:
            urgent = any(item[0] in URGENT_SEVERITIES for item in self.top)
            template = LEAD_TEMPLATE if urgent else NEUTRAL_LEAD_TEMPLATE
            self.lead = template.format(count=TOP_COUNT_WORDS.get(len(self.top), str(len(self.top))))
        self.tail = tail
        self.rollup = rollup
        stated = POSTURE_TOTAL.search(posture)
        more = next((n for n in (_rollup_count(p) for p in tail) if n is not None), None)
        counts = [int(stated.group(1))] if stated else []
        if more is not None:
            counts.append(len(items) + more)
        # The larger, so a posture counting only what "needs attention now"
        # does not hide the roll-up's findings from "show all".
        self.total: int | None = max(counts) if counts else None


def _shape(report: str) -> _Shape | None:
    parsed = _parse(report)
    return None if parsed is None else _Shape(*parsed)


def present(report: str) -> str:
    """The report as headline, top findings and the rest; unchanged if it does not parse."""
    shape = _shape(report)
    if shape is None:
        return report
    headline = f"{BOLD_MARK}{shape.headline}{BOLD_MARK}"
    if shape.note:
        headline += f" {shape.note}"
    rows = [shape.lead] if shape.lead else []
    rows += [_with_sentence(_row(severity, h), sentence) for severity, h, sentence in shape.top]
    blocks = [headline, "\n".join(rows)]
    if shape.rest:
        blocks.append(_rest_text(shape))
    return PARAGRAPH_BREAK.join(blocks + shape.tail) + "\n"


def _rest_text(shape: _Shape) -> str:
    rest_rows = [
        REST_BULLET + _with_sentence(_row(severity, h, bold=False), sentence, REST_INDENT)
        for severity, h, sentence in shape.rest
    ]
    return "\n".join([REST_TEMPLATE.format(count=len(shape.rest))] + rest_rows)


def present_rest(report: str) -> str:
    """The "more worth a look" findings as :func:`present` lays them out; empty when there are none."""
    shape = _shape(report)
    return _rest_text(shape) + "\n" if shape is not None and shape.rest else ""


def _rows(items: list[Item]) -> list[dict]:
    return [{"severity": severity, "text": headline, "detail": sentence} for severity, headline, sentence in items]


def blocks(report: str, fold_in_place: bool = True) -> tuple[list[dict], str, str] | None:
    """The Block Kit report's ``(blocks, text, rest)``, or None when the report does not parse.

    ``text`` is the message's mrkdwn ``text`` field. ``rest`` is the folded
    findings as mrkdwn, empty when there are none or they are folded in
    place; with ``fold_in_place`` False the caller posts it in the thread.
    """
    shape = _shape(report)
    if shape is None:
        return None
    note = " ".join(part for part in (shape.note, shape.lead) if part)
    top = _rows(shape.top)
    choices = [START_WITH.format(finding=_clip(_plain(shape.top[0][1]), BUTTON_TEXT_MAX))] if shape.top else []
    # Offered whenever the total is more than the rows shown, folded rows or none.
    if shape.total and shape.total > len(shape.top):
        choices.append(SHOW_ALL.format(count=shape.total))
    fold_title = FOLD_TITLE.format(count=len(shape.rest))
    built = blocks_report(
        shape.headline,
        note=note,
        rows=top,
        choices=choices,
        fold_title=fold_title,
        fold_rows=_rows(shape.rest),
        action_id_prefix=ACTION_ID_PREFIX,
        fold_in_place=fold_in_place,
        fold_first=True,
        detail=_plain(shape.rollup),
    )
    rest = fallback_text(fold_title, rows=_rows(shape.rest)) if shape.rest and not fold_in_place else ""
    return built, fallback_text(shape.headline, rows=top), rest
