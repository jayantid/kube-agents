#!/usr/bin/env python3
"""Deterministic (no-LLM) delivery for first-time onboarding.

This script backs the ``bootstrap-inventory-delivery`` cron job, which runs
with ``no_agent: true``. Its stdout is delivered verbatim by the cron
scheduler to the job's configured target (``deliver: origin`` — the chat the
user first spoke in, bound by the ``bootstrap_onboarding`` plugin).

Delivery happens exactly once, and only when discovery has finished AND a
human has connected:

- ``INVENTORY.md`` present  -> the background scan has produced the report.
- ``.user_aligned`` present -> a human has opened the chat (set by the plugin;
  never by a background task — see the plugin README).
- ``.bootstrap_completed`` absent -> the report has not been delivered yet.

When all three hold, the script claims delivery, prints ``INVENTORY.md``
(verbatim, or reshaped by ``inventory_presenter`` when ``KAGE_SLACK_UX`` is on
and the job is bound to Slack), sets the report aside, and removes the two onboarding
cron jobs. Otherwise it prints nothing, which the ``no_agent`` cron path treats
as a silent run (no message).

The claim is what makes "exactly once" true rather than merely likely.
``.bootstrap_completed`` is created with ``O_CREAT | O_EXCL`` *before* anything
reaches stdout, so of two runs racing on the same report — a scheduled tick and
the plugin's ``trigger_job``, say — exactly one can win the create and emit;
the loser exits silently. Checking the marker and then writing it after
delivery would leave both runs inside the same window, and the user would be
sent the entire onboarding report twice.

Because the prioritization stage writes a finished, presentation-ready
``INVENTORY.md``, no LLM is involved in delivery: what that stage produced is
exactly what the user sees. The sweep's complete findings are a different file
(``INVENTORY.raw.md``) and are never delivered from here.
"""

import os
import sys
from pathlib import Path

SCAN_JOB_ID = "bootstrap-inventory-scan"
DELIVERY_JOB_ID = "bootstrap-inventory-delivery"

# The delivered report is renamed here rather than deleted. It is the only copy
# of a sweep that can take many minutes over a whole fleet, and a chat message
# is easy to lose; keeping it means a re-send is a `cat`, not a re-scan.
DELIVERED_REPORT_NAME = "INVENTORY.delivered.md"

# The only surface the reshaped report is written for; every other one gets it verbatim.
PRESENTED_PLATFORM = "slack"


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _should_deliver(data_dir: Path) -> bool:
    """True only when the report is ready, a human is present, and it has not
    been delivered yet."""
    if (data_dir / ".bootstrap_completed").exists():
        return False
    return (data_dir / "INVENTORY.md").exists() and (data_dir / ".user_aligned").exists()


def _claim_delivery(data_dir: Path) -> bool:
    """Atomically claim the right to deliver the report. True if we won.

    ``O_CREAT | O_EXCL`` is a single filesystem operation, so this is the point
    at which "may I deliver?" and "I am delivering" become indivisible. Called
    before the first byte of the report is written to stdout.

    A False return means another run already claimed it: the caller must emit
    nothing at all.
    """
    completed = data_dir / ".bootstrap_completed"
    try:
        fd = os.open(str(completed), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False  # another run got there first
    except OSError as e:
        # Cannot claim -> cannot safely deliver. Staying silent costs a retry
        # next tick; delivering unclaimed risks sending the report twice.
        sys.stderr.write(f"bootstrap_delivery: could not claim delivery: {e}\n")
        return False
    os.close(fd)
    return True


def _cleanup(data_dir: Path) -> None:
    """Tidy up after the report has been emitted to stdout.

    Onboarding is already marked complete by the delivery claim, so everything
    here is best-effort: a cleanup hiccup must never turn a delivered report
    into a reported failure. Even if job removal fails, ``.bootstrap_completed``
    keeps both jobs inert.

    The report is renamed, not deleted — see ``DELIVERED_REPORT_NAME``. Moving
    it out of the way still matters: the scan job treats a present
    ``INVENTORY.md`` as "discovery already done", so leaving it in place would
    make a later, deliberate re-run of onboarding a no-op.
    """
    try:
        report = data_dir / "INVENTORY.md"
        if report.exists():
            report.replace(data_dir / DELIVERED_REPORT_NAME)
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not archive INVENTORY.md: {e}\n")

    # Remove the onboarding jobs in-process. Self-removing the delivery job
    # mid-run is safe: the scheduler delivers this run's stdout from the job
    # dict it cached at tick time, and a subsequent mark_job_run on a missing
    # job simply logs a warning (see the plugin README, "Architectural Rules").
    try:
        from cron.jobs import remove_job  # type: ignore import-not-found
    except Exception:
        return
    for job_id in (SCAN_JOB_ID, DELIVERY_JOB_ID):
        try:
            remove_job(job_id)
        except Exception as e:
            sys.stderr.write(f"bootstrap_delivery: could not remove {job_id}: {e}\n")


def _origin_platform() -> str | None:
    """The platform the plugin bound this job's delivery to, or None if unknown.

    The plugin writes the origin before ``.user_aligned``, so it is set by the
    time a delivery can fire.
    """
    try:
        from cron.jobs import get_job  # type: ignore import-not-found

        job = get_job(DELIVERY_JOB_ID) or {}
        return (job.get("origin") or {}).get("platform")
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: could not read the delivery origin: {e}\n")
        return None


def _presented(content: str) -> str:
    """The report as delivered: reshaped when ``KAGE_SLACK_UX`` is on and the
    job is bound to Slack, verbatim otherwise.

    Both helpers ship beside this script in ``/opt/defaults/scripts``. Any
    failure to load or reshape delivers the report verbatim, since this runs
    after the claim and a lost report is not retried.
    """
    try:
        import slack_presenter

        if not slack_presenter.enabled() or _origin_platform() != PRESENTED_PLATFORM:
            return content
        import inventory_presenter

        return inventory_presenter.present(content)
    except Exception as e:
        sys.stderr.write(f"bootstrap_delivery: delivering verbatim: {e}\n")
        return content


def main(data_dir: Path | None = None) -> int:
    if data_dir is None:
        data_dir = _data_dir()

    if not _should_deliver(data_dir):
        return 0  # silent run — nothing to deliver yet (or already delivered)

    # Read before claiming, so a read failure leaves no claim behind to undo
    # and the next tick retries cleanly.
    try:
        content = (data_dir / "INVENTORY.md").read_text(encoding="utf-8")
    except OSError as e:
        sys.stderr.write(f"bootstrap_delivery: could not read INVENTORY.md: {e}\n")
        return 1

    # The cheap check above is advisory; this is the decision. Nothing may be
    # written to stdout before it succeeds.
    if not _claim_delivery(data_dir):
        return 0  # another run is delivering this report — stay silent

    sys.stdout.write(_presented(content))
    sys.stdout.flush()

    # Cleanup runs only after the report is safely on stdout (already captured
    # by the scheduler), so removing INVENTORY.md here cannot truncate delivery.
    _cleanup(data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
