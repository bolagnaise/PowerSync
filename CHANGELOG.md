# Changelog

## [2.12.1344] - 2026-10-03

### Fixed

- Removed duplicate Home Assistant core dependencies from the integration manifest so Hassfest validation accepts the package.

## [2.12.1343] - 2026-10-03

### Fixed

- Kept Smart Schedule charging within configured site import limits, including deadline charging, and prevented explicit EV session limits from overriding lower site limits.
- Applied initial site-headroom checks to all Smart Schedule charger types and prevented optimizer battery reservations from blocking deadline charging.
