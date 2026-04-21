# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in PowerSync, **please report it responsibly**.

**Do not** open a public GitHub issue for security vulnerabilities.

### How to Report

Email the maintainer directly or use [GitHub's private vulnerability reporting](https://github.com/bolagnaise/PowerSync/security/advisories/new).

Include:
- Description of the vulnerability
- Steps to reproduce
- Affected versions
- Potential impact

### What to Expect

- Acknowledgement within 48 hours
- Assessment and fix timeline within 1 week
- Credit in the release notes (unless you prefer anonymity)

## Scope

PowerSync handles:
- **Battery control commands** (charge, discharge, reserve levels) via Modbus, REST APIs, and BLE
- **Electricity pricing data** from Amber Electric, Octopus Energy, AEMO, EPEX, and others
- **Tesla authentication tokens** (Fleet API OAuth, local Powerwall pairing)
- **Inverter credentials** (Sigenergy cloud, FoxESS, GoodWe, AlphaESS API keys)

Vulnerabilities in any of these areas are in scope.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Previous minor | Best effort |
| Older | No |

## Security Best Practices for Users

- Keep PowerSync updated to the latest version
- Use HTTPS for all external API connections
- Store API keys and tokens in Home Assistant's secrets management
- Review HA logs periodically for unexpected access patterns
- Restrict network access to inverter Modbus/API endpoints
