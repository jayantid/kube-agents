# Slack render recipes

The Block Kit each element of the Slack UX refresh posts, as tried live against the dev app in
the render probe thread (2026-09-29). Mock 03 is the reference for the delegated-investigation loop;
mock 08 for the report fold. Build tasks copy these blocks; anything marked fallback is what ships
where Slack will not render the mock.

Status of the user's verdict: pending. Each element below says what was posted, not yet what was
approved.

## The ask's session

Set the status first, then the title. `agents.sessions.rename` returns `not_authorized` on a
thread with no session yet, and `setStatus` turns `:` in its own `title` into `_`.

```json
{"method": "agents.sessions.setStatus", "channel_id": "<channel>", "thread_ts": "<ask ts>", "status": "processing"}
{"method": "agents.sessions.rename", "channel_id": "<channel>", "thread_ts": "<ask ts>", "title": "checkout-gateway restarts (prod)"}
```

Titles are at most 80 characters and may not contain `:`, `/` or `·`; write the environment in
parentheses. `processing` shows Slack's Working… in the thread; `closed` clears it when the answer
posts.

## Stop

Slack's own Stop control needs the app manifest to subscribe to the `agent_session_stopped`
event. Every `setStatus` call warns `missing_agent_session_stopped_event_subscription` until it
does, and the `hermes slack manifest` generator does not emit that event. Until the manifest
carries it, the ack carries our own button, removed when the ack settles:

```json
{
  "type": "actions",
  "block_id": "kage_stop",
  "elements": [
    {
      "type": "button",
      "action_id": "kage_stop",
      "text": { "type": "plain_text", "text": "■ Stop" },
      "value": "stop"
    }
  ]
}
```

Nothing handles `kage_stop` yet; the click handler belongs to build-clicks.

## The ack and its one plan row

One message: a normal-size section naming the target, then a single `task_card` block. A
`task_card` is accepted at the top level, outside a `plan`, which drops the plan's header row; a
`plan` requires a `title`.

```json
[
  {
    "type": "section",
    "text": { "type": "mrkdwn", "text": "checking checkout-gateway." }
  },
  {
    "type": "task_card",
    "task_id": "t1",
    "title": "reading pod state in seeded-a",
    "status": "in_progress",
    "details": {
      "type": "rich_text",
      "elements": [
        {
          "type": "rich_text_list",
          "style": "bullet",
          "elements": [
            {
              "type": "rich_text_section",
              "elements": [
                {
                  "type": "text",
                  "text": "✓ found checkout-gateway only on seeded-a"
                }
              ]
            },
            {
              "type": "rich_text_section",
              "elements": [
                { "type": "text", "text": "◌ reading pod state in seeded-a" }
              ]
            },
            {
              "type": "rich_text_section",
              "elements": [{ "type": "text", "text": "○ checking start times" }]
            }
          ]
        }
      ]
    }
  },
  "<the Stop actions block>"
]
```

Each step is a `chat.update` of the same message with the card's `title` and `details` rewritten,
at most once every 2 seconds. When the answer is ready the card settles to `status: complete` with
a past-tense title ("checked checkout-gateway in 3 steps") and the Stop block is dropped, so the ack
stays in the thread as one line.

`details` must be an object (`rich_text`); a string is rejected. `task_card` also accepts `output`
(`rich_text`) and `sources` (`[{"type": "url", "url": ..., "text": ...}]`). Status is one of
`pending`, `in_progress`, `complete`, `error`. There is no field for the mock's "step 2" counter.

## The answer and its folded why

The headline and the answer in one section, then a native collapsible container. Slack folds and
unfolds it itself, so the fold needs no click handler and no `chat.update`.

```json
[
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Good news: it's not restarting.* Both pods have been up for 45h on seeded-a. ..."
    }
  },
  {
    "type": "container",
    "title": { "type": "plain_text", "text": "why" },
    "is_collapsible": true,
    "default_collapsed": true,
    "child_blocks": [
      {
        "type": "section",
        "text": {
          "type": "mrkdwn",
          "text": "checkout-gateway runs only on *seeded-a* ..."
        }
      },
      {
        "type": "context",
        "elements": [
          {
            "type": "mrkdwn",
            "text": "checked with kubectl get pods, describe, events"
          }
        ]
      }
    ]
  }
]
```

The container needs `title` (plain_text only) or `rich_text_title`; `default_collapsed` is
accepted only alongside `is_collapsible: true`. It also accepts `subtitle` (plain_text). It
rejects `collapsed`, `style` and `border`.

## The report fold (mocks 08 and 15)

Headline section, the top findings as rows in one non-collapsible container (which draws the
border round them), the action and link buttons, then the rest in a collapsed container.

```json
[
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Security & RBAC audit: 7 findings, 2 critical.* 2 are new since yesterday."
    }
  },
  {
    "type": "container",
    "title": { "type": "plain_text", "text": "2 critical" },
    "child_blocks": [
      {
        "type": "section",
        "text": {
          "type": "mrkdwn",
          "text": "`critical`  seeded-b and -c admit privileged pods"
        }
      },
      {
        "type": "section",
        "text": {
          "type": "mrkdwn",
          "text": "`critical`  default service account is cluster-admin on seeded-c"
        }
      }
    ]
  },
  {
    "type": "actions",
    "block_id": "kage_audit",
    "elements": [
      {
        "type": "button",
        "action_id": "kage_audit_0",
        "style": "primary",
        "text": {
          "type": "plain_text",
          "text": "look at the cluster-admin binding"
        },
        "value": "look"
      },
      {
        "type": "button",
        "action_id": "kage_audit_link",
        "text": { "type": "plain_text", "text": "Ledger issue #231 ↗" },
        "url": "<ledger issue url>"
      }
    ]
  },
  {
    "type": "container",
    "title": { "type": "plain_text", "text": "all 7 findings" },
    "is_collapsible": true,
    "default_collapsed": true,
    "child_blocks": ["<one section per remaining finding>"]
  }
]
```

Slack has no severity pill; the severity is inline code. The prompt button falls back to text
until build-clicks proves that a click can reach the session.

## Fallbacks

| Element             | Mock                         | What ships                                                 |
| ------------------- | ---------------------------- | ---------------------------------------------------------- |
| Stop                | Slack's Stop beside Working… | our `kage_stop` button until the manifest subscribes       |
| "step 2" counter    | right-aligned on the row     | none; the step shows in the card's title and `details`     |
| Severity pill       | red outlined label           | inline code                                                |
| Prompt buttons      | send their label as a reply  | plain-text suggestions until build-clicks proves injection |
| The ask's 👀 and ✅ | reactions on the ask         | none until the app is reinstalled with `reactions:write`   |
