# PowerSync Copilot Instructions

## Build, test, and validation

- This repository does not define local `pytest`, lint, or package-manager script entry points.
- CI validation is defined in `.github/workflows/validate.yml` and currently consists of:
  - HACS validation via `hacs/action@main`
  - Hassfest validation via `home-assistant/actions/hassfest@master`
- There is no repo-defined single-test command because no automated test suite is checked in.
- Releases are driven by version bumps in `custom_components/power_sync/manifest.json`; `.github/workflows/release.yml` watches that file.

## High-level architecture

- `custom_components/power_sync/__init__.py` is the integration hub. It owns config-entry setup/unload, chooses the active battery/provider path, stores runtime objects in `hass.data[DOMAIN][entry_id]`, registers services and HTTP views for the mobile app, and auto-registers the Lovelace frontend resources/dashboard.
- `custom_components/power_sync/config_flow.py` is a large multi-step flow that branches first by electricity provider and then by battery system. Reauth is provider-specific too, especially for Tesla token sources.
- `custom_components/power_sync/coordinator.py` contains the main update coordinators and persistence helpers such as the energy accumulator. Provider/battery adapters live in the top-level `*_api.py`, `inverters/`, and `powerwall_local/` modules.
- `custom_components/power_sync/optimization/` is the built-in smart-optimization stack: forecast collection, LP optimization, EV coordination, and schedule execution.
- `custom_components/power_sync/automations/` is a custom automation engine stored with Home Assistant `Store`; it is separate from native HA YAML/UI automations.
- `custom_components/power_sync/frontend/` plus `HA Dashboard/README.md` define the generated PowerSync dashboard. The dashboard is strategy-based and renders sections by detecting which PowerSync entities exist, rather than relying on a static card layout.
- `custom_components/power_sync/powerwall_local/` handles direct local Powerwall communication, including PW2 REST auth and PW3 pairing/signing flows.

## Key conventions

- Follow the integration's `entry.options.get(..., entry.data.get(...))` pattern. Mutable user settings usually live in `entry.options`; credentials and initial identifiers usually live in `entry.data`.
- Runtime state is shared through `hass.data[DOMAIN][entry.entry_id]`. New coordinators, capability flags, automation stores, and dashboard-facing caches should be wired there consistently.
- Entity identity is `entry.entry_id`-based. Unique IDs are typically `f"{entry.entry_id}_{key}"`, and entity IDs should prefer `power_sync_*` object IDs. Preserve the existing migration behavior for legacy unprefixed entity IDs instead of creating duplicates.
- Dashboard/frontend changes are coupled to startup timing. `async_setup()` intentionally waits until Home Assistant has started before adding Lovelace resources or the auto-created dashboard; moving that earlier can wipe existing Lovelace resources.
- The dashboard strategy supports both modern `power_sync_` entity prefixes and legacy bare names. Keep that backward-compatibility path intact when renaming entities.
- If you add or rename a service, update both `custom_components/power_sync/services.yaml` and the imperative `hass.services.async_register(...)` calls in `custom_components/power_sync/__init__.py`.
- `custom_components/power_sync/const.py` treats `manifest.json` as the version source of truth. `DASHBOARD_JS_VERSION` is a separate cache-busting switch for frontend resources.
- Be careful with logs. `coordinator.py` includes a `SensitiveDataFilter` that masks tokens, site IDs, VINs, and similar identifiers; avoid adding raw sensitive values to new logs.
- Release behavior spans multiple files: hand-written `RELEASE_NOTES.md` is preferred by `.github/workflows/release.yml`, and that workflow clears the file after publishing. Keep the workflow and release-note expectations aligned when changing release flow.
