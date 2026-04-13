# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Wiki auto-update workflow on PR merge
- Claude Code workflow: GitHub Actions CI, PR templates, CodeRabbit gate, security scanning
- Python CI pipeline (ruff, mypy, bandit, pytest)
- PR checks workflow with CodeRabbit approval gate
- Documentation check workflow (CHANGELOG enforcement)
- Security scan workflow (gitleaks, dependency review)
- Development section in README
- Max SOC limit: number entity (50-100%), LP optimizer constraint, set_max_soc service, config flow
- Forecast accuracy tracking: MAE, bias, MAPE sensors from 24h error ring buffer
- Load forecast auto-calibration: adaptive pattern weights with exponential decay, persisted across restarts

### Fixed

- Dashboard: entity resolver uses config `entity_prefix`, battery controls use resolver instead of hardcoded names
- Dashboard: error boundaries prevent single card crash from breaking entire dashboard
- Dashboard: FoxESS sensors check entity existence before rendering
- Config: "add another tariff" defaults to unchecked
- Config: Modbus port (1-65535) and slave ID (0-247) range validation on all inverter flows
- Force discharge: GoodWe propagates errors instead of always returning True
- Force discharge: FoxESS returns False when power verify fails after retries
- Force discharge: Sigenergy aborts on power target write failure, propagates restore failure
- Optimizer: software timer not extended when hardware Modbus re-issue fails
- EV: negative energy from clock skew floored at zero
- EV: warning logged when session update called without active session
- EV: stale sessions auto-cleaned after 30 minutes of inactivity
- EV: Zaptec solar detection replaces hardcoded grid-only attribution
