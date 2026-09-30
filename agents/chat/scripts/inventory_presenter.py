"""Reshape the first inventory report for chat behind ``KAGE_SLACK_UX``.

``bootstrap_delivery.py`` prints ``INVENTORY.md`` as one cron message, which
reaches Slack as text: the only Block Kit it carries is what the adapter's
markdown renderer draws, so there are no buttons and no collapsible container.
``present`` gives mock 15's text fallback instead: the first posture sentence
as a bold headline, the top two findings and every critical one a row each,
and the remaining findings listed under a "more worth a look" line. The
roll-up and closing lines are kept as written.

The report is model-written to the format in
``agents/platform/governance/inventory_prioritize_sop.md`` (Step 6). A report
that does not parse to that shape is returned unchanged.
"""

import re

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
# Unicode rather than Slack shortcodes: the delivery target is the chat the user
# first spoke in, which can be Google Chat.
SEVERITY_MARKERS = {"critical": "\U0001f534", "major": "\U0001f7e0", "minor": "\U0001f7e1"}


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


def _item(first: str) -> tuple[str | None, str] | None:
    """``(severity, headline)`` from an item's first line, or None if it has no bold lead.

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
    if span[-1:] in SENTENCE_PUNCTUATION and after:
        headline = span
    else:
        headline = first.replace(BOLD_MARK, "").strip()
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
    return severity, headline


def _parse(report: str) -> tuple[str, list[tuple[str | None, str]], list[str]] | None:
    """Posture, (severity, headline) items and trailing paragraphs, or None."""
    lines = report.splitlines()
    starts = [i for i, line in enumerate(lines) if ITEM_START.match(line)]
    if not starts:
        return None
    posture = _paragraphs([line for line in lines[: starts[0]] if not HEADING.match(line)])
    if not posture:
        return None

    firsts: list[str] = []
    index = starts[0]
    while index < len(lines):
        line = lines[index]
        match = ITEM_START.match(line)
        if match:
            firsts.append(match.group(1).strip())
        elif line.strip() and not line[:1].isspace() and not ITEM_START.match(lines[index - 1]):
            # An unindented line ends the list, unless it directly follows an
            # item line: CommonMark reads that as the item's lazy continuation.
            break
        index += 1

    parsed = [_item(first) for first in firsts]
    if None in parsed:
        return None

    tail = _paragraphs(lines[index:])
    if any(HEADING.match(p) or ITEM_START.match(p) or INLINE_ITEM.search(p) for p in tail):
        return None
    return " ".join(posture), parsed, tail


def _row(severity: str | None, headline: str, bold: bool = True) -> str:
    """One finding on one line, led by its severity when the headline was labelled."""
    if severity:
        return f"{SEVERITY_MARKERS[severity]} {BOLD_MARK}{severity}{BOLD_MARK}  {headline}"
    return f"{BOLD_MARK}{headline}{BOLD_MARK}" if bold else headline


def present(report: str) -> str:
    """The report as headline, top findings and the rest; unchanged if it does not parse."""
    parsed = _parse(report)
    if parsed is None:
        return report
    posture, items, tail = parsed

    sentences = SENTENCE_END.split(posture.replace(BOLD_MARK, ""), maxsplit=1)
    headline = f"{BOLD_MARK}{sentences[0]}{BOLD_MARK}"
    if len(sentences) > 1:
        headline += f" {sentences[1]}"

    top = [item for i, item in enumerate(items) if i < TOP_COUNT or item[0] == CRITICAL]
    rest = [item for i, item in enumerate(items) if not (i < TOP_COUNT or item[0] == CRITICAL)]
    rows = []
    if rest:
        urgent = any(severity in URGENT_SEVERITIES for severity, _ in top)
        template = LEAD_TEMPLATE if urgent else NEUTRAL_LEAD_TEMPLATE
        rows.append(template.format(count=TOP_COUNT_WORDS.get(len(top), str(len(top)))))
    rows += [_row(severity, h) for severity, h in top]
    blocks = [headline, "\n".join(rows)]
    if rest:
        rest_rows = [REST_BULLET + _row(severity, h, bold=False) for severity, h in rest]
        blocks.append("\n".join([REST_TEMPLATE.format(count=len(rest))] + rest_rows))
    return PARAGRAPH_BREAK.join(blocks + tail) + "\n"
