"""Fresh native Powerwall charging that Tesla's own site limit can reduce.

This is measured charging, never a forecast or permission to discharge. It is
separate from PowerSync dispatch capacity, which must physically yield first.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

@dataclass(frozen=True)
class NativeBatteryFlexibility:
    watts: float
    proof: tuple


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def native_battery_flexibility(hass, entry, runtime, effective_cap_w, *, budget=None):
    """Return current measured flexibility only with a matching native cap.

    Normal polling reads signed config and DCQ together. A DCQ-only refresh
    with supplied config cannot renew ``config_observed_monotonic``. The proof
    binds the physical site, setup, client and cap across a queued EV request;
    the measured allowance itself is recomputed at the final write boundary.
    """
    from .powerwall_control import canonical_site_id, foreign_site_owner
    from .powerwall_local.battery_dispatch import site_grid_limits_w

    try:
        if hass.data.get('power_sync', {}).get(entry.entry_id) is not runtime:
            return None
        config = {**entry.data, **entry.options}
        site = canonical_site_id(config.get('tesla_energy_site_id') or config.get('powerwall_local_energy_site_id'))
        paired_site = canonical_site_id(config.get('powerwall_local_energy_site_id'))
        if (site != paired_site or config.get('powerwall_local_paired') is not True
                or not config.get('powerwall_local_din') or not config.get('powerwall_local_public_key_der')):
            return None
        local_data = runtime.get('powerwall_local') or {}
        local = local_data.get('coordinator')
        snapshot = getattr(local, 'data', None)
        client = getattr(local, 'client', None)
        control = getattr(getattr(client, '_transport', None), '_site_control', None)
        matches = getattr(client, '_control_matches_entry', None)
        if (snapshot is None or not getattr(local, 'last_update_success', False)
                or local_data.get('client') is not client or not callable(matches) or not matches()
                or control is None or runtime.get('powerwall_site_control') is not control
                or canonical_site_id(control.site_id) != site):
            return None
        now, monotonic = time.time(), time.monotonic()
        meter_at = getattr(local, 'last_success_ts', None)
        observed = getattr(snapshot, 'observed_monotonic', None)
        config_at = getattr(snapshot, 'config_observed_monotonic', None)
        if (not all(_finite(value) for value in (meter_at, observed, config_at, effective_cap_w))
                or not 0 <= now-meter_at <= 15 or not 0 <= monotonic-observed <= 15
                or not 0 <= monotonic-config_at <= 15 or abs(observed-config_at) > 5):
            return None
        native_cap, _ = site_grid_limits_w(snapshot)
        raw_config = snapshot.raw.get('config')
        info = raw_config.get('site_info', {}) if isinstance(raw_config, dict) else {}
        mode = raw_config.get('default_real_mode') or info.get('default_real_mode')
        if (not _finite(native_cap) or native_cap < 0 or native_cap > effective_cap_w
                or snapshot.grid_status != 'SystemGridConnected' or mode != 'autonomous'
                or snapshot.operation_mode != mode or not _finite(snapshot.battery_w)):
            return None
        # An unexpired deadline is not needed here: unresolved ownership after
        # expiry still blocks flexibility until its original cleanup completes.
        if foreign_site_owner(hass, None, site) is not None:
            return None
        if budget is not None and any(
                value.get('native_max') and canonical_site_id(value.get('site')) == site
                for value in budget.entries.values()):
            return None
        for candidate in hass.data.get('power_sync', {}).values():
            if not isinstance(candidate, dict):
                continue
            candidate_entry = candidate.get('entry')
            candidate_config = ({**candidate_entry.data, **candidate_entry.options}
                                if candidate_entry is not None else {})
            identities = {canonical_site_id(value) for value in (
                getattr(candidate.get('powerwall_site_control'), 'site_id', None),
                candidate_config.get('tesla_energy_site_id'), candidate_config.get('powerwall_local_energy_site_id'))
                if value is not None}
            if identities and site not in identities:
                continue
            if any(candidate.get(key) for key in (
                    'native_rate_battery_transitions', 'tesla_control_method_changing',
                    'powerwall_local_dispatch_stopping', 'max_backup_restore_attempts',
                    'tesla_settings_pending')):
                return None
            dispatch = candidate.get('powerwall_battery_dispatch')
            if dispatch is not None:
                committed = getattr(dispatch, 'prospective_charge_w', None)
                if committed is None:
                    last = getattr(dispatch, '_last_watts', None)
                    committed = max(0.0, -last) if _finite(last) else None
                if (not _finite(committed) or committed > 0 or getattr(dispatch, 'pending', False)
                        or getattr(dispatch, 'charge_generation', None) is not None):
                    return None
        proof = (site, id(runtime), id(local), id(client), id(control),
                 config.get('powerwall_local_din'), config.get('powerwall_local_public_key_der'),
                 native_cap, effective_cap_w, mode)
        return NativeBatteryFlexibility(max(0.0, -snapshot.battery_w), proof)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
