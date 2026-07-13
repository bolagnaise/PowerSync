---
name: powersync-discord-triage
description: Triage PowerSync Discord bug tickets end to end — discover open Tickets v2 channels, apply the repo's diagnostic gates, request the right evidence per bug class, and hand off to fix/release work. Use when the user mentions a PowerSync Discord ticket, asks to sweep/check tickets, or pastes a Discord ticket URL while working in the power-sync repo.
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

## User escalation verbs (interactive sessions)

- Bare ticket URL → read-only triage, post nothing.
- `reply` / `reply to him` / `post it` (typos like `post i`, `fic it` count) → post the drafted reply in the same ticket, after re-reading the channel for freshness.
- `fix it` / `do it` / `add it` → implement in the repo, verify per AGENTS.md, then report or reply.
- `release` / `release it` → full manifest-driven release flow (see AGENTS.md), then reply telling the user to update — only after the release is published.

## Reply style

Concise, operational, code-backed. Answer the newest user question first. Separate confirmed facts from inference. Describe logged actions verbatim (`self_consumption`, not "force discharge"). No process noise (internal workflow, "docs not needed", release housekeeping). If a user posts credentials or remote-access details, don't quote them back and remind them to rotate/revoke afterwards.

## Closing tickets

Only the owner or the automation deletes tickets, via the Tickets v2 control panel in the channel (red **Delete** on the original Tickets bot message → red **Confirm**); the connector has no delete API. A ticket is resolved only when the reporter confirms the fix works **on the current release version** — "works" after a downgrade or workaround is not resolved. Vague thanks or "will test" is not confirmation.
