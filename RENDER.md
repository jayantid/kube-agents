# Slack render recipes

The Block Kit each element of the Slack UX refresh posts, as tried live against the dev app in
the render probe thread (2026-09-29) and approved by the user. Mock 03 is the reference for the
delegated-investigation loop; mock 08 for the report fold. Build tasks copy these blocks; anything
marked fallback is what ships where Slack will not render the mock.

Text goes in `rich_text` blocks, not `mrkdwn` sections: rows inside one `rich_text` sit without the
block gap Slack opens between sections.

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

One message: a `rich_text` line naming the target, then a single `task_card` block. A
`task_card` is accepted at the top level, outside a `plan`, which drops the plan's header row; a
`plan` requires a `title`.

```json
[
  {
    "type": "rich_text",
    "elements": [
      {
        "type": "rich_text_section",
        "elements": [{ "type": "text", "text": "checking checkout-gateway." }]
      }
    ]
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

The headline and the answer in one `rich_text`, then a native collapsible container holding the
evidence. Slack folds and unfolds it itself, so the fold needs no click handler and no
`chat.update`.

The fold's label says why the answer holds, written for each answer: "why it's not restarting",
"why it's crashing". It is never a fixed word, and never the method ("how I checked"). Inside the
fold, the evidence rows share one `rich_text_section`, split with `\n`.

```json
[
  {
    "type": "rich_text",
    "elements": [
      {
        "type": "rich_text_section",
        "elements": [
          {
            "type": "text",
            "text": "Good news: it's not restarting.",
            "style": { "bold": true }
          },
          {
            "type": "text",
            "text": " Both pods have been up for 45h on seeded-a. ..."
          }
        ]
      }
    ]
  },
  {
    "type": "container",
    "title": { "type": "plain_text", "text": "why it's not restarting" },
    "is_collapsible": true,
    "default_collapsed": true,
    "child_blocks": [
      {
        "type": "rich_text",
        "elements": [
          {
            "type": "rich_text_section",
            "elements": [
              { "type": "text", "text": "checkout-gateway runs only on " },
              { "type": "text", "text": "seeded-a", "style": { "bold": true } },
              {
                "type": "text",
                "text": " (seeded-reliability). Both pods are Running with 0 restarts, started 45h ago.\nThe only crashlooping workload in the fleet is ..."
              }
            ]
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

A report is its own top-level message in the channel, not a reply in a thread. It is built from
four pieces:

- the headline;
- the critical findings;
- the action and link buttons;
- the rest in a collapsed container.

The critical findings are not boxed in a container. They share the headline's `rich_text`, and
each 🔴 row is its own `rich_text_section`, so every finding starts on its own line with no blank
line between rows. A row is the `red_circle` emoji, a bold "critical", then the finding.

```json
[
  {
    "type": "rich_text",
    "elements": [
      {
        "type": "rich_text_section",
        "elements": [
          {
            "type": "text",
            "text": "Security & RBAC audit: 7 findings, 2 critical.",
            "style": { "bold": true }
          },
          { "type": "text", "text": " 2 are new since yesterday." }
        ]
      },
      {
        "type": "rich_text_section",
        "elements": [
          { "type": "emoji", "name": "red_circle" },
          { "type": "text", "text": " critical", "style": { "bold": true } },
          { "type": "text", "text": "  seeded-b and -c admit privileged pods" }
        ]
      },
      {
        "type": "rich_text_section",
        "elements": [
          { "type": "emoji", "name": "red_circle" },
          { "type": "text", "text": " critical", "style": { "bold": true } },
          {
            "type": "text",
            "text": "  default service account is cluster-admin on seeded-c"
          }
        ]
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
    "child_blocks": [
      {
        "type": "rich_text",
        "elements": [
          {
            "type": "rich_text_list",
            "style": "bullet",
            "elements": [
              {
                "type": "rich_text_section",
                "elements": [
                  { "type": "text", "text": "high", "style": { "bold": true } },
                  {
                    "type": "text",
                    "text": "  3 namespaces have no NetworkPolicy on seeded-b"
                  }
                ]
              },
              "<one rich_text_section per remaining finding>"
            ]
          }
        ]
      }
    ]
  }
]
```

Slack has no severity pill. A critical finding shows as 🔴 followed by a bold "critical". The
prompt button falls back to text until build-clicks proves that a click can reach the session.

## Fallbacks

| Element             | Mock                         | What ships                                                 |
| ------------------- | ---------------------------- | ---------------------------------------------------------- |
| Stop                | Slack's Stop beside Working… | our `kage_stop` button until the manifest subscribes       |
| "step 2" counter    | right-aligned on the row     | none; the step shows in the card's title and `details`     |
| Severity pill       | red outlined label           | 🔴 and a bold "critical", one row per finding              |
| Prompt buttons      | send their label as a reply  | plain-text suggestions until build-clicks proves injection |
| The ask's 👀 and ✅ | reactions on the ask         | none until the app is reinstalled with `reactions:write`   |
