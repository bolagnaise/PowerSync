"""Regression tests for the generated Home Assistant dashboard."""

from __future__ import annotations

from pathlib import Path


STRATEGY_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "frontend"
    / "power-sync-strategy.js"
)
INIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)


def test_button_card_resource_fallback_accepts_dashed_hacs_url():
    """button-card HACS URLs include dashes and must not be normalized away."""
    source = STRATEGY_PATH.read_text()

    assert "c.element," in source
    assert "c.hacs," in source
    assert "c.element.replace(/-/g, '')" in source
    assert "c.hacs?.replace(/-/g, '')" in source
    assert "url.includes(name)" in source


def test_optimizer_windows_use_combined_visual_card():
    """Optimizer schedule should use the native API-backed dashboard card."""
    source = STRATEGY_PATH.read_text()

    assert "customElements.define('power-sync-optimization-plan'" in source
    assert "custom:power-sync-optimization-plan" in source
    assert "optimizationPath: 'power_sync/optimization'" in source
    assert "optimization_force_charge_windows" in source
    assert "optimization_force_discharge_windows" in source
    assert "lp_import_price_forecast" in source
    assert "lp_export_price_forecast" in source
    assert "Planned Battery Windows" in source
    assert "_batteryWindowsFromActions(actions, model)" in source
    assert "_socRangeForAction(action, model)" in source
    assert "24-Hour Action Plan" in source
    assert "_actionRangesFromApi()" in source
    assert "_fallbackActionRanges()" in source
    assert "_priceStatsForAction(action, model)" in source
    assert "price-kind" in source
    assert "avg min max" in source
    assert "Future Force Charge" not in source


def test_optimizer_plan_shows_calculated_auto_reserve():
    """Auto-applied optimizer reserve should be visible on the schedule graph."""
    source = STRATEGY_PATH.read_text()

    assert "_optimizerReserve(data)" in source
    assert "applied_optimizer_reserve_percent" in source
    assert "auto_apply_reserve_enabled" in source
    assert "Calculated Reserve" in source
    assert "Auto Reserve" in source
    assert "reserveCalculated" in source


def test_dashboard_battery_controls_include_self_consumption_action():
    """Manual battery controls should expose the self-consumption service."""
    source = STRATEGY_PATH.read_text()
    battery_controls = source[
        source.index("function _batteryControls(hass)"):
        source.index("function _teslaEnergySiteControls", source.index("function _batteryControls(hass)"))
    ]

    assert "name: 'Self Consumption'" in battery_controls
    assert "icon: 'mdi:home-battery'" in battery_controls
    assert "service: 'power_sync.set_self_consumption'" in battery_controls
    assert "Set battery to self-consumption mode?" in battery_controls


def test_dashboard_setup_preserves_user_managed_lovelace_layout():
    """Reloads must not overwrite a dashboard the user has edited manually."""
    source = INIT_PATH.read_text()

    assert "def _is_empty_lovelace_dashboard_config" in source
    assert "Initializing empty PowerSync dashboard with strategy mode" in source
    assert "already has a custom Lovelace layout; " in source
    assert "leaving it unchanged" in source
    assert "Migrating PowerSync dashboard to strategy mode" not in source


def test_dashboard_layout_storage_reconciles_card_changes():
    """Saved tile order should survive card additions/removals where possible."""
    source = STRATEGY_PATH.read_text()

    assert "_legacyCardKey(cardConfig, index)" in source
    assert "_cardKey(cardConfig, occurrence)" in source
    assert "legacyToCurrent" in source
    assert "missingItems" in source
    assert "layouts[layoutKey] = normalized" in source
    assert "return `${index}:${parts.join(':')}`" not in source


def test_dashboard_layout_drag_starts_from_handle_only():
    """Mobile scroll gestures should not be captured by the whole card."""
    source = STRATEGY_PATH.read_text()

    assert "const dragSurface = document.createElement('button');" in source
    assert "dragSurface.setAttribute('aria-label', 'Drag dashboard card')" in source
    assert "item.addEventListener('pointerdown'" not in source
    assert "item.addEventListener('pointermove'" not in source
    assert ".item.customizing {\n        cursor: default;" in source
    assert ".drag-surface {\n        display: none;" in source
    drag_surface_css = source[
        source.index(".drag-surface {"):source.index(".item.customizing .drag-surface")
    ]
    assert "touch-action: none;" in drag_surface_css
    assert "appearance: none;" in drag_surface_css


def test_dashboard_layout_can_hide_cards():
    """Customize mode should persist hidden dashboard cards and preview them safely."""
    source = STRATEGY_PATH.read_text()

    assert "this._hiddenStorageKey = 'power-sync-dashboard-hidden-v1';" in source
    assert "this._showingHidden = false;" in source
    assert "_loadHiddenKeys()" in source
    assert "_saveHiddenKeys(keys)" in source
    assert "_visibleItems()" in source
    assert "_layoutItems()" in source
    assert "_hideItem(item)" in source
    assert "_showHiddenItems()" in source
    assert "_unhideItem(item)" in source
    assert "_toggleItemHidden(item)" in source
    assert "const hideSurface = document.createElement('button');" in source
    assert "hideSurface.setAttribute('aria-label', 'Hide dashboard card')" in source
    assert "toolbar.querySelector('.restore-hidden').addEventListener('click', () => this._showHiddenItems())" in source
    show_hidden_method = source[
        source.index("  _showHiddenItems() {"):
        source.index("  _unhideItem(item) {")
    ]
    assert "this._showingHidden = !this._showingHidden;" in show_hidden_method
    assert "this._saveHiddenKeys([]);" not in show_hidden_method
    assert "Unhide dashboard card" in source
    assert ".item.hidden-preview" in source
    assert "const visibleItems = this._layoutItems();" in source


def test_battery_health_uses_native_dashboard_card():
    """Battery health should render with the native compact health card."""
    source = STRATEGY_PATH.read_text()

    assert "customElements.define('power-sync-battery-health', PowerSyncBatteryHealth)" in source
    assert "type: 'custom:power-sync-battery-health'" in source
    assert "Measured vs rated capacity" in source
    assert "Follower capacity is inferred from aggregate gateway data." in source
    assert "_packRows(attrs)" in source
    assert "battery_${index}_health_percent" in source
    assert "battery_${index}_original_kwh" in source
    assert "state_attr('${healthEntity}'" not in source
    assert "healthGauge('Overall'" not in source


def test_dashboard_entity_resolver_accepts_ha_renamed_powersync_sensors():
    """HA may compose PowerSync sensor IDs from the integration/device name."""
    source = STRATEGY_PATH.read_text()

    assert "sensor.powersync_amber_battery_level" in source
    assert "objectId.startsWith('powersync_')" in source
    assert "objectId.startsWith('power_sync_')" in source
    assert "isAvailableState(id)" in source


def test_tesla_controls_gate_uses_entity_resolver():
    """The dashboard must detect current power_sync_tesla_* operation controls."""
    source = STRATEGY_PATH.read_text()

    assert "findEntity('number', 'backup_reserve')" in source
    assert "findEntity('select', 'operation_mode')" in source
    assert "_s['select.power_sync_operation_mode']" not in source
    assert "_s['number.power_sync_backup_reserve']" not in source
