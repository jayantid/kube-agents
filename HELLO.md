# The first-install hello

Spec for task 5b. It fixes what the chat agent says the first time a person talks to it after an
install, in both states the plugin distinguishes: the fleet sweep still running, and the sweep
done. It also fixes the eval that has to go red on `main` and green on the change, and the seam
that lets that eval fire the greeting on demand. Nothing here is built yet.

Sources: `agents/chat/defaults/plugins/bootstrap_onboarding/plugin.py`,
`agents/chat/scripts/bootstrap_delivery.py`, the two prompts in `agents/chat/defaults/onboarding/`,
`kage-ux-proto/DECISIONS.md` §1, Voice and §15, and mock 15.

## What the plugin does today, and what that constrains

- The greeting fires on the first interactive turn on Slack or Google Chat only
  (`DURABLE_CHAT_PLATFORMS`, `plugin.py:20`, checked at `:126`), once per deployment
  (`.bootstrap_greeted`). The plugin binds report delivery to that chat, marks `.user_aligned`,
  triggers the delivery job, then injects one of the two prompts as context. The model writes the
  greeting; there is no template.
- The variant is chosen by whether `INVENTORY.md` exists. The greeter never reads it: the report
  is posted verbatim by `bootstrap_delivery.py`, a separate no-LLM message.
- **In the "done" variant the report can land before the greeting.** The plugin triggers delivery
  before the model runs, so the greeting must not say "above", "below" or "next".
- **If the sweep fails, no report ever arrives** (plugin README: "or forever, if the sweep
  fails"). So the "running" variant must not promise a time, and must not promise more than
  "I'll post it here when it's done". A failed sweep saying nothing is a separate trust bug; this
  spec does not fix it.
- **The model knows the person's name on Slack**: Hermes resolves it with `users.info` and puts it
  in the session context (`gateway/session.py:419-420`, v2026.9.14). When that call fails it
  falls back to the raw user ID (`plugins/platforms/slack/adapter.py:3100-3123`), so the model can
  be handed `U04ABCD12` as a "name".
- **Nothing knows the person's time zone.** Every cron schedule is UTC and nothing sets
  `HERMES_TIMEZONE` (`agents/platform/scripts/eod_report_generator.py:134`). Slack's `users.info`
  returns a `tz`, but Hermes drops it. So no time-of-day greeting ("Morning, Priya"), and no
  question whose answer nothing uses.

## What the first message conveys, in order

One message, at most 60 words, plain sentences, no bullets or headings. Each point is there
because leaving it out costs trust.

1. **Who it is, in one line: "Hi Priya, I'm Kage."** A person who just installed something wants
   to know what is talking to them, not how it is built. No "Planning Agent", Platform Agent,
   Cluster Agent, specialists, kanban or hierarchy: internal names ask the reader to learn an
   architecture, and today's greeting leads with them. The name comes from the session; if it
   looks like an ID (`U` plus capitals and digits) or is missing, say "Hi there". One 👋 is
   allowed, here only.
2. **What it is doing right now, and that it changes nothing.** "I'm taking a first look at your
   GKE fleet. I'm only reading; nothing in your clusters changes." The first fear after installing
   an agent with cluster credentials is that it is about to do something. The claim is true: the
   sweep reads, the Kubernetes RBAC footprint is read-only in every configuration, and the default
   GCP permission set is viewer roles (site `reference/security-and-iam.md`). Say it about the
   sweep, not as a blanket guarantee, because a `custom` permission set can widen IAM.
3. **Where and when results appear.** Running: "I'll post what I find here when it's done." Done:
   "My first look is done, and the summary is in this chat." "Here" is exactly true: delivery is
   bound to this chat. No minute estimate, since sweep time scales with fleet size and a failed
   sweep never delivers (open question 2).
4. **Changes come as pull requests a person reviews.** "If I think something should change, I'll
   open a pull request for your team to review." This is the enforced write path: the credential
   proxy refuses merge and approve, so the agent cannot complete its own change. It answers the
   second fear ("will it fix things behind my back?") before it is asked.
5. **One question, last, and it is an offer.** Proposed: **"Is there anything you want me to look
   at first?"** (done variant: "Want me to start on one of those findings?"). The answer is used
   in the same conversation, so the question promises nothing that does not exist. It gives the
   person something to do while the sweep runs, and it matches DECISIONS §1 and §15 ("ends on an
   offer").

   Rejected: the mock's time-zone question. Nothing consumes the answer (see above), so asking it
   implies morning reports on their clock that would in fact arrive at UTC times. Also rejected:
   "which clusters are production?", because nothing reads that answer either.

**When the first message is a real ask**, not a hello ("why is checkout crashlooping?"), answer
the ask first, then add points 1-4 in two sentences at the end, and skip the question. Someone
who arrives with a problem should not be made to read an introduction before the answer.

## What it must not do

- Ask more than one thing. Today's prompt asks for SOPs, governance workflows and time zone at
  once; three asks in a first message is homework.
- Explain architecture or list capabilities ("I can help you with…"). DECISIONS §1: no list of
  capabilities.
- Claim to have saved, noted or remembered anything. It holds no memory-write tool.
- Promise what nothing does: morning reports at their local time, a scan duration, "I'll keep an
  eye on it", "I'll follow your runbooks".
- Restate, summarise or preview the inventory. It has not read it, and in the done variant it is
  already in the chat.
- Apologise, or use hype ("excited", "thrilled", "supercharge", "seamless").
- Say "let me know", or narrate process ("I've filed a task…"). DECISIONS Voice.
- Greet by time of day, or greet a user ID as if it were a name.

## What happens to the SOP, governance and time-zone ask

- **SOPs and governance:** asked once, by Kage, at the moment they matter: the first time it hands
  a change to the Platform Agent to become a pull request ("Before I open it: does your team have
  a change process or runbook I should follow? Paste or link it."). The answer is filed to
  `platform` with `kanban_create`, as today's prompt already does. If someone volunteers runbooks
  earlier, the same filing applies. This needs a chat SOUL/SOP edit that is **not** part of 5b; it
  is a follow-up, and until it lands nobody asks, which is better than asking in the hello.
- **Time zone:** not asked until a feature uses it. The honest route is reading `tz` from Slack's
  `users.info`, which needs no question at all; that is a Hermes change and out of scope here.
- The prompts' "full inventory" section (file a card to `platform` for `INVENTORY.raw.md`) stays
  as it is. It governs later turns, not the hello.

## Samples

### Scan running

> Hi Priya, I'm Kage 👋 I'm taking a first look at your GKE fleet. I'm only reading, so nothing in
> your clusters changes, and I'll post what I find here when it's done. If I think something
> should change, I'll open a pull request for your team to review. Is there anything you want me
> to look at first?

Name unresolved:

> Hi there, I'm Kage 👋 I'm reading through your GKE fleet now; nothing in your clusters changes.
> I'll post what I find in this chat when it's done, and anything I think should change will come
> as a pull request for your team to review. Is there anything you want me to look at first?

First message is a real ask ("why is checkout crashlooping in prod-east?"):

> _(the answer to the crashloop first, in the normal reply voice)_
>
> I'm Kage, by the way. I'm also taking a first, read-only look at your whole fleet and will post
> it here when it's done; any change I suggest comes as a pull request for your team to review.

### Scan done

> Hi Priya, I'm Kage 👋 My first look at your GKE fleet is done, and the summary is in this chat.
> I only read your clusters, so nothing changed. If I think something should change, I'll open a
> pull request for your team to review. Want me to start on one of those findings?

> Hi Sam, I'm Kage 👋 I've finished a read-only first pass over your GKE fleet; the summary is in
> this chat. Nothing in your clusters changed, and any fix I suggest will come as a pull request
> your team reviews. Want me to start on one of those findings?

First message is a real ask: as in the running variant, answer first, then "I'm Kage, by the way.
My first read-only look at your fleet is done and the summary is in this chat; any change I
suggest comes as a pull request for your team to review."

## The eval

New cases `bench/tasks/first-install-hello-running/` and `-done/`, `deployer: tofu`, one per
variant, because the variant is chosen by a file the provisioner controls. The prompt is
`hi! priya here, just installed you` (an api_server run carries no Slack user name, so the name
comes from the message). Registered in `hack/eval/nightly-cases.txt`, not the blocking roster.

All checks are `report_contains`, `mode: assert`, `scope: final`, over the lowercased,
emphasis-stripped reply (`bench/kube_agents_bench/verifiers.py`). Phrases are forgiving
substrings; patterns are `re.search`.

| #   | Role                | Check                                                                                                                                                             | Trust reason                                       | Red on `main` today?                                                              |
| --- | ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- | --------------------------------------------------------------------------------- |
| 1   | objective           | `required_phrases: [priya]`                                                                                                                                       | It greets the person, not "the team"               | Probably green: the name is in the message                                        |
| 2   | objective           | `required_phrases: [kage]`                                                                                                                                        | It says who it is in one word                      | **Red**: main says "Planning Agent"                                               |
| 3   | objective           | `any_of_phrases: [read-only, only reading, only read, nothing in your clusters, won't change, nothing changed, not changing]`                                     | The first fear is that it will change things       | **Red**: neither prompt asks for it                                               |
| 4   | objective           | `any_of_phrases: [pull request, as a pr, a pr for]`                                                                                                               | Changes are reviewed by a person                   | **Red** on running: its prompt never mentions PRs. Done may pass: its prompt does |
| 5   | objective (running) | `any_of_phrases: [post, share, send]` and `[here, this chat]`                                                                                                     | Says where results go                              | Probably green                                                                    |
| 5   | objective (done)    | `any_of_phrases: [in this chat, in the chat, posted]`                                                                                                             | Says where the results are                         | Probably green                                                                    |
| 6   | objective           | `forbidden_patterns: ['\?[\s\S]*\?']` (two question marks)                                                                                                        | One question, not homework                         | **Red**: main asks for SOPs, governance and time zone                             |
| 7   | objective           | `forbidden_patterns: ['[^?\s]\s*\Z']` (last character is not `?`)                                                                                                 | Ends on an offer                                   | Unknown                                                                           |
| 8   | objective           | `forbidden_patterns: ['\A(?:\S+\s+){60}\S']` (more than 60 words)                                                                                                 | Short enough to read in a notification             | **Red**: main's greeting is multi-paragraph                                       |
| 9   | safeguard           | `forbidden_phrases: [planning agent, platform agent, cluster agent, specialist, kanban, hierarchy]`                                                               | No architecture                                    | **Red**: main is told to introduce all three agents                               |
| 10  | safeguard           | `forbidden_patterns: ['\bsops?\b', 'time ?zone', 'governance']`                                                                                                   | No stacked asks                                    | **Red**: main asks for all three                                                  |
| 11  | safeguard           | `forbidden_patterns: ['\b(saved\|remember(ed)?\|noted\|stored)\b']`                                                                                               | Never claims a save                                | Probably green: main forbids it                                                   |
| 12  | safeguard           | `forbidden_patterns: ['(?m)^\s*(?:[-•]\|\d+[.)])\s', 'i can help( you)? with']`                                                                                   | No capability list                                 | Likely red on running: its optional roadmap invites a list                        |
| 13  | safeguard           | `forbidden_patterns: ['\b(sorry\|apologi[sz]e)', '\b(excited\|thrilled\|delighted\|supercharge\|seamless)', 'let me know', 'good (morning\|afternoon\|evening)']` | No apology, hype, filler, or time-of-day guess     | Unknown                                                                           |
| 14  | safeguard (done)    | `forbidden_patterns: ['\b(above\|below)\b']`                                                                                                                      | The report may arrive before or after the greeting | Unknown                                                                           |

The red has to come from checks 2, 3, 6, 8, 9 or 10: each is a sentence main's prompt asks for or
omits. Checks 1, 5 and 11 guard against a rewrite that loses what main already gets right.

## The eval seam

The case runs through the api_server, which the plugin excludes on purpose (`plugin.py:126`), and
the greeting fires once per deployment. The seam is a one-shot marker:
`/opt/data/.bootstrap_greet_eval`.

- **Who writes it:** only the case's tofu provisioner, with `kubectl exec … touch`. For the done
  variant it also writes a stub `INVENTORY.md`, and removes it on destroy. No image, chart,
  operator, installer or script writes that path, and build-hello adds a test asserting that
  nothing outside `bench/` and the plugin names it.
- **What it does:** on a first turn that is not cron, if the marker exists, the plugin unlinks it
  (`FileNotFoundError` means another turn took it: return `None`), then injects the greeting
  instructions for the variant `INVENTORY.md` selects, on any platform. It does **not** bind
  delivery, touch `.user_aligned`, trigger the delivery job, or touch `.bootstrap_greeted`, so a
  real install's onboarding state is untouched even if someone creates the file by hand.
- **Why it is inert when absent:** the marker check runs after the existing first-turn and cron
  guards; with no file, control reaches the unchanged platform check and every later line as on
  `main`. build-hello's tests assert that, with no marker present, the hook's return value and
  every marker file match `main` across the plugin's existing cases.
- **Red run:** `main` plus the seam commit alone, with today's prompts. The seam changes when the
  greeting fires, not what it says, so the red measures `main`'s text.

## Open questions for you

1. **The flag.** The shared brief puts everything behind `KAGE_SLACK_UX` (default off). If the new
   prompts are gated, presubmit and nightly installs run with the flag off and the eval would
   measure the old text. Proposal: the seam marker selects the new text for its one turn, so the
   case measures what flag-on users get. Alternative: ship the hello ungated, since it is a prompt
   change rather than presentation. Which?
2. **A time estimate.** No number is proposed, because sweep time scales with fleet size and a
   failed sweep never delivers. If you want "usually about N minutes", build-hello measures the
   sweep on the seeded fleet first.
3. **"Kage" everywhere.** The chat SOUL calls it the Planning Agent. If only the hello says Kage,
   the next reply can contradict it. Proposal: 5b also changes the SOUL's identity line to Kage.
4. **Fleet state in the done variant.** DECISIONS §15 wants the greeting to state fleet state. The
   greeter cannot read the report; saying "3 clusters, one needs a look" needs the plugin to pass
   the report's headline into the prompt. Proposal: not for Oct 6; the report itself carries the
   state.
