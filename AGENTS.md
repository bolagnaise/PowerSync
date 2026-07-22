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

## Cursor Cloud specific instructions

- Runtime is Python 3.12 (`.python-version`). Dependencies are installed by the `.cursor/environment.json` `install` step (pip: `pytest`, `aiohttp`, `aemo-to-tariff`, `cryptography`, `goodwe`, `protobuf`, `highspy`, `tzdata`); there is no `requirements.txt`/`pyproject.toml`. Runtime deps are declared in `custom_components/power_sync/manifest.json`.
- `tzdata` is required (not optional): `aemo-to-tariff` resolves IANA zones like `Australia/ACT` at import time, which fails on the minimal VM without it.
- Tests: `python3 -m pytest` from the repo root (config is `pytest.ini`). Home Assistant is fully stubbed via `sys.modules`, so no Home Assistant install is needed. Prefer running focused test files.
- A full `python3 -m pytest` run currently reports ~57 failures out of ~1990 that are test-isolation/stale-stub issues, not environment problems: many pass in isolation (cross-module `sys.modules` stub pollution), and files like `tests/test_ev_vehicle_status.py` force-re-import `power_sync` against an incomplete `power_sync.optimization.coordinator` stub that omits symbols the current `__init__.py` imports (e.g. `sigenergy_capped_optimizer_limit_w`). Treat these as pre-existing; validate your change with the focused tests covering it.
- Lint/build: no linter/formatter is configured. CI (`.github/workflows/validate.yml`) only runs HACS + hassfest manifest validation. Use `python3 -m compileall custom_components` as a fast syntax check.
- The integration cannot run standalone — it requires a full Home Assistant instance and (for real use) a price source and battery/inverter. The core LP optimizer can be exercised without Home Assistant by installing HA stubs and importing `power_sync.optimization.battery_optimizer` (see `scripts/benchmark_lp_optimizer.py` for the stub pattern). Note that script references a stale `SCIPY_AVAILABLE` attribute and will not run as-is; the module now exposes `HIGHS_AVAILABLE` (HiGHS via `highspy`) and falls back to a greedy solver when `highspy` is missing.
