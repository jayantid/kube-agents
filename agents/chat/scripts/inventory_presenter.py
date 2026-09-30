"""Reshape the first inventory report for chat behind ``KAGE_SLACK_UX``.

``bootstrap_delivery.py`` prints ``INVENTORY.md`` as one cron message, which
reaches Slack as text: the only Block Kit it carries is what the adapter's
markdown renderer draws, so there are no buttons and no collapsible container.
``present`` gives mock 15's text fallback instead: the first posture sentence
as a bold headline, the top two findings one row each, and the remaining
findings listed under a "more worth a look" line. The roll-up and closing lines
are kept as written.

The report is model-written to the format in
``agents/platform/governance/inventory_prioritize_sop.md`` (Step 6). A report
that does not parse to that shape is returned unchanged.
"""

import re

TOP_COUNT = 2
TOP_COUNT_WORDS = {1: "One", 2: "Two"}
LEAD_TEMPLATE = "{count} worth fixing first:"
REST_TEMPLATE = "**{count} more worth a look:**"
BOLD_MARK = "**"
PARAGRAPH_BREAK = "\n\n"

HEADING = re.compile(r"^#{1,6}\s")
ITEM_START = re.compile(r"^\d+[.)]\s+(.*)$")
BOLD_LEAD = re.compile(r"^\*\*(.+?)\*\*\s*(.*)$")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

SEVERITY_WORD = re.compile(r"\b(critical|major|minor)\b", re.IGNORECASE)
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


def _parse(report: str) -> tuple[str, list[tuple[str, str]], list[str]] | None:
    """Posture, (headline, sentence) items and trailing paragraphs, or None."""
    lines = report.splitlines()
    starts = [i for i, line in enumerate(lines) if ITEM_START.match(line)]
    if not starts:
        return None
    posture = _paragraphs([line for line in lines[: starts[0]] if not HEADING.match(line)])
    if not posture:
        return None

    items: list[list[str]] = []
    index = starts[0]
    while index < len(lines):
        line = lines[index]
        match = ITEM_START.match(line)
        if match:
            items.append([match.group(1).strip()])
        elif line.strip() and not line[:1].isspace():
            break  # an unindented line that is not an item ends the list
        elif line.strip():
            items[-1].append(line.strip())
        index += 1

    parsed = []
    for item in items:
        match = BOLD_LEAD.match(item[0])
        if not match:
            return None
        parsed.append((match.group(1).strip(), " ".join([match.group(2)] + item[1:]).strip()))

    tail = _paragraphs(lines[index:])
    if any(HEADING.match(p) or ITEM_START.match(p) for p in tail):
        return None
    return " ".join(posture), parsed, tail


def _row(headline: str) -> str:
    """One finding on one line, led by its severity when the headline names one.

    Only the headline is searched: the sentence under it is prose, where
    "not critical" would read as a label.
    """
    severity = SEVERITY_WORD.search(headline)
    if not severity:
        return f"{BOLD_MARK}{headline}{BOLD_MARK}"
    word = severity.group(1).lower()
    text = SEVERITY_TAG.sub(" ", headline).strip()
    return f"{SEVERITY_MARKERS[word]} {BOLD_MARK}{word}{BOLD_MARK}  {text}"


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

    top, rest = items[:TOP_COUNT], items[TOP_COUNT:]
    rows = [LEAD_TEMPLATE.format(count=TOP_COUNT_WORDS[len(top)])] if rest else []
    rows += [_row(h) for h, _ in top]
    blocks = [headline, "\n".join(rows)]
    if rest:
        blocks.append("\n".join([REST_TEMPLATE.format(count=len(rest))] + [f"- {h}" for h, _ in rest]))
    return PARAGRAPH_BREAK.join(blocks + tail) + "\n"
