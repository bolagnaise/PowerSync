---
name: powersync-discord-triage
description: Triage PowerSync Discord bug tickets end to end — discover open Tickets v2 channels, apply the repo's diagnostic gates, request the right evidence per bug class, escalate complex evidenced fix approaches for Pro review, and hand off to fix/release work. Use when the user mentions a PowerSync Discord ticket, asks to sweep/check tickets, or pastes a Discord ticket URL while working in the power-sync repo.
---

# PowerSync Discord Triage

The deep diagnostic knowledge (gates, symptom router, per-brand quirks, evidence tables, release process) lives in [AGENTS.md](/Users/benboller/Developer/power-sync/AGENTS.md) at the repo root — read it first if it isn't already in context. This skill covers the Discord mechanics and procedure.

## Server layout

- PowerSync server, guild `1443943629130567874`. Tickets Bot dashboard: `https://dashboard.tickets.bot/manage/1443943629130567874/tickets` (auth-gated; prefer channel reads).
- Tickets are numbered channels `#<number>-<discord_handle>` (e.g. `#234-jt099493`). `#report-bug` is intake only. Brand channels (`#tesla`, `#sungrow`, `#fox-ess`, `#sigenergy`, `#goodwe`, ...) and provider channels (`#amber-electric`, `#flowpower`, `#globird`, `#epex-eu`, `#octopus-energy`) are context, not the bug list.
- `PowerBot#1455` posts the intake template; `Powersync#1204` is the automation's reply account; `Bolagnaise` is the owner.

## Channel discovery (no list API exists)

The Discord connector (`mcp__discord__read-messages`, loaded via ToolSearch) cannot list channels. Discover tickets by requesting a deliberately invalid channel name — the error message returns the full `Available channels` list. Parse out names matching `^[0-9]+-`. If discovery fails, probe the next sequential numbers above the highest known ticket, or fall back to the dashboard. Never report "no open tickets" when discovery itself failed.

## Sweep procedure

1. Discover the current numbered-channel set (above).
2. Read the newest messages of each candidate (`limit: 30`, raise to 100 if the timeline is incomplete). A ticket whose newest message is from the reporter needs attention; one whose newest message is our own reply is awaiting the user.
3. Prioritize: fresh reporter replies in older tickets outrank brand-new intake-only tickets.
4. For each active ticket, apply the AGENTS.md gates **in order** before forming a diagnosis:
   version reconciliation → log-window validation → monitoring-mode check → classify (bug / stale error / config mistake / misunderstanding / feature request / needs-evidence) → attachment verification.
5. Record per-ticket state (a simple ledger: status, last-seen timestamp, `already_asked`, `missing_evidence`, `next_action`) so nothing is re-asked and follow-ups aren't dropped. Statuses: `new`, `diagnosing`, `awaiting-user-evidence`, `awaiting-retest`, `fix-shipped vX.Y.Z`, `stale`, `confirmed-resolved`.

## Requesting evidence

Always include the paste-ready debug snippet and ask for one full optimization cycle at the symptom time:

```yaml
logger:
  logs:
    custom_components.power_sync: debug
```

Then add the per-bug-class items from the AGENTS.md evidence table (sensor attribute screenshots, toggles, plan/preset names), **all from the same timestamp**. Ask for exactly what is missing — never re-request evidence already supplied earlier in the ticket.

## Diagnostic depth standard

For every new or materially changed technical ticket, complete a second-pass causal trace before replying, classifying it as evidence-blocked, or handing it off:

1. Map the reported outcome through the full current-code path: raw input/entity or API response → normalization/cache/state → optimizer or automation decision → post-solve/action routing → brand/provider command or user-visible sensor/chart. Mark each link as confirmed, inferred, or missing.
2. Trace callers and callees past the first matching log line or plausible function. Check condition precedence, state restoration, fallbacks, stale caches, async timing, units/signs, timezones, and display-only transformations where relevant.
3. Keep at least two credible competing explanations alive until evidence or code falsifies them. Include PowerSync logic, configuration/external control, telemetry/display, and already-fixed version drift when plausible.
4. Inspect current tests and recent related fixes. When the suspected failure is deterministic from current code, reproduce it with the smallest focused harness or failing regression test even if the reporter's runtime capture is stale or incomplete.
5. Separate the **patch gate** from the **investigation gate**, then locate the failure relative to the Monitoring Mode suppression boundary. Monitoring Mode is not itself a patch blocker. If the failure is in a decision, schedule, sensor/chart, state transition, pre-command routing, or any other path that still executes under Monitoring Mode and is reproducible in current code, patch and test it normally. A `[MONITORING] ... blocked` line proves only that no hardware command was sent; it blocks an actuation-path claim only when the alleged failure depends on what the suppressed command would actually have done and cannot be reproduced in current code or a focused harness. Stale logs or a missed window can still prevent a current-version fix claim, but they do not end source tracing.
6. Stop for evidence only after current-code tracing cannot distinguish the remaining hypotheses. State the exact missing runtime value, the competing branches it separates, and why code/tests cannot answer it.

Diagnostic closure requires one of: a concrete root cause backed by current code and a focused reproduction; a code-backed configuration/semantic explanation; proof the defect is already fixed; or one irreducible evidence request after the trace above. A plausible first explanation is not closure.

## Pro approach-review escalation

Use `$pro-project-approach-review` only after the AGENTS.md gates establish a current bug with enough evidence to describe the root cause or the remaining competing explanations. Invoke it before editing when at least one of these is true:

- Two or more credible implementation or debugging approaches remain.
- The likely fix crosses subsystems, changes architecture, or has a broad regression radius.
- A prior fix did not cover the newly reported variant.
- The work requires a major optimizer/state-machine change, refactor, debugging strategy, or course correction.
- The user explicitly asks for a Pro approach review.

Do not invoke it for ticket discovery, stale-version reports, missed log windows, monitoring-mode observations, missing-evidence requests, configuration mistakes, semantic misunderstandings, routine replies, or narrow fixes whose root cause and regression test are already clear. Pro review cannot substitute for missing runtime evidence.

When escalating:

1. Finish the applicable repo workflow first, including `$optimizer-bug-hunt` for optimizer or force/reserve changes.
2. Give Pro only redacted, task-relevant facts. Anonymize the reporter and never paste credentials, private profile details, raw browser/session data, or unnecessary Discord history.
3. Preserve the ticket's requested outcome and the repo's diagnostic, testing, dirty-worktree, and release constraints.
4. Treat the review as advisory: reconcile it against live code, AGENTS.md, current-version evidence, and tests before changing the implementation plan.
5. Keep the review and saved guidance internal unless the user explicitly asks to share it in Discord.

## User escalation verbs (interactive sessions)

- Bare ticket URL → read-only triage, post nothing.
- `reply` / `reply to him` / `post it` (typos like `post i`, `fic it` count) → post the drafted reply in the same ticket, after re-reading the channel for freshness.
- `fix it` / `do it` / `add it` → implement in the repo, using the Pro escalation above when its criteria are met; verify per AGENTS.md, then report or reply.
- `release` / `release it` → full manifest-driven release flow (see AGENTS.md), then reply telling the user to update — only after the release is published.

## Reply style

Concise, operational, code-backed. Answer the newest user question first. Separate confirmed facts from inference. Describe logged actions verbatim (`self_consumption`, not "force discharge"). No process noise (internal workflow, "docs not needed", release housekeeping). If a user posts credentials or remote-access details, don't quote them back and remind them to rotate/revoke afterwards.

## Closing tickets

Only the owner or the automation deletes tickets, via the Tickets v2 control panel in the channel (red **Delete** on the original Tickets bot message → red **Confirm**); the connector has no delete API. A ticket is resolved only when the reporter confirms the fix works **on the current release version** — "works" after a downgrade or workaround is not resolved. Vague thanks or "will test" is not confirmation.
