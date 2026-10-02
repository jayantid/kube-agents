# rv-final-attempt: local only, not for a PR yet

The SOUL.md change (§1 exception and §2 step 5: a `crashed` or `timed_out` wake is
being retried; any `gave_up` wake was the last attempt) and its case
`chat-voice-final-attempt-is-not-retried`, moved off the retry-voice branch `rv` at
its `5a56b0af` so that `rv` can open as "main already passes; the case guards it".
Cut from origin/main `dceb7ed3`. The SOUL.md here is byte-identical to `rv`'s at
`5a56b0af`; main has not touched SOUL.md since `37c6b216`.

## Depends on rv

The case's prompt is a `[bench:card-failure-wake]` replay with
`outcome: timed_out_final`, which needs `rv`'s `card_wake` plant and harness wiring.
Merge main after `rv` lands, then re-add the final-attempt parts of
`NOTES.rv-hunks.patch` (the `37c6b216..5a56b0af` diff of the rosters and tests;
take only what names the final-attempt case):

- `hack/eval/nightly-cases.txt`: its task line and the count comment (+3).
- `hack/eval/inject-lane-exclusions.txt`: its #2039 entry.
- `scripts/test_eval_rosters.py`: its `ADDED_AFTER_THE_MOVE`, `INJECT_LANE_EXCLUDED`
  and `INJECT_LANE_EXCLUDED_TIER` entries.
- `bench/tests/test_verifiers.py`: `_FINAL_CASE`, the two
  `test_the_final_attempt_objectives_*` tests, and `_FINAL_CASE` in the fence test's
  parametrize.
- `bench/tests/test_card_wake.py`: its `WORKER_CASES` entry.

## The red on record (checks frozen at 31577f10; this case's checks unchanged since)

Install `jayantid-gkedemos`, image `main-37c6b216` (`main` at `37c6b216`):
`platform-agent` `e9c17322`, `credential-proxy` `478684eb`, `agent-sandbox`
`bf997bc6`, operator `kube-agents-operator:issue-1684-eval` `71901f2e`. Evidence:
`/tmp/claude/tm8-lease/evidence-31577f10/red{1,2,3}-main-37c6b216-chat-voice-final-attempt-is-not-retried/`.

- `run_20261002_025946_683943`: fails `the-reply-says-it-stopped` (none of the
  stopped or not-retried phrasings). Reply: "… timed out twice and is now blocked …
  I can unblock the card to try again, or route a new task to the platform agent …"
- `run_20261002_030034_074365`: fails `the-reply-says-it-stopped`. Reply: "…
  repeatedly timed out after running for 30 minutes without finishing. It is now
  blocked. Would you like me to try assigning this to the `platform…`"
- `run_20261002_030119_280527`: fails `the-reply-says-it-stopped` and
  `front-door-starts-no-new-work` (`kanban_create` in the router trajectory).
  Reply: "… I've recreated the task to have it try checking the `invoice-renderer`
  pods' events and previous container logs again." Then
  `KUBE_AGENTS_DELEGATION_CEILING` on the recreated task `t_4ea809b5`.

Valid red: 3 of 3 fail for the reason the SOUL change addresses (the reply reports a
blocked card and offers to unblock or retry instead of saying it stopped).

## Still owed

- Green: three runs on an image built from this branch (after the rv merge above),
  in a lease gap that does not collide with the user's Slack session. The
  `rv2g-5ec2ed34` image (`platform-agent` `e76f5159`) carries this SOUL.md and is
  still current if nothing under the image changes; check before reusing it.
- The retry case's checks moved on `rv` after the freeze (tm-4's findings); re-run
  the fresh-context review of this case and the SOUL change before opening.
