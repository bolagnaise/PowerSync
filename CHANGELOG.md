# Changelog

## [Unreleased]

### Fixed

- Kept Smart Schedule charging within configured site import limits, including deadline charging, and prevented explicit EV session limits from overriding lower site limits.
- Applied initial site-headroom checks to all Smart Schedule charger types and prevented optimizer battery reservations from blocking deadline charging.
