# PowerSync agent instructions

PowerSync is a Home Assistant custom integration for electricity pricing, battery and inverter control, optimisation, and EV charging.
Treat controller, tariff, scheduling, reserve, and device-command changes as safety-critical.
Read the current README, tests, contribution guidance, workflows, and pull-request template before changing behaviour.
Run focused tests first and the complete repository test suite before pushing.
Never test control commands against live Home Assistant or real hardware without explicit authorization.
## Work tracking

- GitHub Issues are canonical for planned, multi-session, or backlog work; small one-PR fixes do not require an issue.
- The user-level `Development` Project is a dashboard, while issues, pull requests, reviews, and CI remain authoritative.
- For issue-backed work, use one issue per branch and pull request, include the issue number in the branch name, and add `Fixes #123` to the pull-request body.
- Keep Project status at `Todo` before work, `In Progress` during implementation or review, and `Done` only after closure or merge.
- Update issue checklists only for verified work; checklist completion is never a merge gate.
