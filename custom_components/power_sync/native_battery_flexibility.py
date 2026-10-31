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


def native_max_coexistence_proof(hass, entry, runtime, site, budget):
    """Return stable identity for a currently authoritative native Max grant.

    ``ev_coexistence`` in the durable budget is a capability.  The grant is
    usable only while its live session, adapter, local site sample, and
    ownership fences all agree.  Fresh telemetry is checked here but is not
    embedded in the returned identity, so an ordinary newer sample does not
    revoke an otherwise unchanged proof.
    """
    try:
        from .native_rate_budget import native_max_ev_coexistence
        from .powerwall_control import canonical_site_id, foreign_site_owner
        from .powerwall_local.battery_dispatch import site_grid_limits_w

        if budget is None or hass.data.get('power_sync', {}).get(entry.entry_id) is not runtime:
            return None
        maximum = budget.native_max_claim(site) if budget is not None else None
        session = runtime.get('powerwall_native_max_charge')
        adapter = getattr(session, 'adapter', None)
        if (not isinstance(maximum, dict) or not native_max_ev_coexistence(maximum)
                or maximum.get('entry_id') != entry.entry_id
                or maximum.get('direction', 'charge') not in ('charge', 'discharge')
                or not isinstance(maximum.get('token'), str) or not maximum['token']
                or not _finite(maximum.get('expires_at'))
                or time.time() >= maximum['expires_at']
                or session is None or adapter is None):
            return None
        direction, token = maximum.get('direction', 'charge'), maximum['token']
        current_identity = adapter.current_identity() if callable(getattr(adapter, 'current_identity', None)) else None
        identity = getattr(adapter, 'identity', None)
        if (not identity or current_identity != identity
                or getattr(adapter, 'entry_id', None) != entry.entry_id
                or canonical_site_id(getattr(adapter, 'site', None)) != canonical_site_id(site)
                or getattr(adapter, 'token', None) != token
                or getattr(adapter, 'direction', None) != direction
                or getattr(adapter, 'ev_coexistence', None) is not True
                or not callable(getattr(adapter, 'authority', None))
                or adapter.authority() is not True):
            return None
        status = session.status() if callable(getattr(session, 'status', None)) else None
        record = getattr(session, 'record', None)
        if (not isinstance(status, dict) or status.get('phase') != 'active'
                or status.get('active') is not True or status.get('direction') != direction
                or status.get('id') != token or not _finite(status.get('expires_at'))
                or time.time() >= status['expires_at']
                or not isinstance(record, dict) or record.get('id') != token
                or record.get('phase') != 'active'
                or not _finite(record.get('expires_at'))
                or time.time() >= record['expires_at']
                or not isinstance(record.get('baseline'), dict)
                or record['baseline'].get('direction') != direction):
            return None
        if getattr(session, '_clearing', False):
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
                or canonical_site_id(getattr(control, 'site_id', None)) != canonical_site_id(site)):
            return None
        # A second current runtime claiming the same physical site is a
        # foreign owner until its control registration is gone.  A durable
        # capability flag cannot resolve that ambiguity.
        for candidate in hass.data.get('power_sync', {}).values():
            if candidate is runtime or not isinstance(candidate, dict):
                continue
            candidate_entry = candidate.get('entry')
            candidate_control = candidate.get('powerwall_site_control')
            candidate_config = ({**getattr(candidate_entry, 'data', {}),
                                 **getattr(candidate_entry, 'options', {})}
                                if candidate_entry is not None else {})
            candidate_sites = {
                canonical_site_id(value) for value in (
                    getattr(candidate_control, 'site_id', None),
                    candidate_config.get('tesla_energy_site_id'),
                    candidate_config.get('powerwall_local_energy_site_id'))
                if value is not None
            }
            if canonical_site_id(site) in candidate_sites:
                return None
        now, monotonic = time.time(), time.monotonic()
        meter_at = getattr(local, 'last_success_ts', None)
        observed = getattr(snapshot, 'observed_monotonic', None)
        config_at = getattr(snapshot, 'config_observed_monotonic', None)
        if (not all(_finite(value) for value in (meter_at, observed, config_at))
                or not 0 <= now - meter_at <= 15
                or not 0 <= monotonic - observed <= 15
                or not 0 <= monotonic - config_at <= 15
                or abs(observed - config_at) > 5):
            return None
        if (not all(_finite(getattr(snapshot, key, None)) for key in ('grid_w', 'load_w', 'solar_w', 'battery_w'))
                or getattr(snapshot, 'grid_status', None) != 'SystemGridConnected'
                or getattr(snapshot, 'operation_mode', None) != 'autonomous'):
            return None
        native_limits = site_grid_limits_w(snapshot)
        hardware_limit = native_limits[0] if direction == 'charge' else native_limits[1]
        if not _finite(hardware_limit) or hardware_limit < 0:
            return None
        config = {**getattr(entry, 'data', {}), **getattr(entry, 'options', {})}
        software_key = ('optimization_max_grid_import_w' if direction == 'charge'
                        else 'optimization_max_grid_export_w')
        software_limit = config.get(software_key)
        if software_limit in (None, 0):
            software_limit = math.inf
        if (not _finite(software_limit) and software_limit != math.inf
                or software_limit != math.inf and hardware_limit > software_limit):
            return None
        for key in ('native_max_startup_pending', 'native_max_recovery_unavailable',
                    'pending_tesla_restore', 'optimizer_force_restart_restore_pending',
                    'powerwall_local_recovery_pending', 'powerwall_settings_pending',
                    'tesla_settings_pending', 'powerwall_settings_recovering',
                    'powerwall_dispatch_fallback_pending',
                    'max_backup_restore_attempts', 'max_backup_recovery_unavailable'):
            if runtime.get(key):
                return None
        if (runtime.get('max_backup_local_event')
                or any(runtime.get(key) is not None for key in (
                    'max_backup_end_ts', 'max_backup_saved_reserve',
                    'max_backup_schedule'))):
            return None
        if any((runtime.get(key) or {}).get('active') or (runtime.get(key) or {}).get('pending')
               for key in ('hold_soc_state', 'self_consumption_state')):
            return None
        native_state = runtime.get('force_charge_state' if direction == 'charge' else 'force_discharge_state') or {}
        if native_state and (native_state.get('active') or native_state.get('pending')) and not (
                native_state.get('native_tariff_force') is True
                and native_state.get('power_target_basis') == 'native_tariff'
                and native_state.get('command_status') == 'accepted'):
            return None
        other_state = runtime.get('force_discharge_state' if direction == 'charge' else 'force_charge_state') or {}
        if other_state.get('active') or other_state.get('pending'):
            return None
        if any(runtime.get(key) for key in (
                'native_rate_battery_transitions', 'tesla_control_method_changing',
                'powerwall_local_dispatch_stopping')):
            return None
        dispatch = runtime.get('powerwall_battery_dispatch')
        if dispatch is not None:
            committed = getattr(dispatch, 'prospective_charge_w', None)
            if committed is None:
                last = getattr(dispatch, '_last_watts', None)
                committed = max(0.0, -last) if _finite(last) else None
            if (not _finite(committed) or committed > 0
                    or getattr(dispatch, 'pending', False)
                    or getattr(dispatch, 'charge_generation', None) is not None):
                return None
        if foreign_site_owner(hass, entry.entry_id, site) is not None:
            return None
        return ('native_max_coexistence', canonical_site_id(site), entry.entry_id,
                direction, token, identity, current_identity, canonical_site_id(getattr(adapter, 'site', None)),
                tuple(config.get(key) for key in ('powerwall_local_energy_site_id',
                                                   'tesla_energy_site_id', 'powerwall_local_din',
                                                   'powerwall_local_public_key_der')))
    except Exception:
        return None


def _active_native_charge_claim(hass, entry, runtime, site, budget):
    proof = native_max_coexistence_proof(hass, entry, runtime, site, budget)
    return proof is not None and proof[3] == 'charge'


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
        native_proof = native_max_coexistence_proof(hass, entry, runtime, site, budget)
        own_native_charge = native_proof is not None and native_proof[3] == 'charge'
        if foreign_site_owner(hass, entry.entry_id, site) is not None:
            return None
        if budget is not None and any(
                value.get('native_max') and canonical_site_id(value.get('site')) == site
                and not own_native_charge for value in budget.entries.values()):
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
            same_runtime = candidate is runtime or (
                candidate_entry is not None
                and getattr(candidate_entry, 'entry_id', None) == entry.entry_id
            )
            native = candidate.get('powerwall_native_max_charge')
            if (getattr(native, 'pending', False)
                    and not (same_runtime and own_native_charge)):
                return None
            if same_runtime:
                for key in (
                        'force_charge_state', 'force_discharge_state', 'hold_soc_state',
                        'self_consumption_state'):
                    state = candidate.get(key)
                    if isinstance(state, dict):
                        if not (state.get('active') or state.get('pending')):
                            continue
                        if (key == 'force_charge_state' and own_native_charge
                                and state.get('native_max_charge') is True
                                and state.get('native_tariff_force') is True
                                and state.get('power_target_basis') == 'native_tariff'
                                and state.get('command_status') == 'accepted'):
                            continue
                    if state:
                        return None
                if any(candidate.get(key) for key in (
                        'powerwall_settings_pending',
                        'tesla_settings_pending', 'pending_tesla_restore',
                        'powerwall_settings_recovering', 'max_backup_recovery_unavailable')):
                    return None
                if (candidate.get('max_backup_local_event')
                        or any(key in candidate and candidate[key] is not None for key in (
                            'max_backup_end_ts', 'max_backup_saved_reserve', 'max_backup_schedule'))):
                    return None
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
                 native_cap, effective_cap_w, mode, native_proof)
        return NativeBatteryFlexibility(max(0.0, -snapshot.battery_w), proof)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
