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


def test_optimizer_action_plan_renders_full_scrollable_list():
    """The 24-hour action plan should expose every action instead of hiding overflow."""
    source = STRATEGY_PATH.read_text()
    actions_css = source[
        source.index("        .actions {"):
        source.index("        .action-row {")
    ]

    assert "overflow-y: auto;" in actions_css
    assert "max-height: min(58vh, 620px);" in actions_css
    assert "scrollbar-gutter: stable;" in actions_css
    assert "overscroll-behavior: contain;" not in actions_css
    assert "actions.map(action =>" in source
    assert "actions.slice(0, 10)" not in source
    assert "more actions" not in source


def test_optimizer_plan_gets_are_browser_cached():
    """Frequent dashboard updates must not hammer the optimization API."""
    source = STRATEGY_PATH.read_text()

    assert "OPTIMIZATION_PLAN_FETCH_INTERVAL_MS = 45000" in source
    assert "OPTIMIZATION_PLAN_PENDING_RETRY_MS = 5000" in source
    assert "window.__powerSyncOptimizationPlanCache" in source
    assert "_restoreCachedData(path, force)" in source
    assert "cached?.promise" in source
    assert "_adoptLoadPromise(path, cached.promise)" in source
    assert "this._hass.callApi('GET', path)" in source
    assert "if (force) return false;" in source
    assert "? OPTIMIZATION_PLAN_FETCH_INTERVAL_MS" in source
    assert ": OPTIMIZATION_PLAN_PENDING_RETRY_MS" in source
    assert "now - this._lastFetch < maxAge" in source
    assert "now - this._lastFetch < 60000" not in source


def test_optimizer_plan_does_not_rerender_on_unrelated_state_ticks():
    """Cached optimizer charts should not rebuild for unrelated HA state updates."""
    source = STRATEGY_PATH.read_text()
    plan_start = source.index("class PowerSyncOptimizationPlan extends HTMLElement")
    hass_setter = source[
        source.index("  set hass(hass) {", plan_start):
        source.index("  connectedCallback()", plan_start)
    ]
    render_signature = source[
        source.index("  _renderSignature() {", plan_start):
        source.index("  _render() {", plan_start)
    ]

    assert "this._scheduleRenderIfChanged();" in hass_setter
    assert "this._scheduleRender();" not in hass_setter
    assert "new ResizeObserver(() => this._scheduleRenderIfChanged())" in source[plan_start:]
    assert "this._lastRenderSignature = this._renderSignature();" in source[plan_start:]
    assert "priceMeta" in render_signature
    assert "forceCharge: this._entityStateSignature(this._config?.forceChargeEntity, ['windows'])" in render_signature
    assert "forceDischarge: this._entityStateSignature(this._config?.forceDischargeEntity, ['windows'])" in render_signature


def test_dashboard_ev_panel_is_registered_and_api_cached():
    """HA dashboard EV controls should use the native API-backed panel."""
    source = STRATEGY_PATH.read_text()
    panel_start = source.index("class PowerSyncEVPanel extends HTMLElement")
    hass_setter = source[
        source.index("  set hass(hass) {", panel_start):
        source.index("  connectedCallback()", panel_start)
    ]
    render_signature = source[
        source.index("  _renderSignature() {", panel_start):
        source.index("  _loadpoints() {", panel_start)
    ]

    assert "customElements.define('power-sync-ev-panel'" in source
    assert "custom:power-sync-ev-panel" in source
    assert "function _evPanel()" in source
    assert "center.push(_evPanel());" in source
    assert "cards.some(card => card?.type === 'custom:power-sync-ev-panel')" in source
    assert "cards.push(_evPanel());" in source
    assert "EV_PANEL_FETCH_INTERVAL_MS = 30000" in source
    assert "window.__powerSyncEVPanelCache" in source
    assert "window.setInterval(() => this._maybeLoadData(false), EV_PANEL_FETCH_INTERVAL_MS)" in source
    assert "this._hass.callApi('GET', 'power_sync/ev/loadpoints/status')" in source
    assert "power_sync/ev/solar_surplus_config" in source
    assert "power_sync/ev/price_level_charging/settings" in source
    assert "power_sync/ev/scheduled_charging/settings" in source
    assert "power_sync/ev/auto_schedule/status" in source
    assert "power_sync/ev/auto_schedule/toggle" in source
    assert "power_sync/ev/boost" in source
    assert "start_policy_charging" in source
    assert "this._scheduleRenderIfChanged();" in hass_setter
    assert "this._scheduleRender();" not in hass_setter
    assert "data: this._data" in render_signature
    assert "policy: this._policy" in render_signature


def test_ev_panel_hides_zero_amps_while_charging_without_amp_telemetry():
    """Active chargers without current telemetry should not show a misleading 0 A."""
    source = STRATEGY_PATH.read_text()

    assert "this._amps(loadpoint.current_amps, loadpoint)" in source
    assert "const power = Number(loadpoint?.current_power_kw);" in source
    assert "if (Number.isFinite(power) && power > 0.05) return '--';" in source
    assert "return Number.isFinite(number) ? '0 A' : '--';" in source


def test_generic_dashboard_charts_do_not_rerender_on_unrelated_state_ticks():
    """Dashboard graph tooltips and legend buttons should survive unrelated HA ticks."""
    source = STRATEGY_PATH.read_text()
    chart_start = source.index("class PowerSyncChart extends HTMLElement")
    chart_end = source.index("if (!customElements.get('power-sync-chart'))")
    chart_source = source[chart_start:chart_end]
    hass_setter = chart_source[
        chart_source.index("  set hass(hass) {"):
        chart_source.index("  getCardSize() {")
    ]
    render_signature = chart_source[
        chart_source.index("  _renderSignature() {"):
        chart_source.index("  _chartEntitySignature(")
    ]

    assert "this._scheduleRenderIfChanged();" in hass_setter
    assert "this._scheduleRender();" not in hass_setter
    assert "new ResizeObserver(() => this._scheduleRenderIfChanged())" in chart_source
    assert "this._lastRenderSignature = this._renderSignature();" in chart_source
    assert "clockBucket" in render_signature
    assert "hiddenSeries: Array.from(this._hiddenSeries).sort()" in render_signature
    assert "state: this._chartEntitySignature(mode, seriesConfig, config)" in render_signature
    assert "cache: mode === 'history' ? this._historyCacheSignature(seriesConfig.entity) : undefined" in render_signature


def test_dashboard_chart_tooltips_stay_visibly_transparent():
    """Tooltip surfaces should not blur the graph into an opaque panel."""
    source = STRATEGY_PATH.read_text()
    tooltip_css = source[source.index("        .tooltip {"):source.index("        .tooltip-time {")]
    chart_tooltip_css = source[
        source.index("        .chart-tooltip {"):
        source.index("        .chart-tooltip-time {")
    ]

    assert "rgba(var(--rgb-card-background-color, 255, 255, 255), 0.22)" in tooltip_css
    assert "rgba(var(--rgb-card-background-color, 255, 255, 255), 0.22)" in chart_tooltip_css
    assert "backdrop-filter" not in tooltip_css
    assert "backdrop-filter" not in chart_tooltip_css


def test_generic_dashboard_chart_tooltips_render_above_svg_lines():
    """Generic dashboard chart tooltips should sit above graph paths."""
    source = STRATEGY_PATH.read_text()
    chart_start = source.index("class PowerSyncChart extends HTMLElement")
    chart_end = source.index("if (!customElements.get('power-sync-chart'))")
    chart_source = source[chart_start:chart_end]

    assert "isolation: isolate;" in chart_source
    assert ".tooltip-line" in chart_source
    assert ".tooltip" in chart_source
    assert "z-index: 2;" in chart_source
    assert "z-index: 4;" in chart_source
    assert "background: rgba(var(--rgb-card-background-color, 255, 255, 255), 0.22);" in chart_source
    assert "backdrop-filter: blur(10px) saturate(1.15);" not in chart_source


def test_optimizer_plan_shows_calculated_auto_reserve():
    """Auto-applied optimizer reserve should be visible on the schedule graph."""
    source = STRATEGY_PATH.read_text()

    assert "_optimizerReserve(data)" in source
    assert "applied_optimizer_reserve_percent" in source
    assert "auto_apply_reserve_enabled" in source
    assert "Calculated Reserve" in source
    assert "Auto Reserve" in source
    assert "home_load_export_floor_percent" in source
    assert "applied_export_reserve_floor_percent" in source
    assert "Export Floor" in source
    assert "reserveCalculated" in source
    assert "exportReserveCalculated" in source


def test_optimizer_plan_shows_temporary_idle_hold_separately():
    """Temporary hardware hold should not be graphed as the optimizer reserve."""
    source = STRATEGY_PATH.read_text()

    assert "_idleHoldReserve(data)" in source
    assert "idle_hold_active" in source
    assert "idle_hold_reserve_percent" in source
    assert "IDLE Hold" in source
    assert "idleHoldReservePercent" in source
    assert "holding SOC at" in source


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


def test_dashboard_manual_battery_controls_show_active_mode_countdown():
    """Manual battery controls should highlight active modes from Battery Mode."""
    source = STRATEGY_PATH.read_text()
    battery_controls = source[
        source.index("function _batteryControls(hass)"):
        source.index("function _teslaEnergySiteControls", source.index("function _batteryControls(hass)"))
    ]

    assert "const batteryModeEntity = 'sensor.power_sync_battery_mode'" in battery_controls
    assert "entity: batteryModeEntity" in battery_controls
    assert "triggers_update: [batteryModeEntity]" in battery_controls
    assert "modeState.attributes?.remaining_minutes" in battery_controls
    assert "Math.max(0, Math.ceil(remaining)) + ' min'" in battery_controls
    assert "return '${label} active'" in battery_controls

    for mode in (
        "force_charge",
        "force_discharge",
        "hold_soc",
        "self_consumption",
    ):
        assert mode in battery_controls

    assert "name: activeModeName('force_charge', 'Charge')" in battery_controls
    assert "name: activeModeName('force_discharge', 'Discharge')" in battery_controls
    assert "name: activeModeName('hold_soc', 'Hold SoC')" in battery_controls
    self_consumption = battery_controls[
        battery_controls.index("name: 'Self Consumption'"):
        battery_controls.index("service: 'power_sync.set_self_consumption'")
    ]
    assert "activeModeName('self_consumption'" not in self_consumption
    assert "states['${batteryModeEntity}']?.state === 'self_consumption'" in self_consumption


def test_dashboard_setup_preserves_user_managed_lovelace_layout():
    """Reloads must not overwrite a dashboard the user has edited manually."""
    source = INIT_PATH.read_text()

    assert "def _is_empty_lovelace_dashboard_config" in source
    assert "Initializing empty PowerSync dashboard with strategy mode" in source
    assert "already has a custom Lovelace layout; " in source
    assert "leaving it unchanged" in source
    assert "Migrating PowerSync dashboard to strategy mode" not in source


def test_dashboard_uses_power_sync_ev_power_attributes_for_presence():
    """Sigenergy idle-plugged chargers should show as present in the flow card."""
    source = STRATEGY_PATH.read_text()

    assert "const evPowerAttrs = hass.states[evPower]?.attributes || {};" in source
    assert "Object.prototype.hasOwnProperty.call(evPowerAttrs, 'is_connected')" in source
    assert "config.entities.ev_presence = evPower;" in source


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


def test_dashboard_layout_does_not_rebalance_on_every_state_tick():
    """HA state updates should not run the expensive masonry layout loop."""
    source = STRATEGY_PATH.read_text()
    layout_source = source[source.index("class PowerSyncLayout extends HTMLElement {"):]
    hass_setter = source[
        source.index("  set hass(hass) {", source.index("class PowerSyncLayout extends HTMLElement {")):
        source.index(
            "  disconnectedCallback()",
            source.index("  set hass(hass) {", source.index("class PowerSyncLayout extends HTMLElement {")),
        )
    ]
    resize_scheduler = layout_source[
        layout_source.index("  _scheduleLayoutForResize(entry) {"):
        layout_source.index("  _flattenCards()", layout_source.index("  _scheduleLayoutForResize(entry) {"))
    ]

    assert "for (const c of this._cards) c.hass = hass;" in hass_setter
    assert "this._scheduleLayout();" not in hass_setter
    assert "_scheduleLayoutForResize(entries?.[0])" in source
    assert "widthDelta < 80" in resize_scheduler
    assert "columnCount === this._lastLayoutColumnCount" in resize_scheduler
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


def test_optimizer_plan_charts_have_tooltips():
    """Optimizer SOC/power and price charts should expose hover tooltips."""
    source = STRATEGY_PATH.read_text()
    chart_start = source.index("class PowerSyncOptimizationPlan extends HTMLElement")
    chart_end = source.index("if (!customElements.get('power-sync-optimization-plan'))")
    chart_source = source[chart_start:chart_end]

    assert "class PowerSyncOptimizationPlan extends HTMLElement" in source
    assert '<div class="chart-wrap soc-power-chart">' in chart_source
    assert '<div class="chart-wrap price-chart">' in chart_source
    assert "_attachOptimizerChartTooltip('.soc-power-chart', this._powerTooltipConfig(model, compact))" in chart_source
    assert "_attachOptimizerChartTooltip('.price-chart', this._priceTooltipConfig(model, compact, priceMeta))" in chart_source
    assert "_powerTooltipConfig(model, compact)" in chart_source
    assert "_priceTooltipConfig(model, compact, priceMeta)" in chart_source
    assert ".chart-tooltip-line" in chart_source
    assert ".chart-tooltip-time" in chart_source
    assert "background: rgba(var(--rgb-card-background-color, 255, 255, 255), 0.22);" in chart_source
    assert "backdrop-filter: blur(10px) saturate(1.15);" not in chart_source


def test_optimizer_plan_chart_tooltips_stay_inside_android_webview():
    """Android HA webview can render taller tooltip text, so keep it in bounds."""
    source = STRATEGY_PATH.read_text()

    assert "const tooltipBottom = Math.max(34, rect.height - chart.pad.bottom - 8);" in source
    assert "tooltip.offsetHeight && tooltipBottom - tooltip.offsetHeight < 8" in source
    assert "tooltip.style.transform = 'translate(-50%, 0)';" in source
    assert "tooltip.style.top = '8px';" in source
    assert 'x="${pad.left - 4}"' in source


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


def test_dashboard_adds_provider_pricing_cards_as_hideable_sections():
    """Provider account sensors should render as normal layout cards."""
    source = STRATEGY_PATH.read_text()

    assert "findProviderSensor = (provider, suffixOrSuffixes)" in source
    assert "hasProviderSensor('globird', ['latest_data_status', 'latest_day_cost', 'balance'])" in source
    assert "'power_sync_service_'" in source
    assert "const globirdCard = _globirdProvider(findProviderSensor);" in source
    assert "const flowPowerCard = _flowPower(e, hasE);" in source
    assert "left.push(flowPowerCard)" in source
    assert "left.push(globirdCard)" in source


def test_dashboard_provider_cards_include_portal_account_metrics():
    """Dashboard provider cards should expose the imported portal metrics."""
    source = STRATEGY_PATH.read_text()

    assert "title: 'GloBird Pricing'" in source
    assert "['latest_day_cost', 'Latest Day Cost']" in source
    assert "['zerohero_status', 'ZeroHero Status']" in source
    assert "['billing_period_cost', 'Billing Period Cost']" in source
    assert "title: 'Flow Power Pricing'" in source
    assert "['fp_account_pea', 'Portal PEA']" in source
    assert "['fp_account_lwap', 'Portal LWAP']" in source
    assert "['fp_account_avg_usage', 'Average Demand']" in source
    assert "['fp_account_max_usage', 'Max Demand']" in source
