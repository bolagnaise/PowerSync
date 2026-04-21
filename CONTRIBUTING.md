# Contributing to PowerSync

Thank you for your interest in contributing to PowerSync! This guide helps you get started.

## Getting Started

1. **Fork** the repository on GitHub
2. **Clone** your fork locally
3. **Create a branch** from `main` for your change
4. **Make your changes** following the guidelines below
5. **Submit a pull request** back to `main`

## Development Setup

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/PowerSync.git
cd PowerSync

# Install in your Home Assistant dev environment
# Copy custom_components/power_sync/ to your HA config/custom_components/
```

## Pull Request Guidelines

### One Change Per PR

Each PR should contain **one logical change**. Don't bundle unrelated fixes or features.

**Good:**
- `fix(amber): handle missing site ID in API response`
- `feat(ev): add per-vehicle charging priority`

**Avoid:**
- `fix: assorted bug fixes and code cleanup`
- `feat: new dashboard + bug fixes + formatting`

### Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
{type}({scope}): {description}
```

**Types:** `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `style`, `chore`

**Scopes:** `optimizer`, `ev`, `amber`, `octopus`, `aemo`, `sigenergy`, `sungrow`, `foxess`, `goodwe`, `solax`, `alphaess`, `sensor`, `frontend`, `config`, `ci`

### Code Standards

- **Type hints** on all public functions
- **No bare `except:`** — always catch specific exceptions
- **No secrets** in code or logs — redact tokens, API keys, credentials
- **Async/await** — no blocking calls in async methods
- **Logging** — use `_LOGGER.debug/info/warning/error`, not `print()`

### Critical Files

These files control battery charging/discharging/pricing for every user. Extra care required:

- `coordinator.py`
- `__init__.py`
- `optimization/coordinator.py`
- `optimization/battery_optimizer.py`
- `optimization/executor.py`

### Formatting

Do **not** run whole-repo formatting tools (ruff format, black, etc.) on files you didn't change. Format only files modified in your PR.

## Reporting Bugs

Open an issue with:
- PowerSync version (from manifest.json)
- Home Assistant version
- Battery system and electricity provider
- Steps to reproduce
- Relevant logs (Settings → System → Logs, filter by `power_sync`)

**Redact** any tokens, API keys, or personal information from logs.

## Security Issues

For security vulnerabilities, see [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).
