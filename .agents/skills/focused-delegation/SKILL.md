---
name: focused-delegation
description: Coordinate focused subagents for complex PowerSync repository work that benefits from independent research, implementation, synthesis, or review, using Pro approach review when the active strategy itself is uncertain. Use when the user requests delegation or subagents, or when a multi-part task has independently verifiable workstreams. Avoid for trivial, tightly coupled, or purely sequential tasks.
---

# Focused Delegation

Keep the primary agent accountable for scope, decisions, integration, validation, and the final answer.

## Decide whether to delegate

1. Read the applicable `AGENTS.md` instructions and inspect current repository state first.
2. Perform thought experiments on competing explanations, edge cases, and failure modes before dividing work.
3. Apply the Pro approach-review gate below before implementation fan-out when the strategy itself is uncertain.
4. Delegate only a concrete, bounded subtask that can progress independently and produce verifiable evidence.
5. Keep work local when coordination overhead, shared-file contention, or context loss would outweigh the benefit.

## Pro approach-review gate

Use `$pro-project-approach-review` before dividing implementation work when the active PowerSync task has competing viable strategies, requires a major cross-subsystem change or refactor, has a broad regression radius, is stuck after a failed approach, or the user explicitly requests Pro review.

- Gather enough local evidence to describe the current approach first. If evidence is missing, delegate only the bounded research needed to collect it, then run the review before implementation fan-out.
- Reuse current saved guidance when Discord triage, optimizer bug hunt, or an earlier task step already ran the review and the goal, evidence, and approach have not materially changed.
- Skip Pro for routine bounded research, settled implementations, independent test work, and review of an already coherent candidate change.
- Treat Pro guidance as advisory. The primary agent remains responsible for reconciling it against live code, repo instructions, and tests before assigning work.
- Keep saved guidance out of commits and releases unless the user explicitly requests it.
- Do not invoke `$pro-project-kickoff` from this skill; focused delegation is scoped to the already-established PowerSync project.

## Assign focused roles

- Use a research agent for targeted code tracing, documentation lookup, or evidence collection. Require file paths, line references, commands, or source links.
- Use an implementation agent for a narrow file or subsystem with explicit acceptance criteria and validation commands.
- Use a synthesis agent only when several independent evidence sets must be reconciled.
- Use a review agent for an independent regression, security, edge-case, or test-coverage pass after a coherent candidate change exists.
- Do not assign multiple agents the same open-ended task.

## Control fan-out

1. Start with one subagent.
2. Add another only when its workstream is independent and materially shortens or strengthens the result.
3. Prefer at most two active subagents. Respect the runtime concurrency limit.
4. Give each agent the minimum task-local context needed, plus applicable repository instructions and exact deliverables.
5. Avoid having agents edit overlapping files. If overlap is unavoidable, keep one agent read-only.

## Integrate and verify

1. Inspect every delegated result; do not accept summaries as proof.
2. Reconcile disagreements against repository code, live evidence, and tests.
3. Review the combined diff for unrelated user changes and shared-state interference.
4. Run the narrowest relevant tests first, then adjacent validation proportional to risk. For this repository, use the project-prescribed `rtk` and Python 3.12 commands.
5. Perform the final review yourself when the runtime cannot select a requested final-review model.
6. Report what was verified and any remaining uncertainty without exposing internal process noise.
