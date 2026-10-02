"""Reshape the first inventory report for chat behind ``KAGE_SLACK_UX``.

``bootstrap_delivery.py`` prints ``INVENTORY.md`` as one cron message, which
reaches Slack as text: the only Block Kit it carries is what the adapter's
markdown renderer draws, so there are no buttons and no collapsible container.
``present`` gives a shorter text layout instead, with one number on it: the
total. Its headline is written here, not taken from the posture: "I scanned
3 clusters and 41 workloads and found 22 things to look at.", naming only
the counts the posture states in a sentence saying what was scanned, then a
lead ("Two are worth fixing first:") when the total is more than the rows
shown. The part of the posture naming what was not scanned ("2 clusters could
not be scanned (permission denied)") is kept, as written, on its own line under
the headline, since a silent gap reads as clean; a sentence it shares with the
scan counts is split at its clauses, so the counts stay in the headline. The top two findings and every
finding labelled critical follow, a row each, led by its severity as inline
code when the report labels a finding's severity. Each finding keeps its
headline and, on the line under it, the sentence the report wrote there,
unchanged. The total is the listed items plus the roll-up's count, the
roll-up being the first paragraph after the list that counts "<n> more" or
"Also found", else the first whose count reads as more: a count of a
severity ("2 high"), or "also", "plus", "other", "remaining",
"lower-priority". A bare "22 findings" is a
closing line's, not a roll-up's, unless a colon follows it ("4 issues in 2
namespaces: ..."). A count it states for the whole roll-up
wins ("18 more items: 2 high, 16 low" is 18, not 36): its "<n> more", the
"<n>" of "Also found: <n>" when no other noun follows it ("Also found: 2
clusters with ..." counts clusters), or a first term that heads the rest ("18
items: 2 high, 16 low"). Only with none are its terms ("2 high, 1 medium
and 1 low") summed. With no
roll-up, the total is a closing line's "all <n> findings", then the
posture's own "<n> findings", then the listed items. The rest of the
findings, the rest of the posture and the roll-up paragraph are left out;
every other closing line is kept as written, counts and all, since they are
how a text reader asks for the rest. When the roll-up was the last of them,
"Ask me to see all <n>." takes its place.

``blocks`` is the same card as Block Kit, which ``bootstrap_delivery``
posts itself when it can: the headline, lead and top rows with no count
above them, then a primary "Fix the first one" button and, when the total is
more than the rows shown, a "See all N" button, both answered as the
clicker's turn. The primary button's value names the first row as the card
shows it ("Fix the first one: <finding>"), so a click handler that sends the
value as the turn tells the agent which finding without its reading the
thread. The closing lines are left out too: "See all N" asks for the
rest.

The report is model-written to the format in
``agents/platform/governance/inventory_prioritize_sop.md`` (Step 6). A report
that does not parse to that shape is returned unchanged.
"""

import re

from slack_presenter import as_line, blocks_report, fallback_text, gap_parts, severity_row, shown_text

TOP_COUNT = 2
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

#: A leading "Critical:" label, bold or plain.
SEVERITY_LABEL = re.compile(r"^(critical|major|minor)\s*:\s*", re.IGNORECASE)
#: "[critical]" or "(critical)" anywhere in the headline.
SEVERITY_TAG = re.compile(r"\s*[\[(](critical|major|minor)[\])]\s*", re.IGNORECASE)

#: The Block Kit report.
ACTION_ID_PREFIX = "kage_inventory"
#: The card's headline, from the posture's own counts; a count it does not state is left out.
SCANNED_TEMPLATE = "I scanned {scanned} and found {found}"
FOUND_TEMPLATE = "I found {found}"
SCANNED_JOIN = " and "
FINDINGS_NOUN = ("thing to look at", "things to look at")
CLUSTERS_NOUN = ("cluster", "clusters")
WORKLOADS_NOUN = ("workload", "workloads")
#: The headline ends on a full stop before a lead, and on a colon straight into the rows.
LEADS_ON = "."
ROWS_FOLLOW = ":"
CARD_LEAD_TEMPLATE = "{count} are worth fixing first:"
CARD_LEAD_ONE = "One is worth fixing first:"
CARD_NEUTRAL_LEAD_TEMPLATE = "Start with these {count}:"
CARD_NEUTRAL_LEAD_ONE = "Start with this one:"
FIX_FIRST = "Fix the first one"
FIX_ONLY = "Fix it"
#: The primary button's value: its label naming the first row, as the click's turn.
FIX_TURN = "{label}: {finding}"
SEE_ALL = "See all {count}"
#: Counts written as words in a lead; past the last, the digits.
COUNT_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
#: Number words a posture may use for a count.
NUMBER_WORDS = {word: n for n, word in [(1, "one"), *COUNT_WORDS.items()]}
#: A whole number: not the tail of "1.30", "v2" or "2,500".
WHOLE = r"(?<![\w.,-])"
COUNT = WHOLE + r"(\d+|" + "|".join(NUMBER_WORDS) + r")\b"
#: "3 clusters", "three GKE clusters"; "41 workloads". Read only in a clause
#: that says what was scanned, and only when every such clause agrees.
CLUSTER_COUNT = re.compile(COUNT + r"\s+(?:GKE\s+)?clusters?\b", re.IGNORECASE)
WORKLOAD_COUNT = re.compile(COUNT + r"\s+workloads?\b", re.IGNORECASE)
#: A posture clause: split at a semicolon or a sentence end, not at "e.g. prod".
CLAUSE_END = re.compile(r"[;!?]\s+|\.\s+(?=[A-Z0-9])")
GAP_SEPARATOR = ", "
SCAN_VERB = re.compile(
    r"\b(?:scanned|scanning|checked|covered|covering|across|inventoried|reviewed|examined|looked at)\b",
    re.IGNORECASE,
)
#: The posture's own total, used only when no roll-up or closing line counts.
POSTURE_TOTAL = re.compile(WHOLE + r"(\d+)\s+findings\b", re.IGNORECASE)
#: "2 high-priority", "3 medium-severity": still one severity's count.
SEVERITY_SUFFIX = r"(?:-(?:priority|severity|risk))?"
#: A findings noun, after at most two words: "items", "lower-priority findings".
FINDINGS_WORD = r"(?:[a-z-]+\s+){0,2}?(?:findings?|items?|issues?|problems?)\b"
#: One count in a roll-up: "18 more", "2 high", "19 lower-priority findings";
#: never "all 22 findings", which is a total, not more.
ROLLUP_TERM = re.compile(
    r"(?<!all )" + WHOLE + r"(\d+)\s+(?:"
    r"(?:critical|high|major|medium|moderate|minor|low)" + SEVERITY_SUFFIX + r"(?![\w-])"
    r"|" + FINDINGS_WORD + r"|more\b)",
    re.IGNORECASE,
)
#: A closing line's total: "See all 22 findings".
ALL_TOTAL = re.compile(r"\ball\s+" + WHOLE + r"(\d+)\s+findings\b", re.IGNORECASE)
#: A count the roll-up states for all of itself: "18 more".
MORE_TOTAL = re.compile(WHOLE + r"(\d+)\s+more\b", re.IGNORECASE)
#: What a roll-up may count before its findings: "2 clusters with 9 findings".
SCOPE_NOUN = r"(?:clusters?|namespaces?|nodes?|workloads?|projects?)\b"
#: The SOP's roll-up wording: "Also found: 18", "Also found: 18 items"; not
#: "Also found: 2 high, ...", whose 2 is one term of a breakdown, nor "Also
#: found: 2 clusters with 9 findings", whose 2 counts clusters. A later count
#: that is not of findings ("19 workloads across 3 clusters") leaves it standing.
ALSO_FOUND = re.compile(
    r"\balso found\s*:?\s*(\d+)\b"
    r"(?!\s+(?:critical|high|major|medium|moderate|minor|low)" + SEVERITY_SUFFIX + r"(?![\w-]))"
    r"(?!\s+(?:[\w-]+\s+){0,2}?" + SCOPE_NOUN + r"[^.;]*?\b\d+\s+" + FINDINGS_WORD + r")",
    re.IGNORECASE,
)
#: A paragraph whose count reads as more: only such a paragraph is a roll-up.
#: A severity marks one only as a count's ("2 high"), not as a word ("low risk");
#: a findings count marks one when a colon follows it ("4 issues in 2 namespaces: ...").
ROLLUP_MARK = re.compile(
    r"\b(?:more|also|plus|other|another|further|additional|remaining|lower[- ]priority)\b"
    r"|\b\d+\s+(?:critical|high|major|medium|moderate|minor|low)" + SEVERITY_SUFFIX + r"(?![\w-])"
    r"|" + WHOLE + r"\d+\s+" + FINDINGS_WORD + r"[^.:]*:",
    re.IGNORECASE,
)
#: The roll-up wording a paragraph is preferred for over an earlier marked one.
ROLLUP_STRONG = re.compile(r"\b(?:\d+\s+more|also found)\b", re.IGNORECASE)
SEVERITY_TERM = re.compile(r"\s+(?:critical|high|major|medium|moderate|minor|low)(?![\w-])", re.IGNORECASE)
#: What follows a first term that heads the breakdown: "18 items: ...", "18 findings remain (...",
#: "4 issues in 2 namespaces: ...".
HEADS_BREAKDOWN = re.compile(r"(?:\s+[\w-]+){0,4}?\s*[:(]", re.IGNORECASE)
#: The text layout's ask, when the roll-up it drops was the last line.
ASK_ALL = "Ask me to see all {count}."
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
    """The roll-up's count in ``text``, or None when it has none.

    A total it states wins over its breakdown; only with none are the terms
    summed. A paragraph with no roll-up marker (:data:`ROLLUP_MARK`) has none.
    """
    if not ROLLUP_MARK.search(text):
        return None
    more = [int(n) for n in MORE_TOTAL.findall(text)]
    if more:
        return sum(more)
    also = ALSO_FOUND.search(text)
    if also:
        return int(also.group(1))
    terms = list(ROLLUP_TERM.finditer(text))
    if not terms:
        return None
    first = terms[0]
    heads = not SEVERITY_TERM.match(text, first.end(1)) and HEADS_BREAKDOWN.match(text, first.end())
    if len(terms) > 1 and heads:
        return int(first.group(1))
    return sum(int(term.group(1)) for term in terms)


def _split_gap(clause: str) -> tuple[list[str], str]:
    """``(gaps, rest)``: the parts of a posture clause naming what was not scanned, and the rest of it.

    A part naming a gap keeps its parentheticals ("could not be scanned
    (permission denied)"); the rest keeps the scan counts beside it.
    """
    gaps = gap_parts(clause)
    rest = clause
    for gap in gaps:
        rest = rest.replace(gap, " ", 1)
    return gaps, rest


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


def _parse(report: str) -> tuple[str, list[Item], list[str]] | None:
    """Posture, (severity, headline, sentence) items and the trailing paragraphs, or None."""
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
    return " ".join(posture), parsed, tail


def _row(severity: str | None, headline: str) -> str:
    """A finding's headline line, led by its severity as inline code when the headline was labelled."""
    if severity:
        return severity_row(severity, headline)
    return f"{BOLD_MARK}{headline}{BOLD_MARK}"


def _with_sentence(row: str, sentence: str) -> str:
    """``row`` with its finding's sentence on the line under it."""
    return f"{row}\n{sentence}" if sentence else row


class _Shape:
    """A parsed report reduced to its top rows, its total and the closing lines."""

    def __init__(self, posture: str, items: list[Item], tail: list[str]):
        self.top = [item for i, item in enumerate(items) if i < TOP_COUNT or item[0] == CRITICAL]
        counted = [p for p in tail if _rollup_count(p) is not None]
        rollup = next((p for p in counted if ROLLUP_STRONG.search(p)), counted[0] if counted else None)
        # Only the roll-up goes: the headline carries its count, and a closing
        # line with a count in it is still how a text reader asks.
        self.closing = [p for p in tail if p is not rollup]
        clauses = [c.strip() for c in CLAUSE_END.split(posture.replace(BOLD_MARK, "")) if c.strip()]
        split = [_split_gap(c) for c in clauses]
        self.gap = " ".join(as_line(GAP_SEPARATOR.join(gaps)) for gaps, _ in split if gaps)
        self.scanned = [rest for _, rest in split if SCAN_VERB.search(rest)]
        closing_totals = [int(n) for p in tail for n in ALL_TOTAL.findall(p)]
        stated = POSTURE_TOTAL.search(posture)
        if rollup is not None:
            total = len(items) + (_rollup_count(rollup) or 0)
        elif closing_totals:
            total = closing_totals[0]
        elif stated:
            total = int(stated.group(1))
        else:
            total = len(items)
        # Never fewer than the report lists.
        self.total = max(total, len(items))
        self.ask = ASK_ALL.format(count=self.total) if rollup and not self.closing and self.total > len(self.top) else ""


def _shape(report: str) -> _Shape | None:
    parsed = _parse(report)
    return None if parsed is None else _Shape(*parsed)


def present(report: str) -> str:
    """The report as the card's headline, the top findings and the closing lines or the ask; unchanged if it does not parse."""
    shape = _shape(report)
    if shape is None:
        return report
    headline, note, detail = card_headline(shape)
    head = f"{BOLD_MARK}{headline}{BOLD_MARK}" + (f" {note}" if note else "") + (f"\n{detail}" if detail else "")
    rows = "\n".join(_with_sentence(_row(severity, h), sentence) for severity, h, sentence in shape.top)
    return PARAGRAPH_BREAK.join([head, rows, *shape.closing, *filter(None, [shape.ask])]) + "\n"


def _rows(items: list[Item]) -> list[dict]:
    return [{"severity": severity, "text": headline, "detail": sentence} for severity, headline, sentence in items]


def _stated(pattern: re.Pattern, clauses: list[str]) -> int | None:
    """The count ``pattern`` finds in ``clauses``, digits or a number word; None if absent or they disagree."""
    found = {
        int(word) if word.isdigit() else NUMBER_WORDS[word]
        for word in (m.group(1).lower() for c in clauses for m in pattern.finditer(c))
    }
    return found.pop() if len(found) == 1 else None


def _counted(count: int, nouns: tuple[str, str]) -> str:
    return f"{count} {nouns[count != 1]}"


def card_headline(shape: _Shape) -> tuple[str, str, str]:
    """The card's ``(headline, note, detail)``.

    The headline says what was scanned and the total. The lead saying what to
    fix first is the note on the headline's line, or, when the posture names a
    gap, follows the gap on the line under it, so it still runs into the rows.
    """
    scanned = [
        _counted(count, nouns)
        for count, nouns in (
            (_stated(CLUSTER_COUNT, shape.scanned), CLUSTERS_NOUN),
            (_stated(WORKLOAD_COUNT, shape.scanned), WORKLOADS_NOUN),
        )
        if count is not None
    ]
    found = _counted(shape.total, FINDINGS_NOUN)
    if scanned:
        headline = SCANNED_TEMPLATE.format(scanned=SCANNED_JOIN.join(scanned), found=found)
    else:
        headline = FOUND_TEMPLATE.format(found=found)
    shown = len(shape.top)
    lead = ""
    if shape.total > shown:
        if any(item[0] in URGENT_SEVERITIES for item in shape.top):
            one, template = CARD_LEAD_ONE, CARD_LEAD_TEMPLATE
        else:
            one, template = CARD_NEUTRAL_LEAD_ONE, CARD_NEUTRAL_LEAD_TEMPLATE
        lead = one if shown == 1 else template.format(count=COUNT_WORDS.get(shown, str(shown)))
        lead = lead[:1].upper() + lead[1:]
    if shape.gap:
        return headline + LEADS_ON, "", " ".join(part for part in (shape.gap, lead) if part)
    return headline + (LEADS_ON if lead else ROWS_FOLLOW), lead, ""


def blocks(report: str) -> tuple[list[dict], str] | None:
    """The Block Kit report's ``(blocks, text)``, or None when the report does not parse.

    ``text`` is the message's mrkdwn ``text`` field: the headline, the gap and
    lead, then the top rows.
    """
    shape = _shape(report)
    if shape is None:
        return None
    headline, note, detail = card_headline(shape)
    top = _rows(shape.top)
    label = FIX_FIRST if len(shape.top) > 1 else FIX_ONLY
    # The row as the card shows it, so the click's turn names only what was seen.
    first = shown_text(shape.top[0][1])
    choices: list = [(label, FIX_TURN.format(label=label, finding=first))]
    if shape.total > len(shape.top):
        choices.append(SEE_ALL.format(count=shape.total))
    built = blocks_report(
        headline,
        note=note,
        detail=detail,
        rows=top,
        choices=choices,
        action_id_prefix=ACTION_ID_PREFIX,
    )
    return built, fallback_text(" ".join(part for part in (headline, note, detail) if part), rows=top)
