"""Slack presentation for a fleet-audit report: a headline, the top findings, the ledger link.

Pure functions only, like :mod:`slack_presenter`. The caller is
``session_kv_server.relay_cron_report``. With ``KAGE_SLACK_UX`` on it finds the
ledger issue the report ends with (:func:`ledger_ref`), fetches that issue, and
posts :func:`blocks_from_issue` as the channel message, mock 08's Block Kit with
its buttons, or :func:`headline_from_issue` as text where Slack refuses the
blocks; when the fetch or the parse fails it posts :func:`headline_fallback`.
The text goes out through ``hermes send``, and the Slack adapter converts
standard markdown to mrkdwn on the way, so the text here is markdown:
``**bold**`` and ``[label](url)``. That conversion is also why the text does
not call :func:`slack_presenter.fallback_text`, whose output is mrkdwn
already: its ``*headline*`` would reach Slack as italics. The rows are
:func:`slack_presenter.severity_row` in both, which reads the same either way.

The audit SOPs relay one line ending in ``— <issue_url>``. The counts and the
findings come from the issue itself, whose title is fleet-audit's
``issue_title`` (``[audit] <name> — <n> findings (<c> critical)``) and whose
body has one ``### <Severity> (<n>)`` section per severity, most severe first,
each finding a ``#### <title> <!-- finding:<id> -->`` heading. Nothing in the
report's own text can set the counts or pick the link, beyond its last URL.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from slack_presenter import ELLIPSIS, HEADLINE_MAX, _clip, _plain, blocks_report, fallback_text, severity_row

ISSUE_URL = r"https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/issues/(?P<number>\d+)"
#: The ledger link: the report's last URL, after the SOPs' dash or a "Ledger:"
#: label, bare, in angle brackets or as a markdown link.
TRAILING_LEDGER = re.compile(
    r"(?:[—–]|\s-|\bledger(?:\s+issue)?:?)\s*(?:\[[^\]\n]*\]\()?<?"
    + ISSUE_URL
    + r">?\)?[\s.]*\Z",
    re.IGNORECASE,
)
#: fleet-audit's ``issue_title``, whole. The coverage-incomplete title does not
#: match, so a run that saw too little is never read as clean.
LEDGER_TITLE = re.compile(
    r"\A\[audit\]\s+(?P<name>.+?)\s+[—–-]+\s+(?P<count>\d+)\s+findings?\s+\((?P<critical>\d+)\s+critical\)\s*\Z"
)
SEVERITY_SECTION = re.compile(r"^###[ \t]+(?P<severity>Critical|Major|Minor)[ \t]+\(", re.MULTILINE)
ANY_SECTION = re.compile(r"^###[ \t]", re.MULTILINE)
#: fleet-audit's ``FINDING_MARKER_RE``.
FINDING_HEADING = re.compile(r"^####[ \t]+(.*?)[ \t]*<!--[ \t]*finding:[ \t]*(\S+?)[ \t]*-->[ \t]*$", re.MULTILINE)
NEW_COUNT = re.compile(r"\b(?P<count>\d+)\s+new\b", re.IGNORECASE)
TRAILING_AUDIT = re.compile(r"\s+Audit$")
AUDIT_SUFFIX = " audit"
#: What is left at the end of the fallback's line once the ledger URL is cut off.
LEDGER_SEPARATORS = " —–-:"
BACKTICK = "`"

TOP_FINDINGS = 2
ROW_TEXT_MAX = HEADLINE_MAX
LEDGER_LINK = "[Ledger issue #{number} ↗]({url})"
ALL_FINDINGS = ": all {count} findings"
CLEAN_HEAD = "**{name}: clean.**"

#: The Block Kit report (mock 08): the choice button names the top finding,
#: the link button the ledger, the fold every finding.
ACTION_ID_PREFIX = "kage_audit"
LOOK_AT = "look at: {finding}"
LEDGER_BUTTON = "Ledger issue #{number} ↗"
FOLD_TITLE = "all {count} findings"


class LedgerRef(NamedTuple):
    url: str
    repo: str
    number: int


def ledger_ref(report: str) -> LedgerRef | None:
    """The ledger issue a report ends with, or None when it does not end with one."""
    match = TRAILING_LEDGER.search(report)
    if not match:
        return None
    number = match.group("number")
    return LedgerRef(f"https://github.com/{match.group('repo')}/issues/{number}", match.group("repo"), int(number))


def has_more(report: str) -> bool:
    """Whether the report says more than its one line, so it is worth posting in the thread."""
    return len([line for line in report.splitlines() if line.strip()]) > 1


def _findings_phrase(count: int) -> str:
    return f"{count} finding" if count == 1 else f"{count} findings"


def _new_count(report: str) -> int:
    match = NEW_COUNT.search(report)
    return int(match.group("count")) if match else 0


def _new_phrase(report: str) -> str:
    count = _new_count(report)
    if not count:
        return ""
    verb = "is" if count == 1 else "are"
    return f" {count} {verb} new since the last run."


def _balanced_clip(text: str, limit: int) -> str:
    """:func:`slack_presenter._clip` that never leaves a code span open."""
    clipped = _clip(text, limit - len(BACKTICK))
    if clipped.count(BACKTICK) % 2 == 0:
        return clipped
    if clipped.endswith(ELLIPSIS):
        return clipped[: -len(ELLIPSIS)] + BACKTICK + ELLIPSIS
    return clipped + BACKTICK


def _severity_findings(body: str) -> list[tuple[str, str]]:
    """``(severity, title)`` for every finding under a severity section, in body order."""
    found = []
    for section in SEVERITY_SECTION.finditer(body):
        start = section.end()
        following = ANY_SECTION.search(body, start)
        text = body[start : following.start() if following else len(body)]
        severity = section.group("severity").lower()
        found += [(severity, title.strip()) for title, _ in FINDING_HEADING.findall(text)]
    return found


class AuditReport(NamedTuple):
    name: str
    count: int
    critical: int
    note: str
    findings: list[tuple[str, str]]


def _parse_issue(issue: dict, report: str) -> AuditReport | None:
    """The ledger issue's counts and findings, or None when it does not parse or disagrees with itself."""
    title = LEDGER_TITLE.match(str(issue.get("title") or "").strip())
    if not title:
        return None
    name = TRAILING_AUDIT.sub(AUDIT_SUFFIX, title.group("name").strip())
    count, critical = int(title.group("count")), int(title.group("critical"))
    if _new_count(report) > count:
        return None  # a stale or wrong ledger: the report has more new findings than it lists
    findings = _severity_findings(str(issue.get("body") or ""))
    if count == 0 and (critical or findings):
        return None
    return AuditReport(name, count, critical, _new_phrase(report).strip(), findings)


def _head(parsed: AuditReport) -> str:
    return f"{parsed.name}: {_findings_phrase(parsed.count)}, {parsed.critical} critical."


def _rows(findings: list[tuple[str, str]]) -> list[dict]:
    return [{"severity": severity, "text": _balanced_clip(text, ROW_TEXT_MAX)} for severity, text in findings]


def headline_from_issue(issue: dict, ref: LedgerRef, report: str = "") -> str | None:
    """The channel message built from the fetched ledger issue, or None when it does not parse.

    ``report`` is the relayed line, read only for its "<n> new" count.
    """
    parsed = _parse_issue(issue, report)
    if parsed is None:
        return None
    link = LEDGER_LINK.format(number=ref.number, url=ref.url)
    if parsed.count == 0:
        return f"{CLEAN_HEAD.format(name=parsed.name)} {link}"
    head = f"**{_head(parsed)}**" + (f" {parsed.note}" if parsed.note else "")
    rows = [severity_row(row["severity"], row["text"]) for row in _rows(parsed.findings[:TOP_FINDINGS])]
    return "\n".join([head, *rows, link + ALL_FINDINGS.format(count=parsed.count)])


def blocks_from_issue(
    issue: dict, ref: LedgerRef, report: str = "", fold_in_place: bool = True
) -> tuple[list[dict], str] | None:
    """Mock 08's ``(blocks, text)`` built from the fetched ledger issue, or None.

    None when the issue does not parse, and for a clean run, which stays the
    one line :func:`headline_from_issue` gives. ``text`` is the message's
    mrkdwn ``text`` field. With ``fold_in_place`` False the fold is left out,
    for a caller whose container Slack refused, which then posts the report
    in the thread instead.
    """
    parsed = _parse_issue(issue, report)
    if parsed is None or parsed.count == 0:
        return None
    top = _rows(parsed.findings[:TOP_FINDINGS])
    choices = [LOOK_AT.format(finding=_plain(top[0]["text"]))] if top else []
    links = [(LEDGER_BUTTON.format(number=ref.number), ref.url)]
    head = _head(parsed)
    blocks = blocks_report(
        head,
        note=parsed.note,
        rows=top,
        choices=choices,
        links=links,
        fold_title=FOLD_TITLE.format(count=parsed.count),
        fold_rows=_rows(parsed.findings),
        action_id_prefix=ACTION_ID_PREFIX,
        fold_in_place=fold_in_place,
    )
    return blocks, fallback_text(head, rows=top, links=links)


def _fallback_line(line: str) -> str:
    match = TRAILING_LEDGER.search(line)
    if match:
        line = line[: match.start()]
    return _clip(_plain(line).rstrip(LEDGER_SEPARATORS), HEADLINE_MAX)


def headline_fallback(report: str, ref: LedgerRef) -> str | None:
    """The report's ledger line in bold with the ledger link, for when the issue could not be read.

    The ledger line is the report's last, the SOPs' one line; a sentence the
    relay turn put above it is not the headline. A last line that is only the
    link falls back to the first.
    """
    lines = [line for line in report.splitlines() if line.strip()]
    head = (_fallback_line(lines[-1]) or _fallback_line(lines[0])) if lines else ""
    if not head:
        return None
    return f"**{head}**\n{LEDGER_LINK.format(number=ref.number, url=ref.url)}"
