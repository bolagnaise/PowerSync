"""Regression tests for the generated Home Assistant dashboard."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess


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


def test_powerwall_local_card_uses_domain_aware_entity_resolution():
    """V1R/DCQ card must resolve binary sensors and switches, not sensor IDs."""
    source = STRATEGY_PATH.read_text()

    assert "const powerwallLocalPaired = findEntity('binary_sensor', 'powerwall_local_paired');" in source
    assert "hass.states[powerwallLocalPaired]?.state === 'on'" in source
    assert "_powerwallLocalControl(e, hasE, findEntity)" in source
    assert "function _powerwallLocalControl(e, hasE, findEntity)" in source

    start = source.index("function _powerwallLocalControl")
    end = source.index("function _powerwallHealth", start)
    helper_source = source[start:end]
    runtime = r"""
      const sensors = new Set([
        'pw_system_island_state', 'pw_count', 'pw_active_alerts',
        'pw_v1r_device', 'pw_v1r_firmware', 'pw_v1r_network', 'pw_v1r_internet',
      ]);
      const e = name => `sensor.power_sync_${name}`;
      const hasE = name => sensors.has(name);
      const findEntity = (domain, suffix) => `${domain}.power_sync_${suffix}`;
      const card = _powerwallLocalControl(e, hasE, findEntity);
      const status = card.cards[0].entities.map(row => row.entity);
      if (!status.includes('binary_sensor.power_sync_powerwall_local_paired')) {
        throw new Error('paired binary sensor is not wired');
      }
      if (!status.includes('binary_sensor.power_sync_powerwall_local_islanded')) {
        throw new Error('islanded binary sensor is not wired');
      }
      if (!status.includes('binary_sensor.power_sync_pw_critical_alert')) {
        throw new Error('critical alert binary sensor is not wired');
      }
      if (!status.includes('binary_sensor.power_sync_calibration_active')) {
        throw new Error('calibration binary sensor is not wired');
      }
      if (!status.includes('sensor.power_sync_pw_v1r_device')) {
        throw new Error('V1R device sensor is not wired');
      }
      const controls = card.cards[1].card.entities.map(row => row.entity);
      if (!controls.includes('switch.power_sync_on_grid')) {
        throw new Error('on-grid switch is not wired');
      }
      if (!controls.includes('switch.power_sync_off_grid')) {
        throw new Error('off-grid switch is not wired');
      }
    """
    subprocess.run(
        ["node", "-e", f"{helper_source}\n{runtime}"],
        check=True,
        capture_output=True,
        text=True,
    )


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


def test_optimizer_plan_labels_next_day_ranges_as_tomorrow():
    """A rolling 24-hour plan must not make tomorrow's slot look active today."""
    source = STRATEGY_PATH.read_text()

    assert "this._formatPlanTime(data.next_action_time)" in source
    assert "const anchor = this._data?.schedule?.timestamps?.[0];" in source
    assert "if (dayOffset === 1) return `Tomorrow ${time}`;" in source
    assert "const startText = this._formatPlanTime(start);" in source
    assert "startOffset === endOffset" in source

    assert "18:05 - Tomorrow 17:30" == _render_optimizer_time_range(
        source,
        "2026-08-07T18:05:00+10:00",
        "2026-08-08T17:30:00+10:00",
    )
    assert "Tomorrow 17:30 - 17:50" == _render_optimizer_time_range(
        source,
        "2026-08-08T17:30:00+10:00",
        "2026-08-08T17:50:00+10:00",
    )


def _render_optimizer_time_range(source: str, start: str, end: str) -> str:
    method_names = (
        "_formatTime",
        "_formatOptimizerAxisTime",
        "_planDayOffset",
        "_formatPlanTime",
        "_timeRange",
    )
    methods = []
    for index, name in enumerate(method_names):
        next_name = method_names[index + 1] if index + 1 < len(method_names) else "_clockMinutes"
        match = re.search(
            rf"  {name}\((?P<args>[^)]*)\) \{{(?P<body>.*?)\n  \}}\n\n  {next_name}\(",
            source,
            re.DOTALL,
        )
        assert match is not None
        methods.append(f"{name}({match.group('args')}) {{{match.group('body')}\n  }}")

    runtime = f"""
      const card = {{
        _data: {{schedule: {{timestamps: ['2026-08-07T18:05:00+10:00']}}}},
        {','.join(methods)}
      }};
      process.stdout.write(card._timeRange({start!r}, {end!r}));
    """
    result = subprocess.run(
        ["node", "-e", runtime],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_optimizer_chart_axes_use_scoped_twelve_hour_wall_clock_labels():
    source = STRATEGY_PATH.read_text()
    match = re.search(
        r"  _formatOptimizerAxisTime\((?P<args>[^)]*)\) \{(?P<body>.*?)\n  \}\n\n  _planDayOffset\(",
        source,
        re.DOTALL,
    )
    assert match is not None
    method = f"function axis({match.group('args')}) {{{match.group('body')}\n  }}"
    runtime = """
      const values = [
        '2026-08-17T00:00:00+10:00',
        '2026-08-17T12:00:00+10:00',
        '2026-08-17T13:05:00+10:00',
        '2026-04-05T02:30:00+11:00',
        '2026-04-05T02:30:00+10:00',
        '2026-08-17T24:00:00+10:00',
      ];
      process.stdout.write(JSON.stringify(values.map(axis)));
    """
    result = subprocess.run(
        ["node", "-e", f"{method}\n{runtime}"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == '["12:00 AM","12:00 PM","1:05 PM","2:30 AM","2:30 AM","--:--"]'
    assert "this._formatOptimizerAxisTime(points[i]?.timestamp)" in source
    assert "this._escHtml(this._formatTime(point.timestamp))" in source


def test_dashboard_ai_explanation_is_explicit_safe_and_plan_isolated():
    source = STRATEGY_PATH.read_text()
    start = source.index("class PowerSyncAIPlanExplanation extends HTMLElement")
    end = source.index("const EV_PANEL_FETCH_INTERVAL_MS")
    card_source = source[start:end]

    assert "customElements.define('power-sync-ai-plan-explanation'" in card_source
    assert "custom:power-sync-ai-plan-explanation" in source
    assert "center.push(_optimizationPlan(e));" in source
    assert "center.push(_aiPlanExplanation());" in source
    assert "this._hass.callApi('GET', this._optimizationPath())" in card_source
    assert "this._hass.callApi('POST', this._summaryPath(), { refresh: !!refresh })" in card_source
    assert card_source.count("callApi('POST'") == 1
    assert "generate.addEventListener('click', () => this._generate(false))" in card_source
    assert "refresh.addEventListener('click', () => this._generate(true))" in card_source
    assert "this._expanded = false;" in card_source
    assert "_toggleExpanded()" in card_source
    assert "aria-expanded" in card_source
    assert "Minimize AI explanation" in card_source
    assert "Expand AI explanation" in card_source
    assert "body.hidden = !showDetails" in card_source
    assert "actions.hidden = !showDetails" in card_source
    assert "toggle.addEventListener('click', () => this._toggleExpanded())" in card_source
    assert "text.textContent = value" in card_source
    assert "item.textContent = value" in card_source
    assert "summary.important_actions" in card_source
    assert ": summary.action_explanations" in card_source
    assert "item.window_id" not in card_source
    assert "toLocaleTimeString" in card_source
    assert "provider_auth_failed" in card_source
    assert "Showing the previous explanation." in card_source
    assert "Descriptive only. AI cannot change or execute the optimizer plan." in card_source
    decision_labels = [
        "'Right now'",
        "'Next'",
        "'Why this plan'",
        "'Expected outcome'",
        "'Optional timeline'",
        "'What may change'",
    ]
    positions = [card_source.index(label) for label in decision_labels]
    assert positions == sorted(positions)
    assert "setInterval" not in card_source
    assert "localStorage" not in card_source


def test_lp_battery_power_chart_splits_home_consumption_from_export():
    """LP battery forecast should show where discharge is going."""
    source = STRATEGY_PATH.read_text()
    chart_source = source[
        source.index("function _lpBatteryPowerChart"):
        source.index("function _curtailmentStatus")
    ]

    assert "attribute: 'charge_values_kw'" in chart_source
    assert "attribute: 'home_consumption_values_kw'" in chart_source
    assert "name: 'Powering Home'" in chart_source
    assert "attribute: 'export_values_kw'" in chart_source
    assert "name: 'Export'" in chart_source
    assert "attribute: 'discharge_values_kw'" not in chart_source


def test_lp_solar_chart_uses_explicit_segmented_curtailment_provenance():
    source = STRATEGY_PATH.read_text()
    chart_source = source[
        source.index("class PowerSyncChart extends HTMLElement"):
        source.index("if (!customElements.get('power-sync-chart'))")
    ]
    config_source = source[
        source.index("function _lpSolarLoadChart"):
        source.index("function _lpPriceChart")
    ]

    assert "raw_forecast_values_kw" in config_source
    assert "planned_forecast_values_kw" in config_source
    assert "fallbackAttribute: 'forecast_values_kw'" in config_source
    assert "deltaAttribute: 'curtailment_values_kw'" in config_source
    assert "name: 'System Curtailment'" in config_source
    assert "strokeDasharray: '6,4'" in config_source
    assert "value > 1e-6" in chart_source
    assert "segments.push(current)" in chart_source
    assert "point.upper * yMultiplier" in chart_source
    assert "raw_weather" not in chart_source


def test_lp_solar_chart_renders_only_evidence_backed_curtailment_bands():
    source = STRATEGY_PATH.read_text()
    chart_start = source.index("class PowerSyncChart extends HTMLElement")
    chart_end = source.index("if (!customElements.get('power-sync-chart'))")
    chart_source = source[chart_start:chart_end]
    prelude = """
      global.HTMLElement = class {
        attachShadow() {
          this.shadowRoot = {
            innerHTML: '',
            querySelectorAll() { return []; },
            querySelector() { return null; },
          };
        }
        getBoundingClientRect() { return { width: 640 }; }
      };
    """
    runtime = r"""
      const baseConfig = {
        mode: 'forecast',
        intervalMinutes: 5,
        yUnit: 'kW',
        yMin: 0,
        series: [
          { key: 'raw', entity: 'sensor.solar', attribute: 'raw_forecast_values_kw', name: 'Raw Solar', color: '#aaa', strokeDasharray: '6,4' },
          { key: 'planned', entity: 'sensor.solar', attribute: 'planned_forecast_values_kw', fallbackAttribute: 'forecast_values_kw', name: 'Planned Solar', color: '#ffd700' },
        ],
        bands: [{ key: 'curtail', entity: 'sensor.solar', lowerAttribute: 'planned_forecast_values_kw', lowerFallbackAttribute: 'forecast_values_kw', deltaAttribute: 'curtailment_values_kw', name: 'System Curtailment', color: '#607d8b' }],
      };
      function render(attributes) {
        const chart = new PowerSyncChart();
        chart._config = baseConfig;
        chart._hass = { states: { 'sensor.solar': { state: '1', attributes } } };
        chart._render();
        return chart.shadowRoot.innerHTML;
      }
      const legacy = render({ forecast_values_kw: [4, 3, 2] });
      if (legacy.includes('System Curtailment') || !legacy.includes('Planned Solar')) {
        throw new Error('legacy fallback should render without a curtailment claim');
      }
      const nowcastOnly = render({
        forecast_values_kw: [4, 3, 2],
        raw_forecast_values_kw: [4, 4, 2],
        planned_forecast_values_kw: [4, 3, 2],
        curtailment_values_kw: [0, 0, 0],
      });
      if (nowcastOnly.includes('System Curtailment') || !nowcastOnly.includes('stroke-dasharray="6,4"')) {
        throw new Error('nowcast gap should show two lines but no curtailment band');
      }
      const explicit = render({
        forecast_values_kw: [4, 2, 2, 1, 1],
        raw_forecast_values_kw: [4, 4, 2, 2, 1],
        planned_forecast_values_kw: [4, 1.5, 2, 0.5, 1],
        curtailment_values_kw: [0, 1.5, 0, 0.5, 0],
      });
      if (!explicit.includes('System Curtailment')) throw new Error('explicit band label missing');
      const paths = [...explicit.matchAll(/data-band="curtail"/g)];
      if (paths.length !== 2) throw new Error(`expected two segmented bands, got ${paths.length}`);
      const invalid = render({
        forecast_values_kw: [4, 3],
        raw_forecast_values_kw: [4, 4],
        planned_forecast_values_kw: [4, 3],
        curtailment_values_kw: [1],
      });
      if (invalid.includes('System Curtailment')) throw new Error('misaligned evidence must fail closed');
    """
    subprocess.run(["node", "-e", f"{prelude}\n{chart_source}\n{runtime}"], check=True)


def test_dashboard_uses_tariff_schedule_instead_of_duplicate_price_history():
    """Dynamic tariffs should not show a current-state history chart beside the schedule."""
    source = STRATEGY_PATH.read_text()
    price_layout = source[
        source.index("    // --- Right Column: Price History"):
        source.index("    // --- Center Column: LP Forecast Summary")
    ]

    assert "if (hasE('current_import_price') && !hasE('tariff_schedule'))" in price_layout
    assert "right.push(_priceChart(e, hass));" in price_layout
    assert "if (hasE('tariff_schedule'))" in price_layout
    assert "right.push(_touSchedule(e, hass));" in price_layout
    assert "title: 'Current Price History - Today'" in source
    assert "title: 'Electricity Prices - 24 Hours'" not in source


def test_daily_cost_card_distinguishes_amber_metered_cost_from_estimate():
    """Today's partial Amber bill data should not replace the live estimate."""
    source = STRATEGY_PATH.read_text()

    assert "hasEntityE('daily_import_cost') || hasE('amber_usage_today_cost')" in source
    assert "Amber Metered Cost Today (Partial)" in source
    assert "Estimated Import Cost Today" in source


def test_import_price_gauge_shows_effective_price_source():
    """The live import gauge should identify KWatch and fallback sources."""
    source = STRATEGY_PATH.read_text()
    gauge_source = source[
        source.index("function _svgArcGaugeCard"):
        source.index("function _batteryControls")
    ]

    assert "attrs.price_source" in gauge_source
    assert "flow_power_kwatch: 'KWatch'" in gauge_source
    assert "flow_power_kwatch_fallback_aemo: 'AEMO fallback'" in gauge_source
    assert "attrs.using_price_fallback" in gauge_source
    assert gauge_source.count("showPriceSource: true") == 1


def test_dashboard_summarizes_short_gap_battery_windows():
    """Planned battery windows should not list every tiny LP charge island."""
    source = STRATEGY_PATH.read_text()

    assert "BATTERY_WINDOW_MERGE_GAP_MINUTES = 15" in source
    assert "return this._mergeBatteryWindowRanges(windows, model);" in source
    assert "previous.action === window.action" in source
    assert "gapMinutes <= BATTERY_WINDOW_MERGE_GAP_MINUTES" in source
    assert "active / ${this._formatDuration(window.spanDurationMinutes)} span" in source
    assert "_priceStatsForSegments(previous.segments, previous.action, model)" in source


def test_optimizer_battery_windows_show_integrated_energy_and_value():
    """Window energy/value should use active interval data, not peak power times span."""
    source = STRATEGY_PATH.read_text()

    assert "_energyValueForSegments(segments, action, model)" in source
    assert "model.detailedSchedule ? 'exportKw' : 'dischargeKw'" in source
    assert "Math.min(intervalMs, ranges.reduce" in source
    assert "intervalEnergyKwh = powerKw * overlapMs / 3600000" in source
    assert "value += intervalEnergyKwh * minorPrice / 100" in source
    assert "this._energyValueForSegments(previous.segments, previous.action, model)" in source
    assert "this._renderWindowImpact(window.energyValue, window.action, priceMeta)" in source
    assert "action === 'charge' ? 'Est. cost' : 'Est. earnings'" in source
    assert "window.power_w * window.durationMinutes" not in source


def test_optimizer_charge_window_cost_counts_only_grid_sourced_battery_energy():
    """Concurrent forecast solar must not be priced as paid battery energy."""
    source = STRATEGY_PATH.read_text()
    method = re.search(
        r"  _energyValueForSegments\(segments, action, model\) \{"
        r"(?P<body>.*?)\n  \}\n\n"
        r"  _actionRangesFromApi\(\)",
        source,
        re.DOTALL,
    )
    assert method is not None
    build_method = re.search(
        r"  _buildModel\(\) \{(?P<body>.*?)\n  \}\n\n"
        r"  _renderChips\(model, priceMeta\)",
        source,
        re.DOTALL,
    )
    assert build_method is not None

    runtime = f"""
      const energyValueForSegments = function(segments, action, model) {{{method.group("body")}}};
      const buildModel = function() {{{build_method.group("body")}}};
      const start = '2026-07-29T13:05:00+10:00';
      const end = '2026-07-29T13:10:00+10:00';
      const makeModel = (gridImport) => {{
        const schedule = {{
          timestamps: [start],
          soc: [0.3],
          charge_w: [10000],
          discharge_w: [0],
          battery_consume_w: [0],
          battery_export_w: [0],
          import_price: [0.186],
          export_price: [0],
        }};
        if (gridImport !== undefined) schedule.grid_import_w = gridImport;
        return buildModel.call({{
          _data: {{ schedule }},
          _intervalMinutes: () => 5,
          _percent: value => Number(value) * 100,
          _kw: value => Number(value) / 1000,
          _minorPrice: value => Number(value) * 100,
          _optimizerReserve: () => ({{
            percent: null,
            calculated: false,
            exportPercent: null,
            exportCalculated: false,
          }}),
          _idleHoldReserve: () => ({{ active: false, percent: null }}),
          _demandRanges: () => [],
        }});
      }};
      const result = energyValueForSegments.call(
        {{}},
        [{{ timestamp: start, end_time: end }}],
        'charge',
        makeModel([6500]),
      );
      const expectedEnergy = 6.5 * 5 / 60;
      const expectedCost = expectedEnergy * 0.186;
      const fallback = energyValueForSegments.call(
        {{}},
        [{{ timestamp: start, end_time: end }}],
        'charge',
        makeModel(undefined),
      );
      const invalidSlot = energyValueForSegments.call(
        {{}},
        [{{ timestamp: start, end_time: end }}],
        'charge',
        makeModel([null]),
      );
      const sparseGrid = [];
      sparseGrid.length = 1;
      const sparseSlot = energyValueForSegments.call(
        {{}},
        [{{ timestamp: start, end_time: end }}],
        'charge',
        makeModel(sparseGrid),
      );
      const shortArray = energyValueForSegments.call(
        {{}},
        [{{ timestamp: start, end_time: end }}],
        'charge',
        makeModel([]),
      );
      const zeroGrid = energyValueForSegments.call(
        {{}},
        [{{ timestamp: start, end_time: end }}],
        'charge',
        makeModel([0]),
      );
      const blankSlot = energyValueForSegments.call(
        {{}},
        [{{ timestamp: start, end_time: end }}],
        'charge',
        makeModel(['']),
      );
      const invalidValue = energyValueForSegments.call(
        {{}},
        [{{ timestamp: start, end_time: end }}],
        'charge',
        makeModel(['unavailable']),
      );
      const fallbackEnergy = 10 * 5 / 60;
      const fallbackCost = fallbackEnergy * 0.186;
      if (
        !result
        || Math.abs(result.energyKwh - expectedEnergy) > 1e-9
        || Math.abs(result.value - expectedCost) > 1e-9
        || !fallback
        || Math.abs(fallback.energyKwh - fallbackEnergy) > 1e-9
        || Math.abs(fallback.value - fallbackCost) > 1e-9
        || !invalidSlot
        || Math.abs(invalidSlot.energyKwh - fallbackEnergy) > 1e-9
        || Math.abs(invalidSlot.value - fallbackCost) > 1e-9
        || !sparseSlot
        || Math.abs(sparseSlot.energyKwh - fallbackEnergy) > 1e-9
        || Math.abs(sparseSlot.value - fallbackCost) > 1e-9
        || !shortArray
        || Math.abs(shortArray.energyKwh - fallbackEnergy) > 1e-9
        || Math.abs(shortArray.value - fallbackCost) > 1e-9
        || zeroGrid !== null
        || !blankSlot
        || Math.abs(blankSlot.energyKwh - fallbackEnergy) > 1e-9
        || Math.abs(blankSlot.value - fallbackCost) > 1e-9
        || !invalidValue
        || Math.abs(invalidValue.energyKwh - fallbackEnergy) > 1e-9
        || Math.abs(invalidValue.value - fallbackCost) > 1e-9
      ) {{
        throw new Error(
          `charge cost attribution failed: ${{JSON.stringify({{
            result,
            fallback,
            invalidSlot,
            sparseSlot,
            shortArray,
            zeroGrid,
            blankSlot,
            invalidValue,
          }})}}`
        );
      }}
    """
    subprocess.run(["node", "-e", runtime], check=True)


def test_optimizer_action_plan_renders_full_scrollable_list():
    """The 24-hour action plan should expose every action instead of hiding overflow."""
    source = STRATEGY_PATH.read_text()
    actions_css = source[
        source.index("        .actions {"):
        source.index("        .action-row {")
    ]
    action_ranges_source = source[
        source.index("  _actionRangesFromApi() {"):
        source.index("  _fallbackActionRanges() {")
    ]

    assert "overflow-y: auto;" in actions_css
    assert "max-height: min(58vh, 620px);" in actions_css
    assert "scrollbar-gutter: stable;" in actions_css
    assert "overscroll-behavior: contain;" not in actions_css
    assert "actions.map(action =>" in source
    assert "actions.slice(0, 10)" not in source
    assert ".slice(0, 24)" not in action_ranges_source
    assert "more actions" not in source


def test_optimizer_action_plan_keeps_every_backend_24h_range():
    """A busy 24-hour plan must retain actions beyond the 24th range."""
    source = STRATEGY_PATH.read_text()
    method = re.search(
        r"  _actionRangesFromApi\(\) \{(?P<body>.*?)\n  \}\n\n"
        r"  _fallbackActionRanges\(\)",
        source,
        re.DOTALL,
    )
    assert method is not None

    runtime = f"""
      const actionRangesFromApi = function() {{{method.group("body")}}};
      const actions = Array.from({{ length: 26 }}, (_, index) => ({{
        action: index === 25 ? 'charge' : 'self_consumption',
        timestamp: `2026-07-${{String(24 + Math.floor(index / 24)).padStart(2, '0')}}`
          + `T${{String(index % 24).padStart(2, '0')}}:00:00+10:00`,
      }}));
      const result = actionRangesFromApi.call({{ _data: {{ next_actions: actions }} }});
      if (result.length !== 26 || result[25]?.action !== 'charge') {{
        throw new Error('the 25th and later 24-hour action ranges were truncated');
      }}
    """
    subprocess.run(["node", "-e", runtime], check=True)


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
    assert "power_sync/ev/auto_schedule/settings" in source
    assert "power_sync/ev/auto_schedule/toggle" in source
    assert "power_sync/ev/vehicle_config" in source
    assert "power_sync/ev/boost" in source
    assert "start_policy_charging" in source
    assert "this._scheduleRenderIfChanged();" in hass_setter
    assert "this._scheduleRender();" not in hass_setter
    assert "data: this._data" in render_signature
    assert "policy: this._policy" in render_signature
    assert "modesExpanded: this._modesExpanded" in render_signature


def test_dashboard_ev_modes_are_collapsed_with_enabled_summary():
    """Mode settings should roll up without hiding which automations are enabled."""
    source = STRATEGY_PATH.read_text()
    panel_source = source[
        source.index("class PowerSyncEVPanel extends HTMLElement"):
        source.index("customElements.define('power-sync-ev-panel'")
    ]

    assert "this._modesExpanded = false;" in panel_source
    assert 'data-action="toggle-modes"' in panel_source
    assert 'aria-expanded="${expanded ? \'true\' : \'false\'}"' in panel_source
    assert 'aria-controls="ev-mode-grid"' in panel_source
    assert 'id="ev-mode-grid" ${expanded ? \'\' : \'hidden\'}' in panel_source
    assert "this._modesExpanded = !this._modesExpanded;" in panel_source
    assert "if (grid) grid.hidden = !this._modesExpanded;" in panel_source
    assert "toggle?.setAttribute('aria-expanded'" in panel_source
    assert "this._scheduleRender();" not in panel_source[
        panel_source.index("  _toggleModes() {"):
        panel_source.index("  async _startPolicy()")
    ]
    assert "new CustomEvent('power-sync-card-resize', { bubbles: true, composed: true })" in panel_source
    assert ".modes-toggle:focus-visible" in panel_source
    assert "enabledModes.push('Solar Surplus')" in panel_source
    assert "enabledModes.push('Price Level')" in panel_source
    assert "enabledModes.push('Scheduled')" in panel_source
    assert "enabledModes.push('Smart Schedule')" in panel_source
    assert "return 'All off';" in panel_source

    toggle_method = re.search(
        r"  _toggleModes\(\) \{(?P<body>.*?)\n  \}\n\n"
        r"  async _startPolicy\(\)",
        panel_source,
        re.DOTALL,
    )
    assert toggle_method is not None
    runtime = f"""
      const attrs = {{}};
      const iconAttrs = {{}};
      const icon = {{setAttribute: (key, value) => {{ iconAttrs[key] = value; }}}};
      const toggle = {{
        setAttribute: (key, value) => {{ attrs[key] = value; }},
        querySelector: () => icon,
      }};
      const grid = {{hidden: true}};
      let resizeEvent = null;
      global.CustomEvent = class {{
        constructor(type, options) {{ this.type = type; this.options = options; }}
      }};
      const card = {{
        _modesExpanded: false,
        _lastRenderSignature: '',
        shadowRoot: {{querySelector: (selector) => selector === '#ev-mode-grid' ? grid : toggle}},
        _renderSignature: () => 'expanded-signature',
        dispatchEvent: (event) => {{ resizeEvent = event; }},
        toggle() {{{toggle_method.group('body')}\n  }},
      }};
      card.toggle();
      if (!card._modesExpanded || grid.hidden || attrs['aria-expanded'] !== 'true') throw new Error('expand failed');
      if (iconAttrs.icon !== 'mdi:chevron-up') throw new Error('expand icon failed');
      if (resizeEvent?.type !== 'power-sync-card-resize' || !resizeEvent.options?.composed) throw new Error('resize event failed');
      card.toggle();
      if (card._modesExpanded || !grid.hidden || attrs['aria-expanded'] !== 'false') throw new Error('collapse failed');
      if (iconAttrs.icon !== 'mdi:chevron-down') throw new Error('collapse icon failed');
    """
    subprocess.run(["node", "-e", runtime], check=True)


def test_dashboard_ev_panel_renders_live_solar_delay_countdown():
    """The HA loadpoint summary should match the mobile start/stop delay status."""
    source = STRATEGY_PATH.read_text()
    panel_source = source[
        source.index("class PowerSyncEVPanel extends HTMLElement"):
        source.index("customElements.define('power-sync-ev-panel'")
    ]

    assert "loadpoint?.delay_timer" in panel_source
    assert 'data-delay-status' in panel_source
    assert 'data-delay-remaining' in panel_source
    assert "timer?.remaining_seconds" in panel_source
    assert "timer?.started_at" in panel_source
    assert "timer?.duration_seconds" in panel_source
    assert "this._data?.fetchedAt" in panel_source
    assert "window.setInterval(update, 1000)" in panel_source
    assert "window.clearInterval(this._delayTimerTick)" in panel_source
    assert "status.setAttribute('aria-label'" in panel_source

    remaining = re.search(
        r"  _delayRemainingSeconds\(timer\) \{(?P<body>.*?)\n  \}\n\n"
        r"  _formatDelayRemaining\(seconds\)",
        panel_source,
        re.DOTALL,
    )
    assert remaining is not None
    formatter = re.search(
        r"  _formatDelayRemaining\(seconds\) \{(?P<body>.*?)\n  \}\n\n"
        r"  _syncDelayTimerTick\(\)",
        panel_source,
        re.DOTALL,
    )
    assert formatter is not None
    runtime = f"""
      const format = function(seconds) {{{formatter.group('body')}\n  }};
      const card = {{
        _data: {{fetchedAt: '2026-08-12T00:00:29.000Z'}},
        remaining(timer) {{{remaining.group('body')}\n  }}
      }};
      Date.now = () => Date.parse('2026-08-12T00:00:29.000Z');
      const seconds = card.remaining({{
        started_at: '2026-08-12T00:00:00.000Z',
        duration_seconds: 300,
        remaining_seconds: 999,
      }});
      const actual = [seconds, format(seconds), format(31), format(0)];
      process.stdout.write(JSON.stringify(actual));
    """
    result = subprocess.run(
        ["node", "-e", runtime],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == '[271,"4m 31s remaining","31s remaining","Ending now"]'


def test_dashboard_ev_capacity_editor_uses_vehicle_config_api():
    """Each Smart Schedule row should expose effective capacity and a clearable override."""
    source = STRATEGY_PATH.read_text()

    assert "Usable capacity ${this._escHtml(effectiveCapacity ?? 60)} kWh" in source
    assert "data-capacity-input" in source
    assert "data-capacity-save" in source
    assert "data-capacity-clear" in source
    assert "battery_capacity_kwh: value" in source
    assert "value < 1 || value > 250" in source


def test_dashboard_ev_departure_editor_uses_auto_schedule_settings_api():
    """Each Smart Schedule row should expose editable per-day departure times."""
    source = STRATEGY_PATH.read_text()

    assert "const dayLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];" in source
    assert "data-departure-time" in source
    assert "data-departure-day" in source
    assert "data-departure-save" in source
    assert "data-departure-clear" in source
    assert "departure_times: departureTimes" in source
    assert "EV_PANEL_PATHS.autoSettings" in source
    assert ": (vehicle.display_name || vehicleId);" in source
    assert "`Vehicle ${index + 1}`" not in source
    assert "grid-column: 1 / -1;" in source
    assert "repeat(auto-fit, minmax(145px, 1fr))" in source
    assert 'class="smart-details"' in source
    assert '</button>\n          <div class="departure-editor">' in source


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


def test_energy_history_overlays_battery_soc_on_a_secondary_axis():
    """The power-flow history should pair kW flows with a readable SOC percentage."""
    source = STRATEGY_PATH.read_text()
    chart_source = source[
        source.index("class PowerSyncChart extends HTMLElement"):
        source.index("if (!customElements.get('power-sync-chart'))")
    ]
    energy_chart = source[
        source.index("function _combinedEnergyChart"):
        source.index("function _demandCharge")
    ]

    assert "_combinedEnergyChart(e, hasE('home_load'), hasE('battery_level'))" in source
    assert "axis: 'right'" in energy_chart
    assert "entity: e('battery_level')" in energy_chart
    assert "name: 'Battery SOC'" in energy_chart
    assert "rightYUnit: '%'" in energy_chart
    assert "rightYMin: 0" in energy_chart
    assert "rightYMax: 100" in energy_chart
    assert "const rightAxisSeries = chartSeries.filter" in chart_source
    assert "const primaryAxisSeries = chartSeries.filter" in chart_source
    assert "const seriesYScale = series.axis === 'right' ? rightYScale : yScale;" in chart_source
    assert 'text-anchor="start"' in chart_source

    prelude = """
      global.HTMLElement = class {
        attachShadow() {
          this.shadowRoot = {
            innerHTML: '',
            querySelectorAll() { return []; },
            querySelector() { return null; },
          };
        }
        getBoundingClientRect() { return { width: this.testWidth || 640 }; }
      };
    """

    runtime = r"""
      const now = Date.now();
      const states = {
        'sensor.solar': { state: '4.5', last_updated: new Date(now).toISOString() },
        'sensor.grid': { state: '-1.5', last_updated: new Date(now).toISOString() },
        'sensor.battery': { state: '2.0', last_updated: new Date(now).toISOString() },
        'sensor.home': { state: '5.0', last_updated: new Date(now).toISOString() },
        'sensor.battery_soc': { state: '55', last_updated: new Date(now).toISOString() },
      };
      const config = {
        mode: 'history',
        historyHours: 24,
        yUnit: 'kW',
        rightYUnit: '%',
        rightYUnitCompact: true,
        rightYMin: 0,
        rightYMax: 100,
        zeroBaseline: true,
        series: [
          { entity: 'sensor.solar', name: 'Solar', color: '#ffd700' },
          { entity: 'sensor.grid', name: 'Grid', color: '#f44336' },
          { entity: 'sensor.battery', name: 'Battery', color: '#2196f3' },
          { entity: 'sensor.home', name: 'Home', color: '#9c27b0' },
          {
            entity: 'sensor.battery_soc',
            name: 'Battery SOC',
            color: '#009688',
            axis: 'right',
            minValue: 0,
            maxValue: 100,
          },
        ],
      };

      function rendered(width, { hideSoc = false, missingSoc = false } = {}) {
        const chart = new PowerSyncChart();
        chart.testWidth = width;
        chart._config = config;
        chart._hass = { states: { ...states } };
        const points = (a, b) => [[now - 3600000, a], [now, b]];
        chart._historyCache.set('sensor.solar', points(1, 4.5));
        chart._historyCache.set('sensor.grid', points(-2, -1.5));
        chart._historyCache.set('sensor.battery', points(3, 2));
        chart._historyCache.set('sensor.home', points(2, 5));
        if (missingSoc) {
          chart._hass.states['sensor.battery_soc'] = { state: 'unknown' };
        } else {
          chart._historyCache.set('sensor.battery_soc', points(40, 55));
        }
        if (hideSoc) chart._hiddenSeries.add('sensor.battery_soc');
        chart._render();
        return chart.shadowRoot.innerHTML;
      }

      function rightAxisLabels(html) {
        return [...html.matchAll(
          /<text x="([0-9.]+)"[^>]*text-anchor="start"[^>]*>([^<]+)<\/text>/g
        )];
      }

      const narrow = rendered(360);
      if (!narrow.includes('Battery SOC') || !narrow.includes('100%')) {
        throw new Error('SOC series or fixed percentage axis missing');
      }
      const rightLabels = rightAxisLabels(narrow);
      if (!rightLabels.some(([, x, label]) => Number(x) >= 0 && Number(x) <= 330 && label === '100%')) {
        throw new Error('right-axis labels clip outside the narrow chart');
      }

      const chart = new PowerSyncChart();
      if (chart._seriesDisplayValue({ axis: 'right' }, 55, 1, 1, config) !== '55.0%') {
        throw new Error('SOC tooltip/legend value has the wrong unit');
      }
      if (chart._seriesDisplayValue({}, 5, 1, 1, config) !== '5.00 kW') {
        throw new Error('power tooltip/legend value lost its kW unit');
      }
      if (rightAxisLabels(rendered(360, { hideSoc: true })).length !== 0) {
        throw new Error('hidden SOC still reserves or renders the right axis');
      }
      if (rightAxisLabels(rendered(360, { missingSoc: true })).length !== 0) {
        throw new Error('missing SOC still reserves or renders the right axis');
      }
    """
    subprocess.run(["node", "-e", f"{prelude}\n{chart_source}\n{runtime}"], check=True)


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


def test_optimizer_status_shows_active_charge_by_time_target_read_only():
    """Active Charge By Time settings should be visible without opening config."""
    source = STRATEGY_PATH.read_text()
    optimizer_status = source[
        source.index("function _optimizerStatus"):
        source.index("function _loadIncludesGenericEv")
    ]

    assert "current.attributes?.charge_by_time_enabled" in optimizer_status
    assert "current.attributes?.charge_by_time_target_soc" in optimizer_status
    assert "current.attributes?.charge_by_time_target_time" in optimizer_status
    assert "Charge By Time" in optimizer_status


def test_dashboard_battery_controls_include_self_consumption_action():
    """Manual battery controls should expose the self-consumption service."""
    source = STRATEGY_PATH.read_text()
    battery_controls = source[
        source.index("function _batteryControls(hass)"):
        source.index("function _teslaEnergySiteControls", source.index("function _batteryControls(hass)"))
    ]

    assert (
        "name: activeModeName('self_consumption', 'Self Consumption', "
        "'select.power_sync_force_discharge_duration')"
    ) in battery_controls
    assert "icon: 'mdi:home-battery'" in battery_controls
    assert "service: 'power_sync.set_self_consumption'" in battery_controls
    assert "Set battery to self-consumption mode for ' + dur + ' min?'" in battery_controls
    assert "select.power_sync_force_discharge_duration" in battery_controls
    assert "durationName('select.power_sync_force_discharge_duration', 'Discharge/Hold/Self')" in battery_controls
    assert "durationName('select.power_sync_force_charge_duration', 'Charge timer')" in battery_controls


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
    assert (
        "name: activeModeName('hold_soc', 'Hold SoC', "
        "'select.power_sync_force_discharge_duration')"
    ) in battery_controls
    assert (
        "name: activeModeName('self_consumption', 'Self Consumption', "
        "'select.power_sync_force_discharge_duration')"
    ) in battery_controls
    self_consumption = battery_controls[
        battery_controls.index("name: activeModeName('self_consumption', 'Self Consumption'"):
        battery_controls.index("service: 'power_sync.set_self_consumption'")
    ]
    assert "states['${batteryModeEntity}']?.state === 'self_consumption'" in self_consumption


def test_dashboard_hold_and_self_consumption_share_discharge_duration_label():
    """Manual hold/self-consumption controls should visibly use the discharge timer."""
    source = STRATEGY_PATH.read_text()
    battery_controls = source[
        source.index("function _batteryControls(hass)"):
        source.index("function _teslaEnergySiteControls", source.index("function _batteryControls(hass)"))
    ]

    assert "const durationName = (entity, label, fallback = '30')" in battery_controls
    assert "columns: 2" in battery_controls
    assert "Discharge/Hold/Self" in battery_controls
    assert "Self Consumption', 'select.power_sync_force_discharge_duration'" in battery_controls
    assert "Hold SoC', 'select.power_sync_force_discharge_duration'" in battery_controls
    assert "states['select.power_sync_force_discharge_duration'] ? states['select.power_sync_force_discharge_duration'].state : '30'" in battery_controls
    assert "?? '60'" not in battery_controls


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


def test_dashboard_marks_generic_charger_load_as_including_ev_draw():
    """Home Load basis wins over the legacy generic-charger inference."""
    source = STRATEGY_PATH.read_text()
    helper = re.search(
        r"function _loadIncludesGenericEv\([^)]*\) \{.*?\n\}",
        source,
        re.DOTALL,
    )
    assert helper is not None
    assert "config.load_includes_ev = true;" in source

    checks = """
      const cases = [
        [{ vehicle_id: 'generic_ev' }, true],
        [{ charger_type: 'generic' }, true],
        [{ vehicle_id: 'tesla_5YJ3', charger_type: 'tesla' }, false],
        [{ charger_type: 'teslemetry' }, false],
        [{}, false],
      ];
      for (const [attrs, expected] of cases) {
        const actual = _loadIncludesGenericEv(attrs);
        if (actual !== expected) throw new Error(`${JSON.stringify(attrs)}: ${actual}`);
      }
    """
    subprocess.run(["node", "-e", f"{helper.group(0)}\n{checks}"], check=True)

    basis_helper = re.search(
        r"function _loadIncludesEv\([^)]*\) \{.*?\n\}",
        source,
        re.DOTALL,
    )
    assert basis_helper is not None
    assert "const homeLoadAttrs = hass.states[config.entities.load_power]?.attributes || {};" in source
    assert "_loadIncludesEv(homeLoadAttrs, evPowerAttrs)" in source

    basis_checks = """
      const generic = { vehicle_id: 'generic_ev' };
      const cases = [
        [{ home_load_basis: 'excludes_ev' }, generic, false],
        [{ home_load_basis: 'includes_ev' }, generic, true],
        [{ home_load_basis: 'unknown' }, generic, true],
        [{}, generic, true],
        [{ home_load_basis: 'excludes_ev' }, { charger_type: 'tesla' }, false],
        [{}, { charger_type: 'tesla' }, false],
      ];
      for (const [homeLoad, ev, expected] of cases) {
        const actual = _loadIncludesEv(homeLoad, ev);
        if (actual !== expected) throw new Error(`${JSON.stringify(homeLoad)}/${JSON.stringify(ev)}: ${actual}`);
      }
    """
    subprocess.run(
        ["node", "-e", f"{helper.group(0)}\n{basis_helper.group(0)}\n{basis_checks}"],
        check=True,
    )


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
            "  connectedCallback()",
            source.index("  set hass(hass) {", source.index("class PowerSyncLayout extends HTMLElement {")),
        )
    ]
    resize_scheduler = layout_source[
        layout_source.index("  _scheduleLayoutForResize(entry) {"):
        layout_source.index("  _flattenCards()", layout_source.index("  _scheduleLayoutForResize(entry) {"))
    ]

    assert "for (const c of this._cards) c.hass = hass;" in hass_setter
    assert "this._scheduleLayout();" not in hass_setter
    assert "const entry = entries?.[0];" in source
    assert "this._scheduleLayoutForResize(entry)" in source
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


def test_dashboard_layout_locks_stable_height_across_card_refreshes():
    """Transient card rerenders must not collapse the iOS dashboard scroll range."""
    source = STRATEGY_PATH.read_text()
    layout_source = source[source.index("class PowerSyncLayout extends HTMLElement {"):]

    assert "this._maxObservedHeight = 0;" in layout_source
    assert "_updateHeightLock(height)" in layout_source
    assert "this.style.minHeight = `${nextHeight}px`;" in layout_source
    assert "_resetHeightLock()" in layout_source
    assert "this.style.minHeight = '';" in layout_source
    assert "this._updateHeightLock(entry?.contentRect?.height)" in layout_source
    assert "connectedCallback()" in layout_source
    assert "this._observeLayout();" in layout_source
    assert "this.addEventListener('power-sync-card-resize', this._cardResizeHandler);" in layout_source
    assert "this.removeEventListener('power-sync-card-resize', this._cardResizeHandler);" in layout_source

    card_resize = layout_source[
        layout_source.index("  _handleCardResize() {"):
        layout_source.index("  _scheduleLayout() {")
    ]
    assert "this._resetHeightLock();" in card_resize
    assert "this._scheduleLayout();" in card_resize
    assert "this._updateHeightLock(this.getBoundingClientRect().height)" in card_resize

    resize_scheduler = layout_source[
        layout_source.index("  _scheduleLayoutForResize(entry) {"):
        layout_source.index("  _flattenCards()", layout_source.index("  _scheduleLayoutForResize(entry) {"))
    ]
    assert "Math.max(1, this._layoutItems().length)" in resize_scheduler
    assert "columnCount !== this._lastLayoutColumnCount || widthDelta >= 80" in resize_scheduler
    assert "this._resetHeightLock();" in resize_scheduler

    for method_name in (
        "_resetOrder",
        "_hideItem",
        "_showHiddenItems",
        "_unhideItem",
        "_dropPointerReorder",
    ):
        method_start = layout_source.index(f"  {method_name}(")
        method_end = layout_source.index("\n  }", method_start)
        assert "this._resetHeightLock();" in layout_source[method_start:method_end]

    set_customizing_start = layout_source.index("  _setCustomizing(")
    set_customizing_end = layout_source.index("\n  }", set_customizing_start)
    set_customizing_source = layout_source[set_customizing_start:set_customizing_end]
    assert "if (wasShowingHidden) this._resetHeightLock();" in set_customizing_source


def test_dashboard_height_lock_runtime_lifecycle_and_geometry_changes():
    """The height lock should survive refreshes and reset on real geometry changes."""
    source = STRATEGY_PATH.read_text()
    class_start = source.index("class PowerSyncLayout extends HTMLElement {")
    class_end = source.index("\nif (!customElements.get('power-sync-layout'))", class_start)
    class_source = source[class_start:class_end]
    prelude = """
      let observerCount = 0;
      let disconnectCount = 0;
      class FakeResizeObserver {
        constructor(callback) { this.callback = callback; observerCount += 1; }
        observe(target) { this.target = target; }
        disconnect() { disconnectCount += 1; }
      }
      global.HTMLElement = class {
        constructor() {
          this.style = { minHeight: '' };
          this._rect = { width: 390, height: 1200 };
        }
        attachShadow() { return {}; }
        getBoundingClientRect() { return this._rect; }
        addEventListener() {}
        removeEventListener() {}
      };
      global.ResizeObserver = FakeResizeObserver;
      global.window = {
        innerWidth: 390,
        ResizeObserver: FakeResizeObserver,
        matchMedia: () => ({ matches: true }),
      };
      global.requestAnimationFrame = () => 1;
    """
    checks = """
      const assert = (condition, message) => { if (!condition) throw new Error(message); };

      const layout = new PowerSyncLayout();
      layout._built = true;
      layout._updateHeightLock(1200);
      layout._updateHeightLock(0);
      assert(layout.style.minHeight === '1200px', 'transient collapse lowered the lock');
      layout._observeLayout();
      assert(observerCount === 1, 'initial observer was not created');
      layout.disconnectedCallback();
      assert(disconnectCount === 1 && layout._resizeObserver === null, 'observer did not disconnect');
      layout.connectedCallback();
      assert(observerCount === 2, 'observer was not recreated after reconnect');
      assert(layout.style.minHeight === '', 'reconnect kept stale geometry');

      const limited = new PowerSyncLayout();
      limited._built = true;
      limited._items = [
        { dataset: { hidden: 'false' } },
        { dataset: { hidden: 'false' } },
      ];
      limited._lastLayoutColumnCount = 2;
      limited._lastLayoutWidth = 1400;
      limited._updateHeightLock(900);
      const falseChange = limited._scheduleLayoutForResize({ contentRect: { width: 1400, height: 900 } });
      assert(falseChange === false, 'raw breakpoint count overrode the effective lane count');
      assert(limited.style.minHeight === '900px', 'unchanged geometry cleared the lock');

      const resized = new PowerSyncLayout();
      resized._built = true;
      resized._items = [{ dataset: { hidden: 'false' } }];
      resized._lastLayoutColumnCount = 1;
      resized._lastLayoutWidth = 390;
      resized._updateHeightLock(900);
      const trueChange = resized._scheduleLayoutForResize({ contentRect: { width: 500, height: 900 } });
      assert(trueChange === true, 'material same-column resize was not treated as geometry change');
      assert(resized.style.minHeight === '', 'material resize retained stale height');

      const customized = new PowerSyncLayout();
      customized._built = true;
      customized._showingHidden = true;
      customized._items = [];
      customized._updateHeightLock(1100);
      customized._hiddenItems = () => [];
      customized._updateToolbarState = () => {};
      customized._scheduleLayout = () => {};
      customized._setCustomizing(false);
      assert(customized.style.minHeight === '', 'leaving hidden-card preview retained stale height');

      const reordered = new PowerSyncLayout();
      reordered._built = true;
      reordered._updateHeightLock(1000);
      const parent = { insertBefore: () => {} };
      reordered._dragPlaceholder = { parentElement: parent, remove: () => {} };
      reordered._clearDragStyles = () => {};
      reordered._saveOrder = () => {};
      reordered._dropPointerReorder({});
      assert(reordered.style.minHeight === '', 'completed card reorder retained stale height');
    """

    subprocess.run(["node", "-e", prelude + class_source + checks], check=True)


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


def test_battery_health_uses_authoritative_count_when_relay_omits_pack_row():
    """The aggregate pack count must not shrink with partial relay detail."""
    source = STRATEGY_PATH.read_text()
    class_start = source.index("class PowerSyncBatteryHealth extends HTMLElement")
    class_end = source.index("if (!customElements.get('power-sync-battery-health'))")
    class_source = source[class_start:class_end]
    prelude = """
      class HTMLElement {
        attachShadow() { this.shadowRoot = { innerHTML: '' }; }
      }
    """
    checks = """
      const card = new PowerSyncBatteryHealth();
      card.setConfig({ entity: 'sensor.power_sync_battery_health' });
      card.hass = { states: { 'sensor.power_sync_battery_health': {
        state: '106.6',
        attributes: {
          source: 'ha_fleet_api_relay',
          battery_count: 5,
          original_capacity_kwh: 67.5,
          current_capacity_kwh: 72.0,
          battery_1_health_percent: 106.8,
          battery_1_original_kwh: 14.4,
          battery_2_health_percent: 106.8,
          battery_2_original_kwh: 14.4,
          battery_3_health_percent: 106.2,
          battery_3_original_kwh: 14.3,
          battery_4_health_percent: 107.2,
          battery_4_original_kwh: 14.5,
        },
      } } };
      const html = card.shadowRoot.innerHTML;
      if (!html.includes('<div class="pill">5 packs</div>')) {
        throw new Error('authoritative five-pack count was not rendered');
      }
      if (!html.includes('4 of 5 packs reported individually; aggregate capacity includes all 5.')) {
        throw new Error('partial relay detail was not explained');
      }
      const renderedRows = (html.match(/class="pack"/g) || []).length;
      if (renderedRows !== 4) {
        throw new Error(`partial relay data fabricated rows: ${renderedRows}`);
      }

      const fallbackCard = new PowerSyncBatteryHealth();
      fallbackCard.setConfig({ entity: 'sensor.power_sync_battery_health' });
      fallbackCard.hass = { states: { 'sensor.power_sync_battery_health': {
        state: '100',
        attributes: {
          battery_count: -2,
          battery_1_health_percent: 100,
        },
      } } };
      if (!fallbackCard.shadowRoot.innerHTML.includes('<div class="pill">1 pack</div>')) {
        throw new Error('invalid aggregate count did not fall back to detailed rows');
      }

      const aggregateOnlyCard = new PowerSyncBatteryHealth();
      aggregateOnlyCard.setConfig({ entity: 'sensor.power_sync_battery_health' });
      aggregateOnlyCard.hass = { states: { 'sensor.power_sync_battery_health': {
        state: '106.6',
        attributes: {
          battery_count: 5,
          original_capacity_kwh: 67.5,
          current_capacity_kwh: 72.0,
        },
      } } };
      const aggregateOnlyHtml = aggregateOnlyCard.shadowRoot.innerHTML;
      if (!aggregateOnlyHtml.includes('<div class="pill">5 packs</div>')) {
        throw new Error('aggregate-only authoritative count was not rendered');
      }
      if ((aggregateOnlyHtml.match(/class="pack"/g) || []).length !== 0) {
        throw new Error('aggregate-only data fabricated a detailed row');
      }

      const ninePackCard = new PowerSyncBatteryHealth();
      ninePackCard.setConfig({ entity: 'sensor.power_sync_battery_health' });
      const ninePackAttributes = {
        battery_count: 9,
        original_capacity_kwh: 121.5,
        current_capacity_kwh: 121.5,
      };
      for (let index = 1; index <= 9; index++) {
        ninePackAttributes[`battery_${index}_health_percent`] = 100;
      }
      ninePackCard.hass = { states: { 'sensor.power_sync_battery_health': {
        state: '100',
        attributes: ninePackAttributes,
      } } };
      const ninePackHtml = ninePackCard.shadowRoot.innerHTML;
      if ((ninePackHtml.match(/class="pack"/g) || []).length !== 9) {
        throw new Error('valid authoritative count was truncated to eight rows');
      }
      if (ninePackHtml.includes('reported individually')) {
        throw new Error('fully reported nine-pack site was labelled partial');
      }
    """

    subprocess.run(["node", "-e", prelude + class_source + checks], check=True)


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


def test_optimizer_plan_renders_versioned_price_level_projection_separately():
    source = STRATEGY_PATH.read_text()
    class_start = source.index("class PowerSyncOptimizationPlan extends HTMLElement")
    class_end = source.index("if (!customElements.get('power-sync-optimization-plan'))")
    card_source = source[class_start:class_end]

    assert "schedule.ev_charging_projection?.schema_version === 1" in card_source
    assert "Projected EV Price-Level Windows" in card_source
    assert "Expected recovery load" in card_source
    assert "Conditional - may charge" in card_source
    assert "'Suppressed'" in card_source
    assert "label: 'Planned EV load'" in card_source
    assert "label: 'Conditional Price-Level window'" in card_source
    assert 'stroke-dasharray="6,4"' in card_source
    assert "conditional Price-Level charging windows" in card_source
    assert card_source.index("Projected EV Price-Level Windows") < card_source.index(
        "24-Hour Action Plan"
    )
    assert "Actual charging still requires live home, plug, SOC" in card_source


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


def test_chart_tooltip_clamp_uses_the_measured_tooltip_width():
    """A hard-coded half-width pushed the value column outside the chart clip."""
    source = STRATEGY_PATH.read_text()

    # Both tooltip implementations must share the measured clamp.
    assert "Math.max(cssX, 84), rect.width - 84" not in source
    assert "Math.max(cssX, 78), rect.width - 78" not in source
    assert source.count("_clampTooltipLeft(cssX, rect.width, tooltip.offsetWidth)") == 2

    helper = re.search(
        r"function _clampTooltipLeft\([^)]*\) \{.*?\n\}",
        source,
        re.DOTALL,
    )
    assert helper is not None

    checks = """
      const cases = [
        // [cssX, availableWidth, tooltipWidth, expectedLeft]
        [414, 434, 258, 297],   // right edge: box 168..426 inside 0..434
        [20, 434, 258, 137],    // left edge:  box 8..266
        [217, 434, 258, 217],   // mid-chart is untouched
        [400, 434, 282, 285],   // widest theme font still fits
      ];
      for (const [cssX, avail, width, expected] of cases) {
        const actual = _clampTooltipLeft(cssX, avail, width);
        if (Math.abs(actual - expected) > 1e-9) {
          throw new Error(`clamp(${cssX}, ${avail}, ${width}) = ${actual}, want ${expected}`);
        }
      }
      // A tooltip wider than its chart is centred rather than inverted.
      if (_clampTooltipLeft(10, 250, 300) !== 125) throw new Error('degenerate case not centred');
      if (_clampTooltipLeft(240, 250, 300) !== 125) throw new Error('degenerate case not centred');
      // Property: any tooltip that fits stays fully inside the chart.
      for (let avail = 300; avail <= 1100; avail += 37) {
        for (let width = 120; width <= avail; width += 29) {
          for (const cssX of [0, avail / 2, avail, -50, avail + 50]) {
            const left = _clampTooltipLeft(cssX, avail, width);
            if (left - width / 2 < -1e-9 || left + width / 2 > avail + 1e-9) {
              throw new Error(`overflow at avail=${avail} width=${width} cssX=${cssX}`);
            }
          }
        }
      }
      // Degenerate inputs must not produce NaN.
      for (const bad of [[NaN, 434, 258], [217, 0, 258], [217, 434, NaN]]) {
        if (!Number.isFinite(_clampTooltipLeft(...bad))) {
          throw new Error(`non-finite result for ${JSON.stringify(bad)}`);
        }
      }
    """
    inset = re.search(r"const CHART_TOOLTIP_EDGE_INSET_PX = \d+;", source)
    assert inset is not None
    subprocess.run(
        ["node", "-e", f"{inset.group(0)}\n{helper.group(0)}\n{checks}"],
        check=True,
    )


def test_daily_cost_rows_survive_an_unknown_monetary_value():
    """Partial priced coverage must show Unknown, not delete the row."""
    source = STRATEGY_PATH.read_text()

    assert "const hasEntityE = (name) => (hass.states || {})[e(name)] !== undefined;" in source
    for name in (
        "daily_import_cost",
        "daily_export_earnings",
        "daily_avg_cost_per_kwh",
        "mtd_avg_cost_per_kwh",
    ):
        assert f"hasE('{name}')" not in source, name
        assert f"hasEntityE('{name}')" in source, name

    # Cards that are meaningless without a live value keep the availability test.
    assert "const hasE = (name) => has(e(name));" in source

    runtime = """
      const isAvailableState = (id) => {
        const s = (hass.states || {})[id];
        return s && s.state !== 'unavailable' && s.state !== 'unknown';
      };
      const e = (name) => `sensor.power_sync_${name}`;
      const hasE = (name) => isAvailableState(e(name));
      const hasEntityE = (name) => (hass.states || {})[e(name)] !== undefined;
      const rowsFor = (states) => {
        hass = { states };
        const rows = [];
        if (hasEntityE('daily_import_cost') || hasE('amber_usage_today_cost')) {
          if (hasEntityE('daily_import_cost')) rows.push('Estimated Import Cost Today');
          if (hasEntityE('daily_export_earnings')) rows.push('Export Earnings Today');
          if (hasEntityE('daily_avg_cost_per_kwh')) rows.push('Avg Cost per kWh (Today)');
        }
        return rows;
      };
      let hass = {};
      const withValues = rowsFor({
        'sensor.power_sync_daily_import_cost': { state: '1.20' },
        'sensor.power_sync_daily_export_earnings': { state: '0.05' },
        'sensor.power_sync_daily_avg_cost_per_kwh': { state: '0.31' },
      });
      if (withValues.length !== 3) throw new Error('healthy card lost a row');
      const partial = rowsFor({
        'sensor.power_sync_daily_import_cost': { state: '1.20' },
        'sensor.power_sync_daily_export_earnings': { state: 'unknown' },
        'sensor.power_sync_daily_avg_cost_per_kwh': { state: 'unavailable' },
      });
      if (!partial.includes('Export Earnings Today')) {
        throw new Error('unknown export earnings deleted its row');
      }
      if (!partial.includes('Avg Cost per kWh (Today)')) {
        throw new Error('unavailable average cost deleted its row');
      }
      const absent = rowsFor({});
      if (absent.length !== 0) throw new Error('rows emitted for entities that do not exist');
    """
    subprocess.run(["node", "-e", runtime], check=True)
