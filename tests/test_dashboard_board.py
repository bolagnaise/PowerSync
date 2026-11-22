"""Regression tests for the approved dashboard board (power-sync-dashboard.js).

The strategy hands every card to <power-sync-board>; these tests run the real
frontend sources under node with minimal browser stubs.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
import subprocess
import pytest

from test_current_export_reserve_decision import clock, opt_module


FRONTEND = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "frontend"
)
STRATEGY_PATH = FRONTEND / "power-sync-strategy.js"
BOARD_PATH = FRONTEND / "power-sync-dashboard.js"

# Browser globals both files touch at load time.
BROWSER_STUBS = r"""
  const registry = new Map();
  class StubElement {
    constructor() { this.style = { setProperty() {} }; this.dataset = {}; this.classList = { add() {}, remove() {}, toggle() {} }; }
    attachShadow() { this.shadowRoot = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [], addEventListener() {} }; return this.shadowRoot; }
    addEventListener() {} removeEventListener() {} setAttribute() {} getAttribute() { return null; } appendChild() {} dispatchEvent() {}
    querySelector() { return null; } querySelectorAll() { return []; }
  }
  const context = {
    console, Date, Math, JSON, Promise, Number, String, Array, Object, Set, Map, URL, Intl, setTimeout, clearTimeout, queueMicrotask,
    HTMLElement: StubElement, CustomEvent: class { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } },
    customElements: {
      define: (name, cls) => registry.set(name, cls),
      get: name => (context.__boardMissing && name === 'power-sync-board' ? undefined : registry.get(name)),
    },
    window: {}, document: { getElementById: () => null, createElement: () => new StubElement(), head: { appendChild() {} } },
    requestAnimationFrame: fn => fn(), getComputedStyle: () => ({ getPropertyValue: () => '' }),
    localStorage: (() => { const s = {}; return { getItem: k => (k in s ? s[k] : null), setItem: (k, v) => { s[k] = String(v); }, removeItem: k => { delete s[k]; } }; })(),
  };
  context.window.localStorage = context.localStorage;
  context.globalThis = context;
"""


def _run(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _strategy_script(board_missing: bool) -> str:
    return (
        "const fs = require('fs'); const vm = require('vm');"
        + BROWSER_STUBS
        + f"context.__boardMissing = {'true' if board_missing else 'false'};"
        + r"""
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
  // The board module registers <power-sync-board>; stand in for it here.
  context.customElements.define('power-sync-board', class {});
  const s = (state, attributes = {}) => ({ state: String(state), attributes, last_updated: '2026-10-30T03:00:00Z' });
  const states = {
    'sensor.power_sync_battery_level': s(69, { unit_of_measurement: '%' }),
    'sensor.power_sync_battery_power': s(-10.9, { unit_of_measurement: 'kW' }),
    'sensor.power_sync_solar_power': s(0, { unit_of_measurement: 'kW' }),
    'sensor.power_sync_grid_power': s(14.7, { unit_of_measurement: 'kW' }),
    'sensor.power_sync_home_load': s(3.8, { unit_of_measurement: 'kW' }),
    'sensor.power_sync_current_import_price': s(0, { unit_of_measurement: 'AUD/kWh' }),
    'sensor.power_sync_current_export_price': s(0, { unit_of_measurement: 'AUD/kWh' }),
    'sensor.power_sync_optimization_status': s('active'),
    'sensor.power_sync_battery_mode': s('normal'),
    'sensor.power_sync_tariff_schedule': s('PEAK (51.00c)', { plan_name: 'FOUR4FREE', utility: 'GloBird', schedule: [] }),
    'sensor.power_sync_daily_solar_energy': s(0, { unit_of_measurement: 'kWh' }),
    'sensor.power_sync_daily_grid_import': s(49.1, { unit_of_measurement: 'kWh' }),
    'sensor.power_sync_daily_export_earnings': s(0.75),
    'sensor.power_sync_lifetime_solar_energy': s(16074.7, { unit_of_measurement: 'kWh' }),
    'sensor.power_sync_firmware': s('26.34.2'),
    'number.power_sync_backup_reserve': s(100, { min: 0, max: 100, step: 1 }),
    'select.power_sync_operation_mode': s('autonomous', { options: ['self_consumption', 'autonomous', 'backup'] }),
    'select.power_sync_force_charge_duration': s('30', { options: ['15', '30', '60'] }),
    'switch.power_sync_monitoring_mode': s('on'),
    'switch.power_sync_optimization_enabled': s('on'),
  };
  const hass = {
    states,
    callWS: async ({ type }) => {
      if (type === 'lovelace/resources') return [{ url: '/power_sync/frontend/power-sync-strategy.js?v=2.12.1558.1', type: 'module' }];
      if (type === 'config/entity_registry/list') {
        return Object.keys(states).filter(id => !id.startsWith('sensor.')).map(entity_id => ({
          entity_id, platform: 'power_sync', unique_id: `entry_${entity_id.split('.power_sync_')[1]}`,
        }));
      }
      return [];
    },
  };
  const Strategy = vm.runInContext('PowerSyncStrategy', context);
  Strategy.generate({}, hass).then(result => {
    const view = result.views[0];
    const top = view.cards[0];
    const cards = top.cards || [];
    console.log(JSON.stringify({
      type: top.type,
      legacySupport: (top.columns || [view.cards]).flat().filter(c => c.type === 'custom:ps-support-card').length,
      keys: cards.map(c => c.key),
      wide: cards.filter(c => c.size === 'wide').map(c => c.key),
      natives: [...new Set(cards.map(c => c.native).filter(Boolean))],
      missingRender: cards.filter(c => !c.native && !c.card).map(c => c.key),
      optRows: (cards.find(c => c.key === 'optset')?.config.rows || []).map(r => r.entity),
      evSettings: cards.find(c => c.key === 'evset') || null,
      pwRows: (cards.find(c => c.key === 'pwset')?.config.rows || []).map(r => r.entity),
      controls: cards.find(c => c.key === 'controls')?.config.entities || null,
      controlsAdvanced: cards.find(c => c.key === 'controls')?.config.advanced || false,
      nowEntities: cards.find(c => c.key === 'now')?.config.entities || null,
      planEntities: cards.find(c => c.key === 'plan')?.config.entities || null,
      tariffEntities: cards.find(c => c.key === 'tariff')?.config.entities || null,
      tariffMeta: cards.find(c => c.key === 'tariff')?.meta || null,
    }));
  }).catch(err => { console.error(err); process.exit(1); });
"""
    )


def test_generate_hands_every_card_to_the_board():
    out = _run_strategy(board_missing=False)
    assert out["type"] == "custom:power-sync-board"
    keys = out["keys"]
    assert len(keys) == len(set(keys)), "card keys must be unique"
    for key in ("now", "plan", "controls", "tariff", "today", "pwset", "optset",
                "prices", "energy", "forecast", "bp48", "sl48", "status", "lifetime"):
        assert key in keys, key
    assert out["wide"] == ["now", "plan"]
    assert out["missingRender"] == []
    # Native cards used must all be defined by the board module.
    board_source = BOARD_PATH.read_text()
    for tag in out["natives"]:
        assert f"'{tag}':" in board_source, tag
    assert "switch.power_sync_monitoring_mode" in out["optRows"]
    assert "switch.power_sync_optimization_enabled" in out["optRows"]
    assert "select.power_sync_operation_mode" in out["pwRows"]
    assert out["controls"]["mode"] == "sensor.power_sync_battery_mode"
    assert out["nowEntities"]["exportEarnings"] == "sensor.power_sync_daily_export_earnings"
    assert out["controls"]["chargeDuration"] == "select.power_sync_force_charge_duration"
    assert out["tariffMeta"] == "GloBird FOUR4FREE"
    assert out["planEntities"]["exportEarnings"] == "sensor.power_sync_daily_export_earnings"
    assert out["planEntities"]["mode"] == "sensor.power_sync_battery_mode"
    assert out["tariffEntities"]["exportEarnings"] == "sensor.power_sync_daily_export_earnings"


def test_generate_falls_back_to_classic_layout_without_board():
    out = _run_strategy(board_missing=True)
    assert out["type"] == "custom:power-sync-layout"


def _run_strategy(
    board_missing: bool, *, solar_entity: str | None = None,
    optimizer: bool = True, admin: bool = False
) -> dict:
    script = _strategy_script(board_missing)
    setup = ""
    script = script.replace("    states,", f"    user: {{ is_admin: {str(admin).lower()} }}, states,")
    if solar_entity:
        setup += f"states[{json.dumps(solar_entity)}] = s('off');"
    if not optimizer:
        setup += "delete states['sensor.power_sync_optimization_status'];"
    script = script.replace("  const hass = {", setup + "  const hass = {")
    # Renamed entities retain their registry unique ID.
    if solar_entity:
        script = script.replace(
            "unique_id: `entry_${entity_id.split('.power_sync_')[1]}`",
            f"unique_id: entity_id === {json.dumps(solar_entity)} ? 'entry_solar_surplus_ev_charging' : `entry_${{entity_id.split('.power_sync_')[1]}}`",
        )
    result = subprocess.run(
        ["node", "-e", script, str(STRATEGY_PATH)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_solar_surplus_has_no_duplicate_dashboard_toggle():
    for optimizer in (True, False):
        entity = "switch.power_sync_solar_surplus_ev_charging"
        out = _run_strategy(False, solar_entity=entity, optimizer=optimizer)
        assert out["evSettings"] is None
        assert entity not in out["optRows"]
        assert "ev" in out["keys"]


def _board_script(body: str) -> str:
    return (
        "const fs = require('fs'); const vm = require('vm');"
        + BROWSER_STUBS
        + r"""
  vm.createContext(context);
  const src = fs.readFileSync(process.argv[1], 'utf8').replace(/^export \{[^}]*\};?\s*$/m, '');
  vm.runInContext(src + '\n;globalThis.__t = { PowerSyncBoard, PsSettingsDialog, PS_SETTING_EDITORS, PsSettingsCard, PsTodayCard, PsNowCard, PsTariffCard, psExportOffer, PsPlanCard, PsControlsCard, PsInverterControlsCard, psUntilPreview, psSchedule, psWindows, psActionKey, PS_LAYOUT_KEY, PS_DEFAULT_LAYOUT, psChartValue, psChart, psBars, PsPricesCard, PsEnergyCard, PsLoadForecastCard, PsSolarLoadCard, PsBatteryPlanCard, psApiCache, psPlannedRunEnd, psBatteryControl };', context);
  const T = context.__t;
"""
        + body
    )


def _run_board(body: str) -> dict:
    result = subprocess.run(
        ["node", "-e", _board_script(body), str(BOARD_PATH)],
        check=True, capture_output=True, text=True, timeout=20,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_layout_places_every_present_card_exactly_once():
    out = _run_board(r"""
  const board = Object.create(T.PowerSyncBoard.prototype);
  const keys = ['now', 'plan', 'controls', 'scene', 'tariff', 'today', 'ev', 'details-grid', 'status'];
  board._config = { cards: keys.map(key => ({ key, title: key, size: ['now', 'plan'].includes(key) ? 'wide' : undefined })) };
  const result = {};
  for (const n of [1, 2, 3]) {
    const m = board._layoutFor(n);
    const placed = m.lanes.flat().filter(k => keys.includes(k));
    result[n] = { wide: m.wide, lanes: m.lanes.length, placed: placed.sort(), dupes: placed.length !== new Set(placed).size };
  }
  // A saved layout with a stale key and a duplicate must still place each card once.
  context.localStorage.setItem(T.PS_LAYOUT_KEY, JSON.stringify({ 2: { wide: ['plan', 'now'], lanes: [['status', 'gone', 'status'], ['controls']] } }));
  const saved = board._layoutFor(2);
  result.saved = { wide: saved.wide, first: saved.lanes[0][0], flat: saved.lanes.flat().filter(k => keys.includes(k)).sort() };
  console.log(JSON.stringify(result));
""")
    std = sorted(["controls", "scene", "tariff", "today", "ev", "details-grid", "status"])
    for n in ("1", "2", "3"):
        assert out[n]["wide"] == ["now", "plan"]
        assert out[n]["lanes"] == int(n)
        assert out[n]["placed"] == std
        assert out[n]["dupes"] is False
    assert out["saved"]["wide"] == ["plan", "now"]
    assert out["saved"]["first"] == "status"
    assert out["saved"]["flat"] == std


def test_schedule_actions_and_windows():
    out = _run_board(r"""
  const t0 = Date.parse('2026-10-30T03:00:00Z');
  const ts = Array.from({ length: 8 }, (_, i) => new Date(t0 + i * 30 * 60000).toISOString());
  const data = { schedule: {
    timestamps: ts,
    soc: [0.5, 0.55, 0.6, 0.6, 0.6, 0.58, 0.5, 0.5],
    charge_w: [3000, 3000, 0, 0, 0, 0, 0, 0],
    discharge_w: [0, 0, 0, 0, 1000, 2000, 5000, 0],
    battery_consume_w: [0, 0, 0, 0, 1000, 2000, 0, 0],
    battery_export_w: [0, 0, 0, 0, 0, 0, 5000, 0],
    ev_charging_w: [0, 7000, 0, 0, 0, 0, 0, 0],
    import_price: [0, 0, 0.31, 0.31, 0.51, 0.51, 0.51, 0.31],
    export_price: [0, 0, 0, 0, 0.1, 0.1, 0.45, 0],
  } };
  const { rows, interval } = T.psSchedule(data);
  const charge = T.psWindows(rows, interval, 'charge', t0);
  console.log(JSON.stringify({
    interval,
    acts: rows.map(r => r.act),
    soc0: rows[0].soc, imp: rows[4].imp, ev: rows[1].evKw,
    charge: charge.map(w => [w.start === t0, (w.end - w.start) / 60000]),
    keys: ['charge', 'force_discharge', 'idle', 'self_consumption', 'export', ''].map(T.psActionKey),
  }));
""")
    assert out["interval"] == 30
    assert out["acts"] == ["charge", "charge", "hold", "hold", "self", "self", "export", "hold"]
    assert out["soc0"] == 50
    assert abs(out["imp"] - 51) < 1e-9
    assert out["ev"] == 7
    assert out["charge"] == [[True, 60]]
    assert out["keys"] == ["charge", "self", "hold", "self", "export", ""]


def test_plan_window_shows_varying_targets_instead_of_only_peak_power():
    """Ticket 155: a 100 W slot inside a 5 kW run must remain visible."""
    out = _run_board(r"""
  const start = Math.ceil(Date.now() / 3600000) * 3600000;
  function render(powers, kind = 'export') {
    const ts = powers.map((_, i) => new Date(start + i * 300000).toISOString());
    const zeros = powers.map(() => 0);
    const data = { enabled: true, schedule: {
      timestamps: ts, soc: powers.map((_, i) => 0.8 - i * 0.01),
      charge_w: kind === 'export' ? zeros : powers,
      discharge_w: kind === 'export' ? powers : zeros,
      battery_export_w: kind === 'export' ? powers : zeros,
      battery_consume_w: zeros,
      grid_import_w: kind === 'charge' ? powers : zeros,
      import_price: powers.map(() => 0.45), export_price: powers.map(() => 0.27),
    } };
    const card = new T.PsPlanCard();
    card._hass = { states: {}, locale: { time_format: '24' }, config: { currency: 'AUD' } };
    card._config = { entities: {} };
    card._api = () => data;
    card._loadHistory = () => {};
    card.querySelector = () => ({ addEventListener() {} });
    card._render();
    return card.innerHTML.match(/<div class="d">([^<]*)<\/div>/g);
  }
  console.log(JSON.stringify({
    variable: render([5000, 100.13731406, 100.12845288, 5000]),
    constant: render([5000, 5000, 5000]),
    rounding: render([5000, 4999.98, 4999.97]),
    charge: render([2000, 5000, 3000], 'charge'),
    solar: render([200, 1000, 500], 'solar'),
  }));
""")
    assert "100 W–5.0 kW" in out["variable"][0]
    assert "0.85 kWh" in out["variable"][0]
    assert "5.0 kW" in out["constant"][0] and "–" not in out["constant"][0]
    assert "5.0 kW" in out["rounding"][0] and "–" not in out["rounding"][0]
    assert "2.0 kW–5.0 kW" in out["charge"][0]
    assert "200 W–1.0 kW" in out["solar"][0]


def test_plan_separates_solar_charge_from_paid_grid_share_and_capped_export_value():
    """Ticket 133: natural solar at 8am must not become paid grid charging.

    Premium export quota affects earnings, while the energy row retains the
    full planned export. Exercise the actual plan renderer as well as its rows.
    """
    out = _run_board(r"""
  const start = Math.ceil(Date.now() / 3600000) * 3600000;
  const ts = Array.from({ length: 6 }, (_, i) => new Date(start + i * 1800000).toISOString());
  const data = {
    enabled: true, current_action: 'self_consumption',
    next_actions: [{ action: 'charge', timestamp: ts[2] }],
    daily_cost_breakdown: { zerohero: {
      status: 'enabled', bonus_export_cap_kwh: 15,
      bonus_export_kwh_remaining: 5, bonus_export_kwh_used: 10,
    } },
    schedule: {
      timestamps: ts, soc: [0.4, 0.45, 0.5, 0.55, 0.6, 0.5],
      charge_w: [4000, 4000, 6000, 6000, 0, 0],
      discharge_w: [0, 0, 0, 0, 10000, 10000],
      battery_consume_w: [0, 0, 0, 0, 0, 0],
      battery_export_w: [0, 0, 0, 0, 10000, 10000],
      grid_import_w: [0, 0, 2000, 2000, 0, 0],
      import_price: Array(6).fill(0.4), export_price: Array(6).fill(0.1),
      export_value_price: [0.1, 0.1, 0.1, 0.1, 0.1, 0.02],
    },
  };
  const card = new T.PsPlanCard();
  card._hass = { states: {}, locale: { time_format: '24' }, config: { currency: 'AUD' } };
  card._config = { entities: {} };
  card._api = () => data;
  card._loadHistory = () => {};
  card.querySelector = () => ({ addEventListener() {} });
  card._render();
  const { rows, interval } = T.psSchedule(data);
  console.log(JSON.stringify({
    acts: rows.map(r => r.act), html: card.innerHTML,
    firstBand: card._model.act(card._model.plan[0].h + 0.01),
    solar: T.psWindows(rows, interval, 'solar', start).map(w => (w.end - w.start) / 60000),
  }));
""")
    assert out["acts"] == ["solar", "solar", "charge", "charge", "export", "export"]
    assert out["solar"] == [60]
    assert out["firstBand"] == "solar"
    assert '<span>Current control</span><b>Self-consumption' in out["html"]
    assert '<span>Now</span><b>Power the home' not in out["html"]
    windows = out["html"].split('<div class="win">')[1:]
    assert len(windows) == 3
    assert "Solar charge" in windows[0] and "4.0 kWh" in windows[0]
    assert "A$0.00" in windows[0] and "40.0 c avg" not in windows[0]
    assert "6.0 kWh" in windows[1] and "2.0 kWh from grid" in windows[1]
    assert "A$0.80" in windows[1]
    assert "10.0 kWh" in windows[2] and "A$0.60" in windows[2]
    assert "6.0 c avg" in windows[2]
    assert "Super Export bonus cap" in out["html"] and "15.0 kWh/day" in out["html"]
    assert "5.0 kWh remaining today" in out["html"]
    assert "not total export" in out["html"]


@pytest.mark.parametrize("start,expected_left,expected_width", [
    ("13:30", "56.2500", "10.4167"),
    ("00:00", "0.0000", "66.6667"),
])
def test_tariff_ribbon_uses_clock_positions_for_partial_cosy_day(start, expected_left, expected_width):
    out = _run_board(r"""
  const card = new T.PsTariffCard();
  card._hass = {config: {currency: 'GBP', time_zone: 'UTC'}, states: {
    tariff: {state: 'off_peak', attributes: {schedule: [
      {time: 'START', date_label: 'Today', buy: 0.141, sell: 0},
      {time: '16:00', date_label: 'Today', buy: 0.432, sell: 0},
      {time: '19:00', date_label: 'Today', buy: 0.288, sell: 0},
      {time: '22:00', date_label: 'Today', buy: 0.141, sell: 0},
    ]}}
  }};
  card._config = {entities: {tariff: 'tariff'}};
  card._render();
  console.log(JSON.stringify({html: card.innerHTML}));
""".replace("START", start))
    html = out["html"]
    assert f"left:{expected_left}%;width:{expected_width}%;" in html
    assert "left:66.6667%;width:12.5000%;" in html
    assert "left:79.1667%;width:12.5000%;" in html
    assert "left:91.6667%;width:8.3333%;" in html
    assert '<span class="i">14.1</span>' in html
    assert '<span class="i">43.2</span>' in html


def test_tariff_spike_keeps_all_schedule_rates_in_minor_units():
    out = _run_board(r"""
  const card = new T.PsTariffCard();
  card._hass = {config: {currency: 'AUD', time_zone: 'UTC'}, states: {
    tariff: {state: 'peak', attributes: {schedule: [
      {time: '21:00', date_label: 'Today', buy: 2.7, sell: 0.2},
      {time: '21:30', date_label: 'Today', buy: 0.246, sell: 0.132},
      {time: '22:30', date_label: 'Today', buy: -0.035, sell: -0.012},
    ]}}
  }};
  card._config = {entities: {tariff: 'tariff'}};
  card._render();
  console.log(JSON.stringify({html: card.innerHTML}));
""")
    html = out["html"]
    assert '<span class="i">270.0</span><span class="e">20.0</span>' in html
    assert '<span class="i">24.6</span><span class="e">13.2</span>' in html
    assert '<span class="i">-3.5</span><span class="e">-1.2</span>' in html


def test_tariff_highlights_home_assistant_clock_when_browser_is_elsewhere():
    out = _run_board(r"""
  class FixedDate extends Date {
    constructor(...args) { super(...(args.length ? args : ['2026-11-19T01:00:00Z'])); }
    static now() { return new FixedDate().getTime(); }
  }
  context.Date = FixedDate;
  const card = new T.PsTariffCard();
  card._hass = {config: {currency: 'AUD', time_zone: 'Australia/Adelaide'},
    locale: {language: 'en-GB', time_format: '24'}, states: {
      tariff: {state: 'free', attributes: {current_time: '11:30', schedule: [
        {time: '00:00', buy: 0.352, sell: 0.05},
        {time: '11:00', buy: 0, sell: 0.05},
        {time: '14:00', buy: 0.352, sell: 0.05},
      ]}},
    },
  };
  card._config = {entities: {tariff: 'tariff'}};
  card._render();
  console.log(JSON.stringify({html: card.innerHTML}));
""")
    assert "Free, now" in out["html"]
    assert "11:00–14:00, ends in 2 h 30 min" in out["html"]


def test_tariff_shows_configured_covau_windows_when_standard_rates_apply():
    out = _run_board(r"""
  const card = new T.PsTariffCard();
  card._hass = {config: {currency: 'AUD', time_zone: 'UTC'}, states: {
    tariff: {state: 'shoulder', attributes: {
      schedule: [{time: '00:00', buy: 0.352, sell: 0.05}],
      unavailable_quota_windows: {
        import: {start: '11:00', end: '14:00', reason: 'unverified'},
        export: {start: '18:00', end: '21:00', reason: 'exhausted'},
      },
    }},
  }};
  card._config = {entities: {tariff: 'tariff'}};
  card._render();
  console.log(JSON.stringify({html: card.innerHTML}));
""")
    assert "Free import (11:00–14:00) · remaining allowance could not be verified" in out["html"]
    assert "Premium export (18:00–21:00) · today’s allowance is used up" in out["html"]
    assert '<span class="i">35.2</span>' in out["html"]


def test_tariff_names_distinguish_agl_reward_by_export_rate():
    out = _run_board(r"""
  const tou = [
    {period: 'OFF_PEAK', buy: 6.6, sell: 2},
    {period: 'PEAK_2', buy: 45, sell: 2},
    {period: 'PEAK_2_AGL_REWARD', buy: 45, sell: 27},
    {period: 'PEAK', buy: 45.4, sell: 2},
    {period: 'PEAK_AGL_REWARD', buy: 45.4, sell: 27},
    {period: 'SHOULDER', buy: 24, sell: 2},
  ];
  const attrs = {tou_schedule: tou, schedule: [
    {time: '00:00', buy: 0.24, sell: 0.02},
    {time: '11:00', buy: 0.066, sell: 0.02},
    {time: '15:00', buy: 0.45, sell: 0.02},
    {time: '17:00', buy: 0.454, sell: 0.27},
    {time: '21:00', buy: 0.24, sell: 0.02},
  ]};
  const card = new T.PsTariffCard();
  card._hass = {config: {currency: 'AUD', time_zone: 'UTC'},
    states: {tariff: {state: 'peak', attributes: attrs}}};
  card._config = {entities: {tariff: 'tariff'}};
  card._render();
  const forward = card.innerHTML;
  attrs.tou_schedule = [...tou].reverse();
  card._render();
  console.log(JSON.stringify({forward, reverse: card.innerHTML}));
""")
    for html in out.values():
        assert 'Peak 2 agl reward' not in html
        assert 'Peak 2' in html
        assert 'Peak agl reward' in html
        assert html.count('agl reward') == 1
        assert '<span class="i">45.0</span><span class="e">2.0</span>' in html
        assert '<span class="i">45.4</span><span class="e">27.0</span>' in html


def test_super_export_offer_is_separate_from_base_in_tariff_and_plan():
    out = _run_board(r"""
  class FixedDate extends Date {
    constructor(...args) { super(...(args.length ? args : ['2026-11-03T06:00:00Z'])); }
    static now() { return new FixedDate().getTime(); }
  }
  context.Date = FixedDate;
  const s = (state, attributes = {}) => ({ state: String(state), attributes });
  const bonus = { window_start: '18:00', window_end: '21:00', total_rate: 0.15,
    cap_kwh: 15, remaining_kwh: null };
  const hass = { config: { time_zone: 'Australia/Sydney', currency: 'AUD' }, states: {
    earnings: s('unknown', { currency: 'AUD', export_bonus: bonus, export_bonus_coverage: 'partial' }),
    tariff: s('peak', { schedule: [{time: '00:00', buy: 0.572, sell: 0.05}] }),
    price: s(0.05, {currency: 'AUD', unit_of_measurement: 'AUD/kWh'}),
  }};
  const entities = {tariff: 'tariff', exportEarnings: 'earnings', exportPrice: 'price'};
  const ts = ['2026-11-03T06:00:00Z', '2026-11-03T07:00:00Z', '2026-11-03T08:00:00Z',
    '2026-11-03T09:00:00Z', '2026-11-03T10:00:00Z'];
  const data = {schedule: {timestamps: ts, import_price: ts.map(() => 0.572),
    export_price: ts.map(() => 0.05), export_value_price: [0.05, 0.15, 0.15, 0.10, 0.05]}};
  T.psApiCache.set('power_sync/optimization', {data});
  const plan = new T.PsPlanCard();
  plan._hass = hass; plan._config = {entities}; plan._loadHistory = () => {};
  plan.querySelector = () => ({addEventListener() {}});
  plan._render();
  const model = plan._model;
  const values = model.plan.map(r => [model.exp(r.h), model.exportValue(r.h)]);
  const tariff = new T.PsTariffCard(); tariff._hass = hass; tariff._config = {entities};
  tariff._render();
  const partial = tariff.innerHTML;
  bonus.remaining_kwh = 0;
  tariff._render();
  const exhausted = tariff.innerHTML;
  bonus.total_rate = 0.30; bonus.cap_kwh = 7;
  tariff._render();
  const custom = tariff.innerHTML;
  hass.states.earnings.attributes.export_bonus = null;
  tariff._render();
  console.log(JSON.stringify({values, plan: plan.innerHTML, partial, exhausted, custom,
    disabled: tariff.innerHTML}));
""")
    assert out["values"] == [[5, 5], [5, 15], [5, 15], [5, 10], [5, 5]]
    for html in (out["plan"], out["partial"], out["exhausted"]):
        assert "Super Export offer" in html
        assert "15.0 c/kWh total" in html
        assert "18:00–21:00" in html
        assert "first 15.0 kWh/day" in html
        assert "solar and battery" in html
        assert "base" in html
    assert 'class="e">5.0</span>' in out["partial"]
    assert "30.0 c/kWh total" in out["custom"] and "first 7.0 kWh/day" in out["custom"]
    assert "Super Export offer" not in out["disabled"]


def test_super_export_offer_validation_and_forecast_fallback():
    out = _run_board(r"""
  const bonus = {window_start: '23:00', window_end: '02:00', total_rate: 0.15, cap_kwh: 15};
  const hass = {config: {currency: 'AUD', time_zone: 'Australia/Sydney'}, states: {
    earnings: {attributes: {currency: 'AUD', export_bonus: bonus}},
  }};
  const en = {exportEarnings: 'earnings'};
  const valid = overrides => {
    hass.states.earnings.attributes.export_bonus = {...bonus, ...overrides};
    return !!T.psExportOffer(hass, en);
  };
  const result = {
    overnight: valid({}), zero: valid({cap_kwh: 0}), missing: valid({cap_kwh: null}),
    invalid: valid({window_start: '25:00'}), invalidRate: valid({total_rate: '0.15'}),
  };
  hass.config.currency = 'USD'; result.currency = valid({});
  const plan = new T.PsPlanCard(); plan._hass = hass; plan._config = {entities: en};
  const start = Date.now() + 3600000;
  const ts = Array.from({length: 4}, (_, i) => new Date(start + i * 1800000).toISOString());
  const data = {schedule: {timestamps: ts, export_price: [0.05, 0.20, 0.05, 0.05],
    export_value_price: [null, 0.15, 'invalid', 0.05]}};
  const m = plan._buildModel(data);
  result.rates = m.plan.map(r => m.exportValue(r.h));
  result.past = m.exportValue(m.nowH - 1);
  // An isolated premium at xx:05 must set the scale even when the quarter-hour
  // samples fall before and after it. Keep the fixture independent of wall time.
  const edge = Math.floor(Date.now() / 3600000) * 3600000 + 7200000;
  const short = plan._buildModel({schedule: {
    timestamps: [0, 5, 10, 15].map(min => new Date(edge + min * 60000).toISOString()),
    export_price: [0.05, 0.05, 0.05, 0.05], export_value_price: [0.05, 0.80, 0.05, 0.05],
  }});
  result.shortPremiumFits = short.pMax >= 80;
  console.log(JSON.stringify(result));
""")
    assert out == dict(overnight=True, zero=False, missing=False, invalid=False,
                       invalidRate=False, currency=False, rates=[5, 20, 5, 5], past=None,
                       shortPremiumFits=True)


def test_plan_draws_exact_partial_quota_intervals_and_scrubs_their_value():
    out = _run_board(r"""
  class El {
    constructor(tag = '') { this.tag = tag; this.children = []; this.attrs = {}; this.style = {}; }
    appendChild(el) { this.children.push(el); }
    setAttribute(k, v) { this.attrs[k] = v; }
    set textContent(v) { this.text = v; this.children = []; }
    get textContent() { return this.text || ''; }
  }
  context.document.createElementNS = (_, tag) => new El(tag);
  const start = Date.now() + 3600000;
  const ts = Array.from({length: 4}, (_, i) => new Date(start + i * 300000).toISOString());
  const data = {schedule: {timestamps: ts, export_price: [0.05, 0.05, 0.05, 0.05],
    export_value_price: [0.15, 0.10, 0.05, 0.05]}};
  const card = new T.PsPlanCard(); card._hass = {config: {currency: 'AUD'}};
  card._api = () => data;
  const svg = new El('svg'); svg.clientWidth = 480; svg.clientHeight = 320;
  const tip = new El();
  card.querySelector = s => s === 'svg.dayline' ? svg : s === '[data-sc="v"]' ? tip : null;
  const m = card._model = card._buildModel(data);
  card._draw();
  const value = svg.children.find(e => e.tag === 'path' && e.attrs['stroke-dasharray'] === '2 3');
  const x = card._geo.x, y = card._geo.yP;
  card._setScrub(m.plan[1].h + 0.001);
  console.log(JSON.stringify({path: value?.attrs.d, expected:
    `M${x(m.plan[0].h)},${y(15)} L${x(m.plan[0].h + 5/60)},${y(15)} ` +
    `L${x(m.plan[1].h)},${y(10)} L${x(m.plan[1].h + 5/60)},${y(10)} `,
    tip: tip.textContent}));
""")
    assert out["path"] == out["expected"]
    assert out["tip"] == "10.0 c"


def test_now_export_rate_uses_eligible_total_bonus_then_returns_to_base():
    """Ticket 446: the current rate must agree with capped export earnings."""
    out = _run_board(r"""
  const now = Date.parse('2026-11-02T08:21:00Z'); // 19:21 Sydney, browser is UTC
  vm.runInContext(`Date.now = () => ${now}`, context);
  const bonus = { day: '2026-11-02', window_start: '18:00', window_end: '21:00',
    total_rate: 0.15, remaining_kwh: 10, observed_at: '2026-11-02T08:21:00+00:00' };
  const s = (state, attributes = {}) => ({ state: String(state), attributes });
  const states = { grid: s(-5.2, { unit_of_measurement: 'kW' }),
    price: s(0.05, { unit_of_measurement: 'AUD/kWh', currency: 'AUD' }),
    earnings: s(0.75, { currency: 'AUD', coverage: 'complete', export_bonus_coverage: 'complete', export_bonus: bonus }) };
  const card = new T.PsNowCard();
  card._hass = { states, config: { currency: 'AUD', time_zone: 'Australia/Sydney' } };
  card._config = { entities: { grid: 'grid', exportPrice: 'price', exportEarnings: 'earnings' } };
  card._api = () => ({ enabled: true, daily_cost_breakdown: { zerohero: {
    status: 'enabled', window_start: '18:00', window_end: '21:00', super_export_rate: 0.15,
    bonus_export_cap_kwh: 15, bonus_export_kwh_remaining: 10 } } });
  const render = () => { card._render(); return card.innerHTML; };
  const active = render();
  bonus.remaining_kwh = 0;
  const exhausted = render();
  bonus.remaining_kwh = 10;
  vm.runInContext(`Date.now = () => ${Date.parse('2026-11-02T10:00:00Z')}`, context);
  const ended = render();
  console.log(JSON.stringify({ active, exhausted, ended }));
""")
    assert "Exporting at 15.0 c/kWh" in out["active"]
    assert "Exporting at 5.0 c/kWh" in out["exhausted"]
    assert "Exporting at 5.0 c/kWh" in out["ended"]


def test_now_bonus_rate_guards_local_window_quota_coverage_currency_and_staleness():
    out = _run_board(r"""
  const render = (options = {}) => {
    const now = Date.parse(options.now || '2026-11-02T08:21:00Z');
    vm.runInContext(`Date.now = () => ${now}`, context);
    const bonus = { day: '2026-11-02', window_start: '18:00', window_end: '21:00',
      total_rate: 0.15, remaining_kwh: 5, observed_at: new Date(now).toISOString(), ...options.bonus };
    const attrs = { currency: 'AUD', coverage: 'complete', export_bonus_coverage: 'complete', export_bonus: bonus, ...options.attrs };
    const card = new T.PsNowCard();
    card._hass = { config: { currency: options.currency || 'AUD', time_zone: options.zone === undefined ? 'Australia/Sydney' : options.zone }, states: {
      grid: { state: options.grid || '-5.2', attributes: { unit_of_measurement: 'kW' } },
      price: { state: options.price || '0.05', attributes: { currency: 'AUD', unit_of_measurement: options.priceUnit || 'AUD/kWh', ...options.priceAttrs } },
      earnings: { state: options.earningsState || '0.75', attributes: attrs },
    } };
    card._config = { entities: { grid: 'grid', exportPrice: 'price', exportEarnings: 'earnings' } };
    card._api = () => null; card._render(); return card.innerHTML;
  };
  console.log(JSON.stringify({
    partial: render({ attrs: { export_bonus_coverage: 'partial' } }),
    hardwareGap: render({ attrs: { coverage: 'partial' } }),
    unknown: render({ bonus: { remaining_kwh: null } }),
    missing: render({ bonus: { remaining_kwh: '' } }),
    stale: render({ bonus: { observed_at: '2026-11-02T08:15:00Z' } }),
    oldDay: render({ bonus: { day: '2026-11-01' } }),
    noZone: render({ zone: '' }), badZone: render({ zone: 'Bad/Timezone' }),
    currencyMismatch: render({ attrs: { currency: 'GBP' } }),
    noPrice: render({ price: 'unavailable' }),
    offline: render({ earningsState: 'unavailable' }),
    before: render({ now: '2026-11-02T06:59:00Z' }),
    start: render({ now: '2026-11-02T07:00:00Z' }),
    ended: render({ now: '2026-11-02T10:00:00Z' }),
    winter: render({ now: '2026-07-02T08:00:00Z', bonus: { day: '2026-07-02' } }),
    overnight: render({ now: '2026-11-01T14:00:00Z', bonus: { window_start: '23:00', window_end: '02:00' } }),
    outsideOvernight: render({ bonus: { window_start: '23:00', window_end: '02:00' } }),
    custom: render({ bonus: { total_rate: 0.1075, remaining_kwh: 0.001 } }),
    higherBase: render({ price: '0.20' }),
    minorBase: render({ price: '5', priceUnit: 'c/kWh' }),
    haCurrencyDiffers: render({ currency: 'GBP' }),
    gbp: render({ priceUnit: 'GBP/kWh', priceAttrs: { currency: 'GBP' }, attrs: { currency: 'GBP' } }),
    baseProvider: render({ attrs: { export_bonus: null } }),
    importing: render({ grid: '5.2' }),
    idle: render({ grid: '0.01' }),
  }));
""")
    for key in ("partial", "hardwareGap", "unknown", "missing", "stale", "oldDay",
                "noZone", "badZone", "currencyMismatch", "offline"):
        assert "Exporting at 5.0" in out[key], key
        assert "base rate; bonus unknown" in out[key], key
    assert "15.0" not in out["noPrice"]
    assert "Exporting (rate unknown)" in out["noPrice"]
    for key in ("before", "ended", "outsideOvernight", "baseProvider"):
        assert "Exporting at 5.0 c/kWh" in out[key], key
        assert "bonus unknown" not in out[key], key
    for key in ("start", "winter", "overnight", "minorBase", "haCurrencyDiffers"):
        assert "Exporting at 15.0 c/kWh" in out[key], key
    assert "Exporting at 10.8 c/kWh" in out["custom"]
    assert "Exporting at 20.0 c/kWh" in out["higherBase"]
    assert "Exporting at 15.0 p/kWh" in out["gbp"]
    assert "Exporting" not in out["importing"]
    assert "Exporting" not in out["idle"]


def test_plan_missing_source_and_value_arrays_keep_legacy_meaning():
    out = _run_board(r"""
  const t = Date.now();
  const base = { timestamps: [new Date(t).toISOString()], charge_w: [4000], export_price: [0.1] };
  const cases = [undefined, [null], [''], ['bad'], [0], [2000], [25]];
  const rows = cases.map(grid_import_w => T.psSchedule({ schedule: { ...base, grid_import_w } }).rows[0]);
  console.log(JSON.stringify({ acts: rows.map(r => r.act), values: rows.map(r => r.exportValuePrice) }));
""")
    assert out["acts"] == ["charge", "charge", "charge", "charge", "solar", "charge", "charge"]
    assert out["values"] == [10] * 7


def test_solar_windows_do_not_merge_across_grid_charging():
    out = _run_board(r"""
  const rows = [0, 1, 2].map(i => ({ t: i * 1800000, act: i === 1 ? 'charge' : 'solar' }));
  console.log(JSON.stringify({ windows: T.psWindows(rows, 30, 'solar', 0).map(w => [w.start, w.end]) }));
""")
    assert out["windows"] == [[0, 1800000], [3600000, 5400000]]


def test_plan_exhausted_bonus_and_partial_interval_keep_total_export():
    out = _run_board(r"""
  const fixed = Date.parse('2026-10-31T18:15:00Z');
  context.Date = class extends Date {
    constructor(...args) { super(...(args.length ? args : [fixed])); }
    static now() { return fixed; }
  };
  const data = {
    daily_cost_breakdown: { zerohero: { status: 'enabled', bonus_export_cap_kwh: 15, bonus_export_kwh_remaining: 0 } },
    schedule: {
      timestamps: ['2026-10-31T18:00:00Z', '2026-10-31T18:30:00Z'],
      charge_w: [0, 0], discharge_w: [10000, 0], battery_consume_w: [0, 0], battery_export_w: [10000, 0],
      export_price: [0.02, 0.02], export_value_price: [0.02, null],
    },
  };
  const card = new T.PsPlanCard();
  card._hass = { states: {}, locale: { time_format: '24' }, config: { currency: 'AUD' } };
  card._config = { entities: {} }; card._api = () => data; card._loadHistory = () => {};
  card.querySelector = () => ({ addEventListener() {} });
  card._render(); const enabled = card.innerHTML;
  data.daily_cost_breakdown.zerohero.status = 'disabled';
  card._render(); const disabled = card.innerHTML;
  data.daily_cost_breakdown.zerohero = { status: 'enabled', bonus_export_cap_kwh: 0, bonus_export_kwh_remaining: 0 };
  card._render();
  console.log(JSON.stringify({ enabled, disabled, zero: card.innerHTML }));
""")
    assert "2.5 kWh" in out["enabled"] and "A$0.05" in out["enabled"]
    assert "0.0 kWh remaining today" in out["enabled"]
    assert "Super Export bonus cap" not in out["disabled"]
    assert "Super Export bonus cap" in out["zero"] and "0.0 kWh/day" in out["zero"]


def test_cached_api_payload_does_not_re_render_forever():
    """A resolved cache hit must not invalidate the card (render -> _api loop)."""
    out = _run_board(r"""
  let apiCalls = 0, renders = 0;
  const hass = {
    states: {}, locale: {}, config: {},
    callApi: async () => { apiCalls += 1; return { predicted_savings: 3 }; },
  };
  const card = new T.PsTodayCard();
  Object.defineProperty(card, 'isConnected', { value: true });
  const original = card._render.bind(card);
  card._render = () => { renders += 1; original(); };
  card.config = { entities: {} };
  card.hass = hass;
  setTimeout(() => {
    card.hass = { ...hass };
    setTimeout(() => console.log(JSON.stringify({ apiCalls, renders })), 50);
  }, 50);
""")
    assert out["apiCalls"] == 1
    # First render, one more when the payload lands; then nothing new.
    assert out["renders"] <= 3


def test_saved_custom_selection_initializes_shared_charge_discharge_duration():
    """Live installs keep the custom number at its minimum (1 minute); it must
    not replace the chosen duration unless the discharge select is Custom."""
    out = _run_board(r"""
  const calls = [];
  const st = (state, attributes = {}) => ({ state, attributes, last_updated: 'x' });
  const makeHass = (selectState, customValue) => ({
    states: {
      'sensor.power_sync_battery_mode': st('normal'),
      'select.power_sync_force_charge_duration': st('30', { options: ['5', '15', '30', '60'] }),
      'select.power_sync_force_discharge_duration': st(selectState, { options: ['5', '15', '30', 'Custom'] }),
      'number.power_sync_force_discharge_custom_duration': st(String(customValue), { min: 1, max: 1440, step: 1 }),
    },
    locale: {}, config: {},
    callApi: async () => null,
    callService: async (domain, service, data) => { calls.push([service, data]); },
  });
  const press = (card, kind) => {
    const evt = { target: { closest: sel => (sel === 'button[data-kind]' ? { dataset: { kind } } : null) } };
    card._onClick(evt); card._onClick(evt);
  };
  const entities = {
    mode: 'sensor.power_sync_battery_mode',
    chargeDuration: 'select.power_sync_force_charge_duration',
    dischargeDuration: 'select.power_sync_force_discharge_duration',
    customDischarge: 'number.power_sync_force_discharge_custom_duration',
  };
  const card = new T.PsControlsCard();
  card.config = { entities };
  card.hass = makeHass('5', 1);
  card._duration = 30;
  press(card, 'discharge');
  const customCard = new T.PsControlsCard();
  customCard.config = { entities };
  customCard.hass = makeHass('Custom', 90);
  press(customCard, 'discharge');
  press(customCard, 'charge');
  setTimeout(() => console.log(JSON.stringify({ calls })), 20);
""")
    assert out["calls"] == [
        ["force_discharge", {"duration": "30"}],
        ["force_discharge", {"duration": "90"}],
        ["force_charge", {"duration": "90"}],
    ]



def test_chart_sampling_respects_steps_gaps_and_negative_values():
    out = _run_board(r"""
  const pts = [[1, -2], [2, 0], [3, 4], [4, NaN]];
  const sample = (step, at) => T.psChartValue({ pts, step }, at);
  console.log(JSON.stringify({
    before: sample(true, 0), after: sample(false, 4),
    step: sample(true, 1.5), line: sample(false, 1.5),
    boundary: sample(true, 2), last: sample(true, 3),
    empty: T.psChartValue({ pts: [] }, 0),
  }));
""")
    assert out == dict(before=None, after=None, step=-2, line=-1,
                       boundary=0, last=4, empty=None)


def test_forecast_cards_show_greedy_inputs_without_claiming_planned_generation():
    out = _run_board(r"""
  const ts = [new Date(Date.now() + 300000).toISOString()];
  T.psApiCache.set('power_sync/optimization', { data: {
    forecast_series: { timestamps: ts, interval_minutes: 5,
      solar_forecast_values_kw: [4], load_forecast_values_kw: [2] },
  }});
  const rendered = {};
  for (const name of ['PsSolarLoadCard', 'PsLoadForecastCard']) {
    const card = new T[name]();
    card._hass = { states: {} }; card._config = { entities: {} };
    card._render(); rendered[name] = card.innerHTML;
  }
  console.log(JSON.stringify(rendered));
""")
    assert 'aria-label="Solar and load forecast for 48 hours"' in out["PsSolarLoadCard"]
    assert 'Solar, next 24 h' in out["PsLoadForecastCard"]
    assert '0.3<small>kWh' in out["PsLoadForecastCard"]


def test_battery_plan_labels_fit_without_overlapping_near_midnight():
    """#213: a valid midnight boundary must not overprint the start time."""
    out = _run_board(r"""
  class El {
    constructor(tag = '') {
      this.tag = tag; this.children = []; this.style = {}; this.attrs = {};
      this.clientHeight = 180;
      this.classList = { contains: name => this.className === name };
    }
    appendChild(el) { this.children.push(el); el.parentElement = this; }
    insertBefore(el) { this.appendChild(el); }
    remove() { this.parentElement.children = this.parentElement.children.filter(el => el !== this); }
    setAttribute(k, v) { this.attrs[k] = v; }
    querySelector(sel) { return this.children.find(e => '.' + e.className === sel); }
    getComputedTextLength() { return this.textContent.length * 6.3; }
    set textContent(value) { this._text = value; this.children = []; }
    get textContent() { return this._text || ''; }
  }
  context.document.createElement = tag => new El(tag);
  context.document.createElementNS = (_, tag) => new El(tag);
  const results = [];
  for (const width of [180, 320, 480]) {
    for (const format of ['12', '24']) {
      for (const minute of [0, 30, 45]) {
        class ChartDate extends Date {
          constructor(...args) { super(...(args.length ? args : [2026, 10, 2, 23, minute])); }
          static now() { return new ChartDate().getTime(); }
        }
        context.Date = ChartDate;
        const start = ChartDate.now();
        const ts = Array.from({length: 192}, (_, i) => new Date(start + i * 900000).toISOString());
        T.psApiCache.set('power_sync/optimization', { data: {schedule: {
          timestamps: ts, charge_w: ts.map(() => 3000), discharge_w: ts.map(() => 0),
        }}});
        const card = new T.PsBatteryPlanCard();
        card._hass = {locale: {language: 'en-US', time_format: format}};
        const svg = new El('svg'), parent = new El();
        svg.clientWidth = width; parent.appendChild(svg);
        card.querySelector = () => svg;
        card._draw();
        const labels = svg.children.filter(el => el.tag === 'text' && el.attrs.y === 176);
        const boxes = labels.map(el => {
          const w = el.getComputedTextLength(), anchor = el.attrs['text-anchor'];
          const left = Number(el.attrs.x) - (anchor === 'middle' ? w / 2 : anchor === 'end' ? w : 0);
          return {left, right: left + w, text: el.textContent};
        });
        let previous = null;
        const fits = boxes.every(b => {
          const valid = b.left >= 34 && b.right <= width - 10 && (!previous || b.left - previous.right >= 8);
          previous = b; return valid;
        });
        // Sparse axis labels must not remove the exact timestamp from tooltips.
        svg.onkeydown({key: 'ArrowRight', preventDefault() {}});
        const tip = svg.parentElement.querySelector('.graph-tooltip');
        results.push({width, format, minute, fits, count: boxes.length, boxes,
          tooltip: !tip.hidden && tip.innerHTML.includes('3.00 kW')});
        svg.clientWidth = width + 120;
        card._draw();
        results[results.length - 1].bars = svg.children.filter(el => el.tag === 'rect').length;
      }
    }
  }
  console.log(JSON.stringify({results}));
""")
    for sample in out["results"]:
        assert sample["fits"], sample
        assert sample["count"] >= (2 if sample["width"] >= 320 else 1), sample
        assert sample["tooltip"], sample
        assert sample["bars"] == 192, sample


def test_battery_plan_covers_48_hours_of_a_5_minute_plan():
    """A 5-minute LP plan must still chart the full 48 h, not the first 192 rows (16 h)."""
    out = _run_board(r"""
  class El {
    constructor(tag = '') {
      this.tag = tag; this.children = []; this.style = {}; this.attrs = {};
      this.clientWidth = 480; this.clientHeight = 180;
      this.classList = { contains: name => this.className === name };
    }
    appendChild(el) { this.children.push(el); el.parentElement = this; }
    insertBefore(el) { this.appendChild(el); }
    remove() { this.parentElement.children = this.parentElement.children.filter(el => el !== this); }
    setAttribute(k, v) { this.attrs[k] = v; }
    querySelector(sel) { return this.children.find(e => '.' + e.className === sel); }
    getComputedTextLength() { return this.textContent.length * 6.3; }
    set textContent(value) { this._text = value; this.children = []; }
    get textContent() { return this._text || ''; }
  }
  context.document.createElement = tag => new El(tag);
  context.document.createElementNS = (_, tag) => new El(tag);
  class ChartDate extends Date {
    constructor(...args) { super(...(args.length ? args : [2026, 10, 2, 6, 0])); }
    static now() { return new ChartDate().getTime(); }
  }
  context.Date = ChartDate;
  const start = ChartDate.now();
  // 48 h at 5-minute resolution; only the final 6 h discharge.
  const ts = Array.from({length: 576}, (_, i) => new Date(start + i * 300000).toISOString());
  T.psApiCache.set('power_sync/optimization', { data: {schedule: {
    timestamps: ts,
    charge_w: ts.map((_, i) => (i < 504 ? 3000 : 0)),
    discharge_w: ts.map((_, i) => (i < 504 ? 0 : 2000)),
  }}});
  const card = new T.PsBatteryPlanCard();
  card._hass = {locale: {language: 'en-US', time_format: '24'}};
  const svg = new El('svg'), parent = new El();
  parent.appendChild(svg);
  card.querySelector = () => svg;
  card._draw();
  const rects = svg.children.filter(el => el.tag === 'rect');
  // Discharge bars sit below the zero line, so they start where charge bars end.
  const zero = Number(rects[0].attrs.y) + Number(rects[0].attrs.height);
  const neg = rects.filter(el => Math.abs(Number(el.attrs.y) - zero) < 0.01);
  console.log(JSON.stringify({bars: rects.length, discharging: neg.length}));
""")
    assert out["bars"] == 192, out
    # The last 6 h (24 x 15-minute bars) are discharging and must be visible.
    assert out["discharging"] == 24, out


def test_every_small_chart_has_working_pointer_and_keyboard_tooltips():
    out = _run_board(r"""
  class El {
    constructor() {
      this.children = []; this.style = {}; this.attrs = {};
      this.clientWidth = 320; this.clientHeight = 180; this.offsetWidth = 180;
      this.classList = { contains: name => this.className === name };
    }
    appendChild(el) { this.children.push(el); el.parentElement = this; }
    insertBefore(el) { this.appendChild(el); }
    remove() { this.parentElement.children = this.parentElement.children.filter(el => el !== this); }
    setAttribute(k, v) { this.attrs[k] = v; }
    querySelector(sel) { return this.children.find(e => '.' + e.className === sel); }
    getBoundingClientRect() { return { left: 100, width: 640 }; }
  }
  context.document.createElement = () => new El();
  context.document.createElementNS = () => new El();
  // Use local evening in every timezone: the noon pointer must have history.
  class ChartDate extends Date {
    constructor(...args) { super(...(args.length ? args : [2026, 9, 30, 18, 0])); }
    static now() { return new ChartDate().getTime(); }
  }
  context.Date = ChartDate;
  const now = new ChartDate();
  const ts = Array.from({length: 97}, (_, i) => new Date(now.getTime() + i * 1800000).toISOString());
  T.psApiCache.set('power_sync/optimization', { data: {
    last_optimization: 'test', schedule: { timestamps: ts, charge_w: [3000, 0, 0], discharge_w: [0, 2000, 0] },
    forecast_series: { timestamps: ts, load_forecast_values_kw: ts.map(() => 2), planned_forecast_values_kw: ts.map(() => 0) },
  }});
  const results = {};
  for (const name of ['PsPricesCard', 'PsEnergyCard', 'PsLoadForecastCard', 'PsSolarLoadCard', 'PsBatteryPlanCard']) {
    const card = new T[name]();
    card._hass = { states: { 'sensor.x': { state: '1', attributes: { unit_of_measurement: 'kW' } } } };
    card._config = { entities: { importPrice: 'sensor.x', exportPrice: 'sensor.x', home: 'sensor.x', battery: 'sensor.x' } };
    card._hist = { 'sensor.x': [[new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime(), 1], [now.getTime(), 2]] };
    const svg = new El(), parent = new El(); parent.appendChild(svg);
    card.querySelector = () => svg;
    card._draw();
    const tip = svg.parentElement.querySelector('.graph-tooltip');
    const move = svg.onpointermove;
    // A scaled viewport must use the same chart coordinate as the plot.
    move({ clientX: 100 + 2 * 172 });
    const pointer = !tip.hidden && /kW|kWh/.test(tip.innerHTML) && !/undefined|NaN/.test(tip.innerHTML);
    svg.onpointerleave(); const leave = tip.hidden;
    svg.onkeydown({ key: 'ArrowRight', preventDefault() {} });
    const keyboard = !tip.hidden;
    svg.onkeydown({ key: 'Escape' }); const escape = tip.hidden;
    svg.onpointerdown({ clientX: 100 + 2 * 172 }); const tap = !tip.hidden;
    svg.onblur(); const blur = tip.hidden;
    // Repeated resize redraws retain one wrapper/tooltip and replace handlers.
    card._draw(); card._draw();
    const oneTip = svg.parentElement.children.filter(e => e.className === 'graph-tooltip').length === 1;
    svg.onpointermove({ clientX: 100 }); const outside = tip.hidden;
    results[name] = { pointer, leave, keyboard, escape, tap, blur, oneTip, outside, focusable: svg.attrs.tabindex === '0' };
  }
  // Negative and zero bars keep direction and real magnitude; empty data hides.
  const svg = new El(), parent = new El(); parent.appendChild(svg);
  T.psBars(svg, { vals: [-2, 0], y0: -3, y1: 3, times: ['first', 'second'] });
  svg.onpointerdown({ clientX: 100 + 2 * 60 });
  const tip = svg.parentElement.querySelector('.graph-tooltip');
  results.negativeBar = /Discharging/.test(tip.innerHTML) && /2.00 kW/.test(tip.innerHTML);
  T.psBars(svg, { vals: [], y0: -3, y1: 3 });
  svg.onpointerdown({ clientX: 100 + 2 * 60 });
  results.empty = tip.hidden;
  console.log(JSON.stringify(results));
""")
    assert out.pop('negativeBar') is True
    assert out.pop('empty') is True
    for card, checks in out.items():
        assert all(checks.values()), (card, checks)


def test_plan_current_control_follows_live_command_while_api_is_cached():
    """An accepted force command must not retain the cached self-consumption chip."""
    out = _run_board(r"""
  const now = Date.now(), step = 300000;
  const ts = [0, 1, 2].map(i => new Date(now - step + i * step).toISOString());
  const data = {enabled: true, current_action: 'self_consumption',
    effective_current_action: 'self_consumption', schedule: {
      timestamps: ts, soc: [0.8, 0.79, 0.78], charge_w: [0, 0, 0],
      discharge_w: [5000, 5000, 5000], battery_export_w: [5000, 5000, 5000],
      battery_consume_w: [0, 0, 0], import_price: [0.4, 0.4, 0.4], export_price: [0.1, 0.1, 0.1],
  }};
  const card = new T.PsPlanCard();
  card._config = {entities: {mode: 'sensor.mode'}};
  card._api = () => data;
  card._loadHistory = () => {};
  card.querySelector = () => ({addEventListener() {}});
  const samples = [];
  for (const mode of ['force_discharge', 'force_charge', 'hold_soc', 'self_consumption', 'normal', 'unavailable']) {
    for (const status of ['accepted', 'pending', 'restore_pending']) {
      card._hass = {states: {'sensor.mode': {state: mode, attributes: {
        control_source: 'optimizer', command_status: status, remaining_minutes: 138,
      }}}, config: {}, locale: {}};
      card._render();
      samples.push({mode, status, chip: card.innerHTML.match(/<span>Current control<\/span><b>(.*?)<\/b>/)?.[1]});
    }
  }
  console.log(JSON.stringify({samples}));
""")
    for sample in out["samples"]:
        mode, status, chip = sample["mode"], sample["status"], sample["chip"]
        if mode in ("normal", "unavailable"):
            assert chip == "Self-consumption"
        elif status == "restore_pending":
            assert chip == "Restoring…"
        elif status == "pending":
            assert chip == "Waiting for command acknowledgement"
        else:
            request = {"force_discharge": "Discharge", "force_charge": "Charge",
                       "hold_soc": "Battery hold", "self_consumption": "Self-consumption"}[mode]
            assert chip == f"{request} requested by the optimiser. 2 h 18 min left."


def test_plan_refreshes_effective_action_without_a_new_solve():
    out = _run_board(r"""
  const card = new T.PsPlanCard();
  card._config = {entities: {}};
  card._hass = {states: {}, config: {}, locale: {}};
  const data = {last_optimization: '2026-11-17T07:50:00Z', current_action: 'self_consumption', effective_current_action: 'self_consumption'};
  T.psApiCache.set('power_sync/optimization', {at: Date.now(), data, promise: null});
  const before = card._signature();
  data.effective_current_action = 'discharge';
  const after = card._signature();
  console.log(JSON.stringify({changed: before !== after}));
""")
    assert out["changed"] is True


def test_battery_cards_identify_command_source_without_inferring_from_optimizer():
    out = _run_board(r"""
  const samples = [];
  for (const mode of ['force_charge', 'force_discharge']) {
    for (const source of ['optimizer', 'user', 'manual', 'automation', 'aemo_spike', 'unknown', null, 'future_controller']) {
      for (const enabled of [true, false]) {
        const attributes = {command_status: 'accepted', remaining_minutes: 129};
        if (source !== null) attributes.control_source = source;
        const hass = {states: {'sensor.mode': {state: mode, attributes}}, locale: {}, config: {}};
        const data = {enabled, monitoring_mode: false, current_action: mode === 'force_charge' ? 'charge' : 'export'};
        const cards = [new T.PsNowCard(), new T.PsControlsCard()];
        for (const card of cards) {
          card._config = {entities: {mode: 'sensor.mode'}};
          card._hass = hass;
          card._api = () => data;
          card._render();
        }
        samples.push({mode, source, enabled, now: cards[0].innerHTML, controls: cards[1].innerHTML});
      }
    }
  }
  console.log(JSON.stringify({samples}));
""")
    origins = {
        "optimizer": ("Optimiser control", "by the optimiser"),
        "user": ("Manual override", "manually"),
        "manual": ("Manual override", "manually"),
        "automation": ("Automation control", "by an automation"),
        "aemo_spike": ("Price spike control", "by the price spike automation"),
    }
    for sample in out["samples"]:
        label, attribution = origins.get(
            sample["source"], ("Battery control active", "")
        )
        request = "Charge" if sample["mode"] == "force_charge" else "Discharge"
        message = f"{request} requested{f' {attribution}' if attribution else ''}."
        for card in ("now", "controls"):
            html = sample[card]
            assert label in html, sample
            assert message in html, sample
            assert "on your command" not in html, sample
            assert ("Manual override" in html) == (
                sample["source"] in ("user", "manual")
            ), sample
        assert "2 h 9 min left." in sample["controls"]
        assert "Returns to normal in" not in sample["now"]


def test_battery_card_source_changes_refresh_without_a_mode_change():
    out = _run_board(r"""
  const samples = [];
  for (const Card of [T.PsNowCard, T.PsControlsCard]) {
    const card = new Card();
    card._config = {entities: {mode: 'sensor.mode'}};
    card._api = () => null;
    const versions = [];
    for (const [index, source] of ['optimizer', 'user', null].entries()) {
      const attributes = {remaining_minutes: 15};
      if (source !== null) attributes.control_source = source;
      card._hass = {states: {'sensor.mode': {state: 'force_discharge', attributes, last_updated: String(index)}}, locale: {}, config: {}};
      card._render();
      versions.push({signature: card._signature(), html: card.innerHTML});
    }
    samples.push(versions);
  }
  console.log(JSON.stringify({samples}));
""")
    for versions in out["samples"]:
        assert len({v["signature"] for v in versions}) == 3
        for version, label in zip(
            versions, ("Optimiser control", "Manual override", "Battery control active")
        ):
            assert label in version["html"]


def test_battery_command_sources_preserve_starting_and_restoring_feedback():
    out = _run_board(r"""
  const samples = [];
  for (const Card of [T.PsNowCard, T.PsControlsCard]) {
    for (const mode of ['force_charge', 'force_discharge']) {
      for (const source of ['optimizer', 'user', 'automation', null]) {
        for (const status of ['pending', 'restore_pending']) {
          const card = new Card();
          card._config = {entities: {mode: 'sensor.mode'}};
          card._api = () => null;
          card._hass = {states: {'sensor.mode': {state: mode, attributes: {control_source: source, command_status: status, remaining_minutes: 129}}}, locale: {}, config: {}};
          card._render();
          samples.push({mode, source, status, html: card.innerHTML});
        }
      }
    }
  }
  console.log(JSON.stringify({samples}));
""")
    labels = {
        "optimizer": "Optimiser control", "user": "Manual override",
        "automation": "Automation control", None: "Battery control active",
    }
    for sample in out["samples"]:
        html = sample["html"]
        assert labels[sample["source"]] in html
        assert "2 h 9 min" not in html
        assert "requested" not in html
        if sample["status"] == "pending":
            direction = "charge" if sample["mode"] == "force_charge" else "discharge"
            assert f"Starting {direction}…" in html
            assert "Waiting for command acknowledgement" in html
        else:
            assert "Restoring" in html


def test_controls_distinguish_starting_restoring_and_acknowledged_requests():
    out = _run_board(r"""
  const card = new T.PsControlsCard();
  card.config = {entities: {mode: 'sensor.mode'}};
  card._api = () => null;
  const samples = [];
  for (const mode of ['force_charge', 'force_discharge']) {
    for (const status of ['pending', 'accepted', 'restore_pending']) {
      card._hass = {states: {'sensor.mode': {state: mode, attributes: {command_status: status, remaining_minutes: 15}}}};
      card._render();
      samples.push({mode, status, html: card.innerHTML});
    }
  }
  console.log(JSON.stringify({samples}));
""")
    for sample in out['samples']:
        html, status = sample['html'], sample['status']
        if status == 'pending':
            direction = 'charge' if sample['mode'] == 'force_charge' else 'discharge'
            assert f'Starting {direction}…' in html
            assert '15 min left.' not in html
        elif status == 'restore_pending':
            assert 'Restoring…' in html
            assert '15 min left.' not in html
        else:
            assert '15 min left.' in html


def test_now_card_shows_pending_command_status_instead_of_active_countdown():
    out = _run_board(r"""
  const card = new T.PsNowCard();
  card._config = {entities: {mode: 'sensor.mode'}};
  card._api = () => null;
  const samples = [];
  for (const mode of ['force_charge', 'force_discharge']) {
    for (const status of ['pending', 'restore_pending']) {
      card._hass = {states: {'sensor.mode': {state: mode, attributes: {command_status: status, remaining_minutes: 15}}}, locale: {}, config: {}};
      card._render();
      samples.push({mode, status, html: card.innerHTML});
    }
  }
  console.log(JSON.stringify({samples}));
""")
    for sample in out['samples']:
        html = sample['html']
        assert 'Returns to normal in' not in html
        if sample['status'] == 'restore_pending':
            assert 'Restoring normal operation…' in html
        else:
            direction = 'charge' if sample['mode'] == 'force_charge' else 'discharge'
            assert f'Starting {direction}…' in html


def test_right_now_does_not_read_step_end_as_battery_running_out():
    """The optimizer's current_action_end_time is the end of its 5-minute step.
    "Powering the home until 21:25" read as the battery running out; the card
    must state what is happening now and when the *plan* changes."""
    out = _run_board(r"""
  const now = Date.now();
  const step = 30 * 60000;
  const t0 = Math.floor(now / step) * step;
  const n = 12;
  const ts = Array.from({ length: n }, (_, i) => new Date(t0 + i * step).toISOString());
  const self = i => (i < 6 ? 1500 : 0);
  const data = {
    enabled: true, monitoring_mode: false,
    current_action: 'self_consumption',
    current_action_end_time: new Date(now + 5 * 60000).toISOString(),
    next_action: 'self_consumption', next_action_time: new Date(now + 5 * 60000).toISOString(),
    next_actions: [
      { action: 'self_consumption', timestamp: new Date(now + 5 * 60000).toISOString() },
      { action: 'charge', timestamp: new Date(t0 + 8 * step).toISOString() },
    ],
    schedule: {
      timestamps: ts, soc: Array(n).fill(0.6),
      charge_w: ts.map((_, i) => (i >= 8 ? 3000 : 0)),
      discharge_w: ts.map((_, i) => self(i)), battery_consume_w: ts.map((_, i) => self(i)),
      battery_export_w: Array(n).fill(0), ev_charging_w: Array(n).fill(0),
      import_price: Array(n).fill(0.3), export_price: Array(n).fill(0.05),
    },
  };
  T.psApiCache.set('power_sync/optimization', { at: Date.now(), data, promise: null });
  const st = (state, attributes = {}) => ({ state: String(state), attributes, last_updated: 'x' });
  const hass = {
    states: { 'sensor.soc': st(72), 'sensor.batt': st(1.9, { unit_of_measurement: 'kW' }), 'sensor.mode': st('normal') },
    locale: { time_format: '24' }, config: {}, callApi: async () => data,
  };
  const card = new T.PsNowCard();
  card._hass = hass;
  card._config = { entities: { soc: 'sensor.soc', battery: 'sensor.batt', mode: 'sensor.mode' } };
  card._render();
  const { rows, interval } = T.psSchedule(data);
  const runEnd = T.psPlannedRunEnd(rows, interval, 'self', now);
  console.log(JSON.stringify({ html: card.innerHTML, runEndHours: (runEnd - now) / 3600000, stepEnd: data.current_action_end_time }));
""")
    html = out["html"]
    headline = html.split("<h1>")[1].split("</h1>")[0]
    assert headline == "The battery is powering the home."
    assert "The plan keeps self-consumption until" in html
    # The plan runs about 3 hours, not to the 5-minute step end.
    assert 2.4 < out["runEndHours"] <= 3.0
    # A "next" action that repeats the current one is not announced; the
    # first real change from next_actions is.
    assert "Next it will power the home" not in html
    assert "Next it will charge at" in html


def test_self_consumption_heading_follows_measured_battery_flow():
    """Self-consumption permits solar charging; it does not prove discharge."""
    out = _run_board(r"""
  const samples = [];
  const st = (state, attributes = {}) => ({state: String(state), attributes});
  for (const mode of ['normal', 'self_consumption']) {
    for (const unit of ['W', 'kW']) {
    for (const power of [-10000, -100, 0, 100, 1900, 'unavailable']) {
      const card = new T.PsNowCard();
      card._config = {entities: {battery: 'sensor.battery', mode: 'sensor.mode', solar: 'sensor.solar', home: 'sensor.home', grid: 'sensor.grid'}};
      card._hass = {states: {
        'sensor.battery': st(typeof power === 'number' && unit === 'kW' ? power / 1000 : power, {unit_of_measurement: unit}),
        'sensor.mode': st(mode),
        'sensor.solar': st(11.5, {unit_of_measurement: 'kW'}),
        'sensor.home': st(0.4, {unit_of_measurement: 'kW'}),
        'sensor.grid': st(-1.1, {unit_of_measurement: 'kW'}),
      }, locale: {}, config: {}};
      card._api = () => ({enabled: true, current_action: 'self_consumption'});
      card._render();
      samples.push({mode, power, unit, headline: card.innerHTML.split('<h1>')[1].split('</h1>')[0]});
    }
    }
  }
  console.log(JSON.stringify({samples}));
""")
    for sample in out["samples"]:
        power = sample["power"]
        expected = (
            "Self-consumption is enabled." if power == "unavailable" else
            "The battery is charging." if power < -100 else
            "The battery is powering the home." if power > 100 else
            "The battery is idle."
        )
        assert sample["headline"] == expected, sample


def test_right_now_in_monitoring_describes_battery_and_planned_action():
    """In monitoring mode nothing executes, so effective_current_action is the
    safe default (self_consumption). Observed 2026-11-01: the board said "The
    battery is powering the home." while the battery charged at 7 kW and the
    plan wanted to charge. The headline must follow battery power and the lead
    must state the plan's intent."""
    out = _run_board(r"""
  const now = Date.now();
  const step = 30 * 60000;
  const t0 = Math.floor(now / step) * step;
  const n = 8;
  const ts = Array.from({ length: n }, (_, i) => new Date(t0 + i * step).toISOString());
  const data = {
    enabled: true, monitoring_mode: true,
    current_action: 'self_consumption',
    effective_current_action: 'self_consumption',
    planned_current_action: 'charge',
    next_action: 'charge', next_action_time: new Date(now + 5 * 60000).toISOString(),
    next_actions: [
      { action: 'charge', timestamp: new Date(now + 5 * 60000).toISOString() },
      { action: 'export', timestamp: new Date(t0 + 4 * step).toISOString() },
    ],
    schedule: {
      timestamps: ts, soc: Array(n).fill(0.6),
      charge_w: ts.map((_, i) => (i < 4 ? 7000 : 0)),
      discharge_w: Array(n).fill(0), battery_consume_w: Array(n).fill(0),
      battery_export_w: ts.map((_, i) => (i >= 4 ? 3000 : 0)), ev_charging_w: Array(n).fill(0),
      import_price: Array(n).fill(0.3), export_price: Array(n).fill(0.05),
    },
  };
  T.psApiCache.set('power_sync/optimization', { at: Date.now(), data, promise: null });
  const st = (state, attributes = {}) => ({ state: String(state), attributes, last_updated: 'x' });
  const card = new T.PsNowCard();
  card._config = { entities: { soc: 'sensor.soc', battery: 'sensor.batt', mode: 'sensor.mode' } };
  const html = {};
  for (const [name, kw] of [['charging', -7.0], ['discharging', 2.0], ['idle', 0.0]]) {
    card._hass = {
      states: { 'sensor.soc': st(60), 'sensor.batt': st(kw, { unit_of_measurement: 'kW' }), 'sensor.mode': st('normal') },
      locale: { time_format: '24' }, config: {}, callApi: async () => data,
    };
    card._render();
    html[name] = card.innerHTML;
  }
  data.monitoring_mode = false;
  card._hass = {
    states: { 'sensor.soc': st(60), 'sensor.batt': st(-7.0, { unit_of_measurement: 'kW' }), 'sensor.mode': st('normal') },
    locale: { time_format: '24' }, config: {}, callApi: async () => data,
  };
  card._render();
  html.controlling = card.innerHTML;
  console.log(JSON.stringify(html));
""")
    headline = lambda h: h.split("<h1>")[1].split("</h1>")[0]
    assert headline(out["charging"]) == "The battery is charging."
    assert headline(out["discharging"]) == "The battery is powering the home."
    assert headline(out["idle"]) == "The battery is idle."
    for name in ("charging", "discharging", "idle"):
        html = out[name]
        assert "The plan would charge now." in html
        # psNextChange compares against the planned action: the repeated
        # charge is skipped and the export is announced conditionally.
        assert "Next it would export at" in html
        assert "Next it would charge" not in html
        assert "Next it will" not in html
        assert "The plan keeps this going until" not in html
    # Outside monitoring the executed action still drives the card; its
    # self-consumption headline follows the measured flow.
    controlling = out["controlling"]
    assert headline(controlling) == "The battery is charging."
    assert "The plan would" not in controlling
    assert "Next it will charge at" in controlling


def test_settings_editor_patch_preserves_other_values_and_encodes_one_percent():
    out = _run_board(r"""
  const dialog = new T.PsSettingsDialog();
  dialog._fields = ['charge_by_time_target_soc', 'charge_by_time_target_time'];
  dialog._values = { charge_by_time_target_soc: 80, charge_by_time_target_time: '17:00', enabled: true };
  const inputs = { charge_by_time_target_soc: { value: '1' }, charge_by_time_target_time: { value: '17:00' } };
  dialog.querySelector = sel => inputs[sel.match(/name="(.*?)"/)[1]];
  const charge = dialog._changes();
  dialog._fields = ['max_grid_export_w', 'allow_grid_charge'];
  dialog._values = { max_grid_export_w: 5000, allow_grid_charge: true };
  inputs.max_grid_export_w = { value: '' };
  inputs.allow_grid_charge = { checked: false };
  console.log(JSON.stringify({ charge, grid: dialog._changes() }));
""")
    assert out["charge"] == {
        "patch": {"charge_by_time_target_soc": 0.01},
        "expected": {"charge_by_time_target_soc": 1},
    }
    assert out["grid"]["patch"] == {"max_grid_export_w": None, "allow_grid_charge": False}


def test_solar_editor_accepts_fractional_tariffs_and_saves_only_edited_fields():
    out = _run_board(r"""
  const dialog = new T.PsSettingsDialog();
  const keys = ['surplus_calculation', 'max_export_price_cents', 'household_buffer_kw'];
  dialog._fields = T.PS_SETTING_EDITORS.solar.fields.filter(key => keys.includes(key));
  dialog._tariffUnit = 'p/kWh';
  dialog._values = { surplus_calculation: 'grid_based', max_export_price_cents: 15, household_buffer_kw: 0.5 };
  const inputs = { surplus_calculation: 'direct', max_export_price_cents: '14.25', household_buffer_kw: '0.5' };
  dialog.querySelector = sel => ({ value: inputs[sel.match(/name="(.*?)"/)[1]] });
  const fields = Object.fromEntries(dialog._fields.map(key => [key, dialog._fieldHtml(key)]));
  console.log(JSON.stringify({ fields, patch: dialog._changes().patch }));
""")
    tariff = out["fields"]["max_export_price_cents"]
    assert "p/kWh" in tariff
    assert 'step="any"' in tariff
    assert '<option value="direct"' in out["fields"]["surplus_calculation"]
    assert out["patch"] == {"surplus_calculation": "direct", "max_export_price_cents": 14.25}


def test_settings_save_requires_successful_write_and_matching_readback():
    out = _run_board(r"""
  (async () => {
    const results = {};
    for (const mode of ['success', 'rejected', 'stale', 'offline']) {
      const dialog = new T.PsSettingsDialog();
      const calls = [], busy = [], errors = [];
      let closed = false;
      dialog._editor = T.PS_SETTING_EDITORS.charge_by_time;
      dialog.isConnected = true;
      dialog._dialog = { close: () => { closed = true; } };
      dialog.querySelector = sel => sel === 'form' ? { reportValidity: () => true } : {};
      dialog._changes = () => ({ patch: { charge_by_time_target_soc: 0.01 }, expected: { charge_by_time_target_soc: 1 } });
      dialog._busy = value => { dialog._saving = value; busy.push(value); };
      dialog._error = message => errors.push(message);
      dialog._hass = { callApi: async (method, path, data) => {
        calls.push({ method, path, data });
        if (mode === 'offline') throw Error('Connection lost');
        if (method === 'POST') return mode === 'rejected' ? { success: false, error: 'Access denied' } : { success: true };
        return { success: true, config: { charge_by_time_target_soc: mode === 'stale' ? 80 : 1 } };
      } };
      await dialog._save();
      results[mode] = { calls, busy, errors, closed };
    }
    console.log(JSON.stringify(results));
  })().catch(err => { console.error(err); process.exit(1); });
""")
    assert out["success"]["closed"] is True
    assert out["success"]["errors"] == []
    assert [c["method"] for c in out["success"]["calls"]] == ["POST", "GET"]
    assert out["success"]["calls"][0]["data"] == {"charge_by_time_target_soc": 0.01}
    for mode in ("rejected", "stale", "offline"):
        assert out[mode]["closed"] is False
        assert out[mode]["errors"]
        assert out[mode]["busy"] == [True, False]
    assert len(out["rejected"]["calls"]) == 1


def test_settings_toggle_keeps_entity_index_when_an_earlier_row_is_missing():
    out = _run_board(r"""
  const card = new T.PsSettingsCard();
  card._config = { rows: [
    { kind: 'switch', entity: 'switch.missing', label: 'Missing' },
    { kind: 'switch', entity: 'switch.charge', label: 'Charge by time', editor: 'charge_by_time' },
  ] };
  card._hass = { states: { 'switch.charge': { state: 'on' } } };
  card._render();
  const calls = [];
  card._call = (...args) => calls.push(args);
  const btn = { dataset: { i: '1' }, getAttribute: () => 'true', setAttribute() {} };
  card._onClick({ target: { closest: sel => sel === 'button[data-i]' ? btn : null } });
  console.log(JSON.stringify({ html: card.innerHTML, calls }));
""")
    assert 'data-editor="1"' in out["html"]
    assert 'aria-haspopup="dialog"' in out["html"]
    assert out["calls"] == [["homeassistant", "turn_off", {"entity_id": "switch.charge"}]]


def test_solar_configuration_stays_in_existing_ev_mode_without_changing_session_policy():
    script = (
        "const fs = require('fs'); const vm = require('vm');"
        + BROWSER_STUBS
        + "vm.createContext(context);"
        + f"vm.runInContext(fs.readFileSync({json.dumps(str(STRATEGY_PATH))}, 'utf8'), context);"
        + r"""
  const Panel = vm.runInContext('PowerSyncEVPanel', context);
  const panel = new Panel();
  panel.getRootNode = () => ({ host: { localName: 'power-sync-board' } });
  panel._data = { solarConfig: { enabled: false, household_buffer_kw: 0.5 }, priceSettings: {}, scheduledSettings: {}, autoStatus: { settings: {} } };
  const dashboard = panel._modeSectionsHtml();
  let event;
  panel.dispatchEvent = value => { event = value; };
  const opener = {};
  panel._openSolarSettings(opener);
  const calls = [];
  panel._selectedLoadpoint = () => ({ loadpoint_id: 'car' });
  panel._runCommand = (...args) => calls.push(args);
  panel._startPolicy();
  panel.getRootNode = () => ({ host: { localName: 'classic-layout' } });
  const classic = panel._modeSectionsHtml();
  console.log(JSON.stringify({ dashboard, classic, event, calls }));
"""
    )
    out = _run(script)
    assert 'data-action="configure-solar"' in out["dashboard"]
    assert 'data-setting="solar:enabled"' in out["dashboard"]
    assert 'data-setting="solar:household_buffer_kw"' not in out["dashboard"]
    assert 'data-setting="solar:household_buffer_kw"' in out["classic"]
    assert out["event"]["type"] == "ps-settings-open"
    assert out["event"]["detail"]["row"]["editor"] == "solar"
    assert out["event"]["composed"] is True
    assert len(out["calls"]) == 1
    assert out["calls"][0][2]["command"] == "start_policy_charging"
    assert out["calls"][0][2]["policy"] == "solar_only"


def test_custom_duration_plus_moves_one_minute():
    out = _run_board(r"""
  const card = new T.PsControlsCard(), calls = [];
  card.config = {entities: {customDischarge: 'number.custom', dischargeDuration: 'select.duration'}};
  card.hass = {states: {
    'number.custom': {state: '1', attributes: {min: 1, max: 1440, step: 1}},
    'select.duration': {state: 'Custom', attributes: {options: ['30', 'Custom']}},
  }, callService: async (domain, service, data) => calls.push(data)};
  card._onClick({target: {closest: sel => sel === 'button[data-step]' ? {dataset: {step: 'custom', d: '1'}} : null}});
  setTimeout(() => console.log(JSON.stringify({calls})), 0);
""")
    assert out["calls"] == [{"entity_id": "number.custom", "value": 2}]


def test_custom_editor_applies_to_charge_discharge_but_not_hold_and_clears_confirmation():
    out = _run_board(r"""
  const calls = [], card = new T.PsControlsCard();
  card.config = {entities: {chargeDuration: 'select.charge', forcePower: 'number.power'}};
  card.hass = {states: {'select.charge': {state: '30', attributes: {options: ['30']}}, 'number.power': {state: '2', attributes: {step: .05}}},
    config: {time_zone: 'Australia/Brisbane'}, callService: async (domain, service, data) => calls.push([service, data])};
  const click = (selector, dataset) => card._onClick({target: {closest: sel => sel === selector ? {dataset} : null}});
  const press = kind => click('button[data-kind]', {kind});
  click('[data-custom-toggle]', {});
  card._onTimingInput({target: {dataset: {timingInput: 'hours'}, value: '1'}});
  card._onTimingInput({target: {dataset: {timingInput: 'minutes'}, value: '17'}});
  press('charge');
  card._onTimingInput({target: {dataset: {timingInput: 'minutes'}, value: '18'}});
  press('charge');
  const beforeConfirm = calls.length;
  press('charge'); press('discharge'); press('discharge'); press('hold'); press('hold');
  card._onTimingInput({target: {dataset: {timingInput: 'minutes'}, value: ''}});
  press('charge'); press('charge');
  const invalidCalls = calls.length;
  click('button[data-min]', {min: '30'}); press('charge'); press('charge');
  setTimeout(() => console.log(JSON.stringify({calls, beforeConfirm, invalidCalls})), 0);
""")
    assert out["beforeConfirm"] == 0
    assert out["invalidCalls"] == 3
    assert out["calls"] == [
        ["force_charge", {"duration": "78", "power_w": 2000}],
        ["force_discharge", {"duration": "78", "power_w": 2000}],
        ["hold_battery_soc", {"duration": "30"}],
        ["force_charge", {"duration": "30", "power_w": 2000}],
    ]


def test_until_preview_matches_ha_timezone_instead_of_browser_timezone():
    out = _run_board(r"""
  const sample = (zone, now, until) => T.psUntilPreview(until, zone, Date.parse(now));
  console.log(JSON.stringify({
    today: sample('Australia/Brisbane', '2026-10-31T08:00:20Z', '18:30'),
    tomorrow: sample('Australia/Brisbane', '2026-10-31T13:50:00Z', '00:10'),
    equal: sample('Australia/Brisbane', '2026-10-31T08:30:00Z', '18:30'),
    fold: sample('America/New_York', '2026-11-01T05:45:00Z', '01:30'),
    gap: sample('America/New_York', '2026-03-08T07:00:00Z', '02:30'),
    long: sample('America/New_York', '2026-11-01T04:00:00Z', '00:00'),
    invalid: sample('Australia/Brisbane', '2026-10-31T08:00:00Z', '24:00'),
    nozone: sample(null, '2026-10-31T08:00:00Z', '18:30'),
  }));
""")
    assert out["today"]["minutes"] == 30
    assert "today" in out["today"]["label"]
    assert out["tomorrow"]["minutes"] == 20
    assert "tomorrow" in out["tomorrow"]["label"]
    assert out["equal"]["minutes"] == 1440
    assert out["fold"]["minutes"] == 45
    assert out["gap"]["minutes"] == 1410
    assert all("error" in out[key] for key in ("long", "invalid", "nozone"))


def test_until_sends_clock_time_only_and_power_changes_rearm_confirmation():
    out = _run_board(r"""
  const calls = [], card = new T.PsControlsCard();
  card.config = {entities: {forcePower: 'number.power'}};
  card.hass = {states: {'number.power': {state: '1'}}, config: {time_zone: 'Australia/Brisbane'},
    callService: async (domain, service, data) => calls.push([service, data])};
  card._customEnabled = true; card._timingMode = 'until'; card._until = '18:30';
  const press = kind => card._onClick({target: {closest: sel => sel === 'button[data-kind]' ? {dataset: {kind}} : null}});
  press('charge');
  card.hass.states['number.power'].state = '2';
  press('charge');
  const beforeConfirm = calls.length;
  press('charge'); press('discharge'); press('discharge');
  setTimeout(() => console.log(JSON.stringify({calls, beforeConfirm})), 0);
""")
    assert out["beforeConfirm"] == 0
    assert out["calls"] == [
        ["force_charge", {"until": "18:30", "power_w": 2000}],
        ["force_discharge", {"until": "18:30", "power_w": 2000}],
    ]


def test_classic_controls_keep_custom_charge_duration_and_finish_time_entry():
    script = (
        "const fs = require('fs'); const vm = require('vm');" + BROWSER_STUBS
        + f"vm.createContext(context); vm.runInContext(fs.readFileSync({json.dumps(str(STRATEGY_PATH))}, 'utf8'), context);"
        + r"""
  const states = {
    'number.power_sync_force_discharge_custom_duration': {state: '77'},
    'select.power_sync_force_discharge_duration': {state: 'Custom'},
    'select.power_sync_force_charge_duration': {state: '30'},
  };
  context.hass = {states};
  const controls = vm.runInContext('_batteryControls(hass)', context);
  const cards = controls.cards.flatMap(c => c.cards || [c]);
  const charge = cards.find(c => c.tap_action?.service === 'power_sync.force_charge');
  const duration = new Function('states', charge.tap_action.data.duration.slice(3, -3))(states);
  console.log(JSON.stringify({duration, until: cards.some(c => c.type === 'custom:power-sync-until-controls'),
    editable: cards.some(c => c.type === 'entities' && c.entities?.some(e => e.entity === 'number.power_sync_force_discharge_custom_duration'))}));
"""
    )
    out = _run(script)
    assert out == {"duration": "77", "until": True, "editable": True}


def test_plan_history_distinguishes_solar_and_keeps_unknown_sources_generic():
    out = _run_board(r"""
  const now = Date.now(), base = now - 2 * 3600000;
  const id = name => `sensor.${name}`;
  const cases = [
    [-2000, 0, 3000], [-2000, -500, 3000], [-2000, 300, 1800],
    [-2000, 301, 3000], [-2000, 1500, 3000], [-2000, 0, 0],
    [-2000, 0, 1000], [-2000, NaN, 3000], [-2000, 0, NaN],
    [NaN, 0, 3000], [2000, -1000, 3000], [2000, 0, 0], [0, 0, 3000],
  ];
  const results = {};
  for (const unit of ['W', 'kW']) {
    const card = new T.PsPlanCard();
    card._config = { entities: { battery: id('battery'), grid: id('grid'), solar: id('solar') } };
    card._hass = { states: Object.fromEntries(['battery', 'grid', 'solar'].map(name => [id(name), {attributes: {unit_of_measurement: unit}}])) };
    card._hist = Object.fromEntries(['battery', 'grid', 'solar'].map((name, j) => [id(name), cases.map((c, i) => [base + i * 300000, c[j] / (unit === 'kW' ? 1000 : 1)])]));
    const model = card._buildModel({schedule: {}});
    const midnight = new Date(new Date().getFullYear(), new Date().getMonth(), new Date().getDate()).getTime();
    results[unit] = cases.map((_, i) => model.act((base + i * 300000 - midnight) / 3600000));
  }
  console.log(JSON.stringify(results));
""")
    expected = ["solar", "solar", "solar", "charge", "charge", "charge",
                "charge", "charge", "charge", "", "export", "self", "hold"]
    assert out == {"W": expected, "kW": expected}


def test_plan_requests_solar_history_and_preserves_unavailable_gaps():
    out = _run_board(r"""
  (async () => {
    const card = new T.PsPlanCard();
    card._config = {entities: {solar: 'sensor.solar', battery: 'sensor.battery', grid: 'sensor.grid'}};
    let requested;
    card._hass = {states: {}, callApi: async (_, path) => {
      requested = path;
      return [[
        {entity_id: 'sensor.solar', last_changed: '2026-11-06T00:00:00Z', state: '3000'},
        {last_changed: '2026-11-06T00:05:00Z', state: 'unavailable'},
        {last_changed: '2026-11-06T00:10:00Z', state: null},
      ]];
    }};
    card._invalidate = () => {};
    card._loadHistory();
    await new Promise(resolve => setTimeout(resolve, 0));
    console.log(JSON.stringify({requested, values: card._hist['sensor.solar'].map(p => p[1])}));
  })();
""")
    assert "sensor.solar" in out["requested"]
    assert out["values"] == [3000, None, None]


def test_shared_charger_selector_and_vehicle_editor_send_profile_scoped_payloads():
    script = (
        "const fs = require('fs'); const vm = require('vm');"
        + BROWSER_STUBS
        + "vm.createContext(context);"
        + f"vm.runInContext(fs.readFileSync({json.dumps(str(STRATEGY_PATH))}, 'utf8'), context);"
        + r"""
  (async () => {
    const Panel = vm.runInContext('PowerSyncEVPanel', context);
    const panel = new Panel();
    panel._data = {sharedCharger: {available: true, enabled: true, selection_allowed: true,
      selected_vehicle_id: 'byd', profiles: [
        {id: 'byd', name: 'BYD <ATTO>', soc_entity: 'sensor.byd_soc', battery_capacity_kwh: 60},
        {id: 'zeekr', name: 'Zeekr', soc_entity: 'sensor.zeekr_soc', battery_capacity_kwh: 86}
      ]}};
    const html = panel._sharedChargerHtml();
    const canStartSelected = panel._canStart({loadpoint_id: 'generic_ev', connected: true});
    panel._data.sharedCharger.selected_vehicle_id = null;
    const canStartUnselected = panel._canStart({loadpoint_id: 'generic_ev', connected: true});
    panel._data.sharedCharger.selected_vehicle_id = 'byd';
    const calls = [];
    panel._runCommand = (...args) => calls.push(args);
    panel.shadowRoot.querySelector = selector => ({value: {
      '[data-shared-name]': 'Zeekr', '[data-shared-soc]': 'sensor.zeekr_soc',
      '[data-shared-capacity]': '86'
    }[selector]});
    await panel._saveSharedVehicle(true);
    await panel._saveSharedVehicle(false);
    panel._data.sharedCharger.selection_allowed = false;
    panel._data.sharedCharger.selection_block_reason = 'Stop charging first';
    const blocked = panel._sharedChargerHtml();
    console.log(JSON.stringify({html, calls, blocked, canStartSelected, canStartUnselected}));
  })();
"""
    )
    out = _run(script)
    assert out["canStartSelected"] is True and out["canStartUnselected"] is False
    assert 'value="byd" selected' in out["html"]
    assert 'value="zeekr"' in out["html"]
    assert "BYD &lt;ATTO&gt;" in out["html"]
    assert out["calls"][0][2] == {
        "operation": "save", "vehicle_id": None, "name": "Zeekr",
        "soc_entity": "sensor.zeekr_soc", "battery_capacity_kwh": 86,
    }
    assert out["calls"][1][2]["vehicle_id"] == "byd"
    assert 'data-connected-vehicle disabled' in out["blocked"]
    assert 'Stop charging first' in out["blocked"]


def test_native_inverter_controls_confirm_and_route_without_button_card():
    out = _run_board(r"""
      (async () => {
        const calls = [], toasts = [];
        const card = new T.PsInverterControlsCard();
        card._config = {entities: {inverter: 'sensor.power_sync_inverter_status'}};
        card._hass = {states: {'sensor.power_sync_inverter_status': {state: 'online'}},
          callService: async (...args) => { calls.push(args); }};
        card._toast = text => toasts.push(text);
        const click = action => card._onClick({target: {closest: () => ({dataset: {inverterAction: action}})}});
        card._render();
        const html = card.innerHTML;
        for (const action of ['load_following', 'shutdown', 'restore']) {
          const before = calls.length;
          await click(action);
          if (calls.length !== before) throw Error('first tap dispatched');
          await click(action);
        }
        for (const state of ['unavailable', 'unknown', 'offline', 'error']) {
          card._hass.states['sensor.power_sync_inverter_status'].state = state;
          await click('shutdown'); await click('shutdown');
        }
        card.disconnectedCallback();
        console.log(JSON.stringify({calls, toasts, html}));
      })();
    """)
    assert out['calls'] == [
        ['power_sync', 'curtail_inverter', {'mode': 'load_following'}],
        ['power_sync', 'curtail_inverter', {'mode': 'shutdown'}],
        ['power_sync', 'restore_inverter', {}],
    ]
    assert 'Load Follow' in out['html'] and '>Stop<' in out['html'] and '>Restore<' in out['html']
    assert all('Request sent' in message for message in out['toasts'])


def test_strategy_uses_native_inverter_controls():
    script = _strategy_script(board_missing=False).replace(
        "'sensor.power_sync_battery_level': s(69,", "'sensor.power_sync_inverter_status': s('online'), 'sensor.power_sync_battery_level': s(69,"
    ).replace("type: top.type,", "inverter: cards.find(c => c.key === 'acinv'), type: top.type,")
    result = subprocess.run(['node', '-e', script, str(STRATEGY_PATH)], check=True, capture_output=True, text=True)
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out['inverter']['native'] == 'ps-inverter-controls-card'
    assert out['inverter']['config']['entities']['inverter'] == 'sensor.power_sync_inverter_status'


def test_native_inverter_controls_serialize_requests_and_report_failure():
    out = _run_board(r"""
      (async () => {
        let release, calls = 0;
        const messages = [];
        const card = new T.PsInverterControlsCard();
        card._config = {entities: {inverter: 'sensor.inverter'}};
        card._hass = {states: {'sensor.inverter': {state: 'online'}},
          callService: () => { calls++; return new Promise((resolve, reject) => { release = () => reject(Error('offline')); }); }};
        card._toast = text => messages.push(text);
        const click = () => card._onClick({target: {closest: () => ({dataset: {inverterAction: 'shutdown'}})}});
        await click(); const pending = click();
        await click(); await click();
        const during = calls;
        release(); await pending;
        console.log(JSON.stringify({calls, during, messages, busy: card._busy, confirm: card._confirm}));
      })();
    """)
    assert out['calls'] == out['during'] == 1
    assert out['busy'] is None and out['confirm'] is None
    assert len(out['messages']) == 1 and 'offline' in out['messages'][0]
    assert 'Request sent' not in out['messages'][0]


def test_right_now_weather_chip_summarizes_today_and_degrades():
    """Right now shows today's weather: high/low, dawn-to-dusk cloud cover and
    daylight hours. Missing forecast fields must say why, and no payload (or a
    failed one) must hide the chip rather than show an empty control."""
    out = _run_board(r"""
  const at = (h, m = 0) => { const d = new Date(); d.setUTCHours(h, m, 0, 0); return d.toISOString(); };
  const full = {
    success: true, date: new Date().toISOString().slice(0,10), source: 'weather.forecast_home', condition: 'partlycloudy',
    temperature: 27.2, temperature_unit: '°C', high: 29.4, low: 19.1, high_low_source: 'daily',
    cloud_cover_avg: 38, cloud_cover_complete: true, cloud_cover_from: at(4),
    cloud_cover_hours: Array.from({ length: 15 }, (_, i) => ({ time: at(4 + i), cloud: i === 7 ? 70 : 30 })),
    dawn: at(4, 28), sunrise: at(4, 52), sunset: at(18, 12), dusk: at(18, 37), daylight_minutes: 849,
    fetched_at: at(11, 20),
  };
  const st = (state, attributes = {}) => ({ state: String(state), attributes, last_updated: 'x' });
  const card = new T.PsNowCard();
  card._config = { entities: { soc: 'sensor.soc', battery: 'sensor.batt', mode: 'sensor.mode' } };
  const render = (wx, open = false) => {
    if (wx === undefined) T.psApiCache.delete('power_sync/weather/today');
    else T.psApiCache.set('power_sync/weather/today', { at: Date.now(), data: wx, promise: null });
    card._wxOpen = open;
    card._hass = {
      states: { 'sensor.soc': st(60), 'sensor.batt': st(-3, { unit_of_measurement: 'kW' }), 'sensor.mode': st('normal') },
      locale: { time_format: '24' }, config: {}, callApi: async () => null,
    };
    card._render();
    return card.innerHTML;
  };
  const html = {
    full: render(full), open: render(full, true),
    yesterday: render({...full, date:'2000-01-01'}),
    stale: render({...full, forecast_stale:true}),
    hourly: render({...full, high_low_source:'hourly'}),
    nocloud: render({ ...full, cloud_cover_avg: null, cloud_cover_hours: [], cloud_cover_complete: false }),
    partial: render({ ...full, cloud_cover_complete: false, cloud_cover_from: at(11), cloud_cover_hours: full.cloud_cover_hours.slice(7) }),
    sunOnly: render({ ...full, source: 'sun.sun', condition: null, temperature: null, high: null, low: null,
      cloud_cover_avg: null, cloud_cover_hours: [] }),
    failed: render({ success: false, error: 'boom' }),
    empty: render({ success: true, temperature: null, high: null, low: null, daylight_minutes: null }),
  };
  card._config.weather = false;
  html.optOut = render(full);
  console.log(JSON.stringify(html));
""")
    full = out["full"]
    assert 'class="wx-chip"' in full and 'aria-expanded="false"' in full
    assert 'id="ps-wx-pop" role="dialog" aria-label="Today’s weather" hidden' in full
    assert ">27°<" in full and "H <b>29°</b> L <b>19°</b>" in full
    assert '<span class="hi">29°</span><span class="sep">/</span><span class="lo">19°</span>' in full
    assert "38<small>%</small>" in full and "Dawn to dusk avg" in full
    assert "14<small>h</small> 9<small>m</small>" in full
    assert "Peak <b class=\"num\">70%</b> at <b class=\"num\">11:00</b>" in full
    assert "Partly cloudy" in full and "<code>weather.forecast_home</code>" in full
    assert 'aria-label="Daylight from 04:28 to 18:37"' in full
    assert "sunrise 04:52 · sunset 18:12" in full
    # The mode pill still renders beside the chip.
    assert 'class="now-top"' in full

    assert 'aria-expanded="true"' in out["open"]
    assert 'aria-label="Today’s weather" hidden' not in out["open"]

    assert "Not in forecast" in out["nocloud"] and "Cloud by hour" not in out["nocloud"]
    assert "Partial avg from 11:00" in out["partial"]
    assert 'class="wx-clouds-x"><span>04:00</span><span>11:30</span><span>19:00</span>' in out["partial"]
    sun_only = out["sunOnly"]
    assert "Needs a weather entity" in sun_only and "No weather forecast" in sun_only
    assert ">14 h 9 m<" in sun_only and ">daylight<" in sun_only

    assert "Retained forecast values" in out["stale"]
    assert "Available hourly forecast" in out["hourly"]
    for name in ("failed", "empty", "optOut", "yesterday"):
        assert "wx-chip" not in out[name], name


def test_today_average_labels_net_home_energy_cost_not_battery_acquisition():
    """Ticket456: free battery charging can coexist with supply-inclusive cost."""
    out = _run_board(r"""
  const card = new T.PsTodayCard();
  const state = (value, attributes = {}) => ({ state: String(value), attributes });
  card.config = { entities: {
    avgCost: 'sensor.avg', mtdAvgCost: 'sensor.mtd',
    importCost: 'sensor.cost', gridImport: 'sensor.import', netCost: 'sensor.net',
  } };
  card.hass = {
    states: {
      'sensor.avg': state(0.099, { unit_of_measurement: 'AUD/kWh' }),
      'sensor.mtd': state(1.62, { unit_of_measurement: 'AUD/kWh' }),
      'sensor.cost': state(0.01),
      'sensor.import': state(2.5, { unit_of_measurement: 'kWh' }),
      'sensor.net': state(1.35, { supply_charge: 1.34 }),
    }, config: { currency: 'AUD' }, locale: {}, callApi: async () => null,
  };
  card._render();
  console.log(JSON.stringify({ html: card.innerHTML }));
""")
    assert 'Average net energy cost' in out['html']
    assert 'Net cost per kWh used at home' in out['html']
    assert 'Average import cost' not in out['html']
    assert '9.9' in out['html']
    assert 'Month to date 162.0 c' in out['html']


@pytest.mark.parametrize("board_missing", [False, True])
@pytest.mark.parametrize("admin", [False, True])
def test_dashboard_has_one_admin_support_card(board_missing, admin):
    out = _run_strategy(board_missing, admin=admin)
    assert (out["legacySupport"] if board_missing else out["keys"].count("support")) == int(admin)
    assert "diagnostics" not in out["keys"]
    assert "ps-diagnostics-card" not in out["natives"]
    if not board_missing:
        assert ("ps-support-card" in out["natives"]) == admin


def test_advanced_battery_context_switch_discards_stale_drafts_reads_and_mutations():
    result = _run_board(r'''
(async () => {
  const Control = registry.get('power-sync-advanced-controls');
  context.crypto = {}; // HTTP Home Assistant has no crypto.randomUUID.
  const card = new Control();
  let reads = [];
  card.hass = {callApi: (method, path) => new Promise(resolve => reads.push({path, resolve}))};
  card.entryId = 'a'; card._draft.power_w = '9000';
  const oldRead = card._refresh();
  card.entryId = 'b';
  const freshDraft = card._draft.power_w === '1800';
  reads.forEach(r => r.resolve({schema_version:1, from:'a'}));
  await oldRead;
  const staleReadIgnored = !card._output && !card._limits;
  const posts = [];
  const status = {schema_version:1, control_revision:'revision', capabilities:{battery_output:{supported:true}}, operation:null};
  card.hass = {callApi: (method,path,body) => method === 'POST' ? new Promise(resolve => posts.push({body,resolve})) : Promise.resolve(path.includes('native_site_limits') ? {schema_version:0} : status)};
  card.entryId = 'a'; card._output = status;
  await card._act('target'); const oldWrite = card._act('target');
  card.entryId = 'b'; card._output = status;
  await card._act('target'); const newWrite = card._act('target');
  if (posts.length !== 2) throw Error('New site could not start its independently confirmed request');
  if (!posts.every(p => /^[A-Za-z0-9._:-]{1,80}$/.test(p.body.request_id)) || posts[0].body.request_id === posts[1].body.request_id) throw Error('Invalid or duplicate HTTP-compatible request IDs');
  posts[0].resolve({success:true}); await oldWrite;
  const newRequestStillBusy = card._busy;
  posts[1].resolve({success:true}); await newWrite;
  console.log(JSON.stringify({freshDraft,staleReadIgnored,newRequestStillBusy,entries:posts.map(p=>p.body.entry_id)}));
})().catch(e=>{console.error(e);process.exitCode=1});
''')
    assert result == {'freshDraft': True, 'staleReadIgnored': True, 'newRequestStillBusy': True, 'entries': ['a', 'b']}


def test_advanced_controls_send_zero_and_soc_without_legacy_force_or_paperwork():
    out = _run_board(r"""
      (async () => {
        const c = new (context.customElements.get('power-sync-advanced-controls'))();
        c.entryId = 'entry-one';
        const output = {schema_version:1, control_revision:'rev', capabilities:{battery_output:{supported:true,zero_hold_supported:true,target_soc_supported:true}}};
        const limits = {schema_version:1, capabilities:{writable:true}, limits:{import_limit_w:{value_w:10000},export_limit_w:{value_w:5000}}, configuration_revision:'cfg', snapshots:[]};
        const posts = [];
        c.hass = {callApi: async (method,path,body) => { if(method === 'POST') {posts.push({path,body}); return {};} return path.includes('native_site_limits') ? limits : path.includes('backup_preparation') ? {schema_version:1,capabilities:{writable:true}} : output; }};
        await c._act('refresh'); const html = c.innerHTML;
        c._draft.mode='idle'; await c._act('target'); await c._act('target');
        c._draft.mode='charge'; c._draft.target_soc='80'; await c._act('target'); await c._act('target');
        c._draft.mode='discharge'; c._draft.target_soc='40'; await c._act('target'); await c._act('target');
        c._draft.import_limit_w='12000'; await c._act('set'); await c._act('set');
        console.log(JSON.stringify({posts,html}));
      })().catch(e=>{console.error(e);process.exitCode=1});
    """)
    posts = out['posts']
    assert [p['body']['power_w'] for p in posts[:3]] == [0, -1800, 1800]
    assert 'target_soc' not in posts[0]['body']
    assert [p['body']['target_soc'] for p in posts[1:3]] == [80, 40]
    assert all(p['body']['entry_id'] == 'entry-one' for p in posts)
    assert posts[3]['body']['import_limit_w'] == 12000
    assert all('force' not in p['path'] for p in posts)
    assert 'Battery power and targets' in out['html'] and 'Advanced battery controls' in out['html'] and 'Site limits' in out['html']
    assert 'Approval document' not in out['html'] and 'Approved installation' not in out['html']


def test_advanced_battery_remembers_only_the_last_successful_target_for_the_user_and_entry():
    out = _run_board(r"""
      (async () => {
        const Control = registry.get('power-sync-advanced-controls');
        const posts = [];
        let accept = true, site = 'entry-one', targetSupported = true;
        function open(user = 'owner') {
          const card = new Control();
          card.hass = {user: {id: user}, callApi: async (method, path, body) => {
            if (method === 'POST') {
              posts.push(body);
              return {success: accept};
            }
            if (!path.startsWith('power_sync/powerwall/battery_dispatch')) return {schema_version: 0};
            return {schema_version: 1, entry_id: site, control_revision: 'rev',
              capabilities: {battery_output: {supported: true, zero_hold_supported: true, target_soc_supported: targetSupported}}};
          }};
          return card;
        }
        const card = open();
        await card._refresh();
        Object.assign(card._draft, {mode: 'charge', power_w: '2300', minutes: '45', target_soc: '85', takeover: true});
        await card._act('target');
        const beforeConfirm = posts.length;
        await card._act('target');
        accept = false;
        card._draft.minutes = '60';
        await card._act('target');
        await card._act('target');
        card._draft.minutes = '90';
        await card._act('refresh');
        const refreshKeepsEdits = card._draft.minutes === '90';
        const reopened = open();
        await reopened._refresh();
        const restored = {...reopened._draft};
        targetSupported = false;
        const limited = open();
        await limited._refresh();
        const unsupportedMode = limited._draft.mode;
        targetSupported = true;
        const postsBeforeReconfirm = posts.length;
        await reopened._act('target');
        const afterReopen = posts.length;
        accept = true;
        await reopened._act('target');
        Object.assign(reopened._draft, {mode: 'power', power_w: '-1700', target_soc: ''});
        await reopened._act('target');
        await reopened._act('target');
        const fixedPower = open();
        await fixedPower._refresh();
        const otherUser = open('guest');
        await otherUser._refresh();
        site = 'entry-two';
        await reopened._refresh();
        const otherSite = reopened._draft;
        console.log(JSON.stringify({
          beforeConfirm, postsBeforeReconfirm, afterReopen, posts, refreshKeepsEdits,
          restored, fixedPower: fixedPower._draft.power_w,
          otherUser: otherUser._draft.power_w, otherSite: otherSite.power_w,
          unsupportedMode,
        }));
      })().catch(e => {console.error(e);process.exitCode=1});
    """)
    assert out['beforeConfirm'] == 0
    assert out['postsBeforeReconfirm'] == out['afterReopen'] == 2
    assert out['refreshKeepsEdits']
    assert {k: out['restored'][k] for k in ('mode', 'power_w', 'minutes', 'target_soc', 'takeover')} == {
        'mode': 'charge', 'power_w': '2300', 'minutes': '45', 'target_soc': '85', 'takeover': False,
    }
    assert [post['power_w'] for post in out['posts']] == [-2300, -2300, -2300, -1700]
    assert [post['takeover'] for post in out['posts']] == [True, True, False, False]
    assert out['fixedPower'] == '-1700'
    assert out['unsupportedMode'] == 'power'
    assert out['otherUser'] == out['otherSite'] == '1800'


def test_advanced_stop_and_backup_recovery_survive_lost_acquisition_support():
    out = _run_board(r"""
      (async () => {
        const c = new (context.customElements.get('power-sync-advanced-controls'))();
        c.entryId = 'entry'; const posts=[];
        const o={schema_version:1,control_revision:'changed',capabilities:{battery_output:{supported:false}},operation:{active:true,operation_id:'owned',phase:'recovery_required',power_w:0}};
        c.hass={callApi:async(method,path,body)=>{if(method==='POST'){posts.push({path,body});return{};}return path.includes('backup_preparation')?{schema_version:1,capabilities:{writable:false},recovery_required:true,operation:{phase:'prepared',active:true,ends_at:1800000000,can_cancel:false}}:path.includes('native_site_limits')?null:o;}};
        await c._act('refresh'); const html=c.innerHTML;
        await c._act('stop'); await c._act('stop');
        await c._act('backup_reconcile'); await c._act('backup_reconcile');
        console.log(JSON.stringify({html,posts}));
      })().catch(e=>{console.error(e);process.exitCode=1});
    """)
    assert 'Stop this battery target' in out['html']
    assert 'Check backup status' in out['html']
    assert out['posts'][0]['body']['operation_id'] == 'owned'
    assert out['posts'][0]['path'].endswith('battery_dispatch/stop')
    assert out['posts'][1]['body']['operation'] == 'reconcile'


def test_zero_idle_status_does_not_claim_charging_or_change_legacy_max():
    out = _run_board(r"""console.log(JSON.stringify({
      idle: T.psBatteryControl({state:'force_charge',attributes:{control_source:'optimizer',battery_control_mode:'idle'}}),
      max: T.psBatteryControl({state:'force_charge',attributes:{control_source:'user'}})
    }));""")
    assert out['idle']['message'] == 'Idle requested by the optimiser.'
    assert out['max']['message'] == 'Charge requested manually.'


def test_advanced_sections_and_confirmation_survive_independent_refreshes():
    out = _run_board(r'''
(async () => {
  const c = new (registry.get('power-sync-advanced-controls'))();
  let markup = '', sections = [], waiting = null, rejectLimits = false;
  Object.defineProperty(c, 'innerHTML', {get:()=>markup, set:value=>{markup=value; sections=[...value.matchAll(/data-advanced-section="([^"]+)"/g)].map(m=>({dataset:{advancedSection:m[1]},open:false}));}});
  c.querySelectorAll=()=>sections;
  c.querySelector=selector=>sections.find(n=>selector.includes('"'+n.dataset.advancedSection+'"'));
  c.entryId='entry';
  const output={schema_version:1,control_revision:'rev',capabilities:{battery_output:{supported:true,zero_hold_supported:true,target_soc_supported:true,max_power_w:10000}}};
  const limits={schema_version:1,configuration_revision:'cfg',capabilities:{readable:true,writable:true},limits:{import_limit_w:{value_w:12000},export_limit_w:{value_w:5000}},snapshots:[]};
  c.hass={callApi:async(method,path,body)=>{
    if(method==='POST')return {success:false,error:'unconfirmed_gateway_write'};
    if(path.includes('battery_dispatch'))return output;
    if(path.includes('native_site_limits')){if(rejectLimits)throw Error('gateway_offline');return limits;}
    if(waiting)return await waiting;
    return {schema_version:1,capabilities:{writable:true}};
  }};
  await c._act('refresh'); sections.forEach(n=>n.open=true); c._draft.export_limit_w='2000';
  await c._act('set'); const confirmed=c._confirm==='set'&&sections.every(n=>n.open);
  let release; waiting=new Promise(resolve=>release=resolve);
  const read=c._refresh(); for(let i=0;i<15;i++)await Promise.resolve();
  const independent=c._output===output&&c._loading.size===1&&markup.includes('Battery power and targets');
  const stable=c._confirm==='set'&&sections.every(n=>n.open)&&c._draft.export_limit_w==='2000';
  release({schema_version:1,capabilities:{writable:true}});await read;waiting=null;
  await c._act('set');const unconfirmed=c._message.includes('unconfirmed_gateway_write')&&!c._message.includes('Control updated');
  rejectLimits=true;await c._refresh();const retained=c._limits===limits&&markup.includes('gateway_offline')&&c._draft.export_limit_w==='2000';
  console.log(JSON.stringify({confirmed,independent,stable,unconfirmed,retained,advancedBeforeLimits:markup.indexOf('Advanced battery controls')<markup.indexOf('Site limits'), collapsedEditor:!markup.includes('data-advanced-section="battery" open'), flatGroups:!markup.includes('>More controls<')}));
})().catch(e=>{console.error(e);process.exitCode=1});
''')
    assert all(out.values()), out


def test_advanced_controls_start_independent_reads_once_per_entry_on_connection():
    out = _run_board(r'''
(async () => {
  const c=new (registry.get('power-sync-advanced-controls'))();const reads=[];
  c.entryId='a';c.isConnected=true;
  const hass={callApi:async(method,path)=>{reads.push(path);return path.includes('native_site_limits')?{schema_version:1,capabilities:{writable:true},limits:{import_limit_w:{value_w:12000},export_limit_w:{value_w:5000}},snapshots:[]}:path.includes('battery_dispatch')?{schema_version:1,capabilities:{battery_output:{supported:true}}}:{schema_version:1,capabilities:{writable:true}};}};
  c.hass=hass;c.hass=hass;c.connectedCallback();
  for(let i=0;i<15;i++)await Promise.resolve();
  const initial=reads.length;c.entryId='b';
  for(let i=0;i<15;i++)await Promise.resolve();
  console.log(JSON.stringify({initial,total:reads.length,selected:reads.slice(3).every(p=>p.includes('entry_id=b'))}));
})().catch(e=>{console.error(e);process.exitCode=1});
''')
    assert out == {"initial": 3, "total": 6, "selected": True}


def test_advanced_dashboard_controls_require_a_tesla_local_capability_entity():
    assert _run_strategy(board_missing=False)["controlsAdvanced"] is False
    script = _strategy_script(False).replace("  const hass = {", "  states['binary_sensor.power_sync_powerwall_local_paired'] = s('off');\n  const hass = {")
    result = subprocess.run(["node", "-e", script, str(STRATEGY_PATH)], check=True, capture_output=True, text=True)
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["controlsAdvanced"] is True, "Unpaired Tesla still gets setup feedback, other brands do not get Tesla-only controls"


def test_plan_renders_battery_efficiency_from_the_coordinator_api(opt_module, clock):
    from test_battery_efficiency_learning import _record_cycle
    from test_current_export_reserve_decision import _controller, _prepare_api

    coordinator, _, _, _ = _controller(opt_module)
    _prepare_api(opt_module, coordinator)
    coordinator._is_in_demand_window_at = lambda timestamp: False
    now = clock.now
    learner = opt_module.BatteryEfficiencyLearner()
    coordinator._battery_efficiency_learner = learner
    coordinator._last_published_optimizer_parameters = learner.resolved_parameters(now=now)
    payloads = {"cold": coordinator.get_api_data()}

    for index in range(10):
        _record_cycle(learner, now - timedelta(days=10 - index))
    next_parameters = learner.resolved_parameters(now=now)
    coordinator._last_resolved_optimizer_parameters = next_parameters
    payloads["pending"] = coordinator.get_api_data()
    coordinator._last_published_optimizer_parameters = next_parameters
    payloads["learned"] = coordinator.get_api_data()

    result = _run_board(f"const payloads = {json.dumps(payloads)};" + r"""
  (async () => {
    const result = {};
    for (const [name, data] of Object.entries(payloads)) {
      T.psApiCache.clear();
      const card = new T.PsPlanCard();
      card._config = {entities: {}};
      card._hass = {states: {}, config: {}, locale: {}, callApi: async (method, path) => {
        if (method !== 'GET' || path !== 'power_sync/optimization') throw new Error(path);
        return data;
      }};
      card._loadHistory = () => {};
      card._invalidate = () => {};
      card.querySelector = () => ({addEventListener() {}});
      card._render();
      await T.psApiCache.get('power_sync/optimization').promise;
      card._render();
      result[name] = card.innerHTML.match(/<p class="hint">([^<]*round-trip[^<]*)<\/p>/)?.[1];
    }
    console.log(JSON.stringify(result));
  })();
""")

    assert "This plan assumes 84.6% battery round-trip efficiency" in result["cold"]
    assert "No measured estimate yet" in result["cold"]
    assert "This plan assumes 84.6% battery round-trip efficiency" in result["pending"]
    measured = f"{next_parameters.battery_efficiency_candidate_rte:.1%}"
    assert f"Measured round-trip estimate is {measured}" in result["pending"]
    assert "10 valid cycles over 10 days" in result["pending"]
    confidence = f"{next_parameters.battery_efficiency_confidence:.0%}"
    assert f"{confidence} learning confidence" in result["pending"]
    expected = f"{next_parameters.physical_round_trip_efficiency:.1%}"
    assert f"This plan uses {expected} battery round-trip efficiency" in result["learned"]


def test_plan_history_selects_charge_level_and_prices_at_the_requested_time():
    out = _run_board(r"""
  const now = Date.now(), base = now - 2 * 3600000;
  const midnight = new Date(new Date().getFullYear(), new Date().getMonth(), new Date().getDate()).getTime();
  const card = new T.PsPlanCard();
  card._config = { entities: { soc: 'sensor.soc', importPrice: 'sensor.price' } };
  card._hass = { states: { 'sensor.soc': {state:'80',attributes:{}}, 'sensor.price': {attributes:{unit_of_measurement:'c/kWh'}} } };
  card._hist = { 'sensor.soc': [[base,65],[base+300000,0],[base+600000,NaN],[base+900000,40]],
    'sensor.price': [[base,25],[base+300000,0],[base+600000,-5],[base+900000,35]] };
  const model = card._buildModel({schedule:{}});
  const at = offset => (base + offset + 60000 - midnight) / 3600000;
  console.log(JSON.stringify({soc:[0,300000,600000,900000].map(t=>model.soc(at(t))),
    prices:[0,300000,600000,900000].map(t=>model.imp(at(t)))}));
""")
    assert out == {"soc": [65, 0, None, 40], "prices": [25, 0, -5, 35]}


def test_ev_panel_poll_does_not_skip_a_tick_after_a_slow_response():
    script = (
        "const fs = require('fs'); const vm = require('vm');"
        + BROWSER_STUBS
        + f"const strategy = {json.dumps(str(STRATEGY_PATH))};"
        + r"""
    (async () => {
      let now = 1000;
      let tick;
      context.Date = class extends Date { static now() { return now; } };
      let delay;
      context.window.setInterval = context.window.setTimeout = (callback, ms) => {
        tick = callback;
        delay = ms;
        return 1;
      };
      vm.createContext(context);
      vm.runInContext(fs.readFileSync(strategy, 'utf8'), context);
      const Panel = vm.runInContext('PowerSyncEVPanel', context);
      const panel = new Panel();
      panel._hass = {callApi() {}};
      panel._scheduleRender = () => {};
      panel._syncSelectedLoadpoint = () => {};
      const starts = [];
      let finish;
      panel._fetchBundle = () => {
        starts.push(now);
        return new Promise(resolve => { finish = resolve; });
      };

      panel.connectedCallback();
      now = 1250;
      finish({status: {loadpoints: []}});
      await new Promise(resolve => setImmediate(resolve));
      const firstCompletedAt = panel._lastFetch;
      now = 30999;
      tick();
      const earlyRetryDelay = delay;
      now = 31000;
      tick();
      tick();
      console.log(JSON.stringify({starts, firstCompletedAt, earlyRetryDelay}));
    })().catch(error => { console.error(error); process.exit(1); });
"""
    )

    assert _run(script) == {
        "starts": [1000, 31000],
        "firstCompletedAt": 1250,
        "earlyRetryDelay": 1,
    }


def test_ev_panel_setting_forms_patch_and_confirm_selected_vehicle():
    result = _run(
        "const fs = require('node:fs'); const vm = require('node:vm');"
        + BROWSER_STUBS
        + f"const strategy = {json.dumps(str(STRATEGY_PATH))};"
        + r"""
        context.CSS = {escape: x => x};
        vm.createContext(context);
        vm.runInContext(fs.readFileSync(strategy, 'utf8'), context);
        const Panel = vm.runInContext('PowerSyncEVPanel', context);
        const assert = require('node:assert/strict');
        const copy = x => JSON.parse(JSON.stringify(x));
        const tick = () => new Promise(resolve => setImmediate(resolve));
        const api = vm.runInContext('EV_PANEL_PATHS', context);
        function input(dataset, type, value, step) {
          return {dataset, type, value, step, validity: {}, listeners: {}, closest: () => null, focus() {},
            addEventListener(name, fn) { this.listeners[name] = fn; },
            edit(next) { this.value = next; this.listeners.input({target: this}); },
            click() { return this.listeners.click({currentTarget: this}); }};
        }
        function makePanel() {
          const panel = new Panel();
          const car = {vehicle_id:'generic_ev', shared_charger_profile_id:'car-a', enabled:false, target_soc:80,
            departure_times:{'0':'07:00'}, max_grid_price_cents:0, departure_max_grid_price_cents:{'0':-2}};
          const price = {enabled:false, recovery_soc:30, recovery_price_cents:12.5, opportunity_price_cents:3};
          const shared = {available:true, enabled:true, selection_allowed:true, selected_vehicle_id:'car-a',
            profiles:[{id:'car-a',name:'A',soc_entity:'sensor.a',battery_capacity_kwh:50}]};
          const data = {priceSettings:price, autoSettings:{generic_ev:car}, autoStatus:{settings:{generic_ev:car}},sharedCharger:shared};
          panel._data = copy(data);
          panel._scheduleRender = () => {};
          panel._loadData = async () => {};
          const writes = [], reads = [], controls = [];
          const p = {panel, price, car, shared, writes, reads, controls, stale:false, wrongAck:false, offline:false, wrongRead:false};
          panel._hass = {callApi: async (verb,path,payload) => {
            if (verb === 'GET') {
              reads.push(path);
              if (writes.length && p.offline) throw new Error('offline');
              if (path === api.price) return {success:true,settings:p.stale && writes.length ? data.priceSettings : copy(p.price)};
              if (path === api.autoSettings) return {success:true,settings:{generic_ev:{...copy(p.car), ...(p.wrongRead && writes.length ? {shared_charger_profile_id:'car-b'} : {})}}};
              if (path === api.sharedCharger) return {success:true,shared_charger:copy(p.shared)};
            }
            assert.equal(verb, 'POST');
            writes.push([path,copy(payload)]);
            if (p.hold) await p.hold;
            if (path === api.price) {
              p.price = {...p.price,...payload};
              return {success:true,settings:p.wrongAck ? {} : copy(p.price)};
            }
            if ([api.autoSettings,api.autoToggle].includes(path)) {
              const {expected_settings,...update}=payload;
              if (p.conflict) throw {status_code:409,body:{success:false,code:'settings_conflict',field:'departure_max_grid_price_cents',error:'Weekday prices changed. Reload and retry.'}};
              p.car={...p.car,...update};
              return path === api.autoToggle ? {success:true,...copy(p.car)} : {success:true,settings:copy(p.car)};
            }
            if (path === api.sharedCharger) {
              if (payload.operation === 'save') {
                const id=payload.vehicle_id ?? 'new-id';
                const profile={id,name:payload.name,soc_entity:payload.soc_entity,battery_capacity_kwh:payload.battery_capacity_kwh};
                p.shared.profiles=[...p.shared.profiles.filter(x=>x.id!==id),profile];
              } else if (payload.operation === 'select') p.shared.selected_vehicle_id=payload.vehicle_id;
              else p.shared.profiles=p.shared.profiles.filter(x=>x.id!==payload.vehicle_id);
              return {success:true,shared_charger:copy(p.shared)};
            }
            throw Error('Unexpected route ' + path);
          }};
          panel.shadowRoot.querySelectorAll = selector => {
            if (selector.includes(', ')) return controls.filter(i => i.type !== 'button');
            if (selector.startsWith('[data-setting^=')) return controls.filter(i => i.dataset.setting?.startsWith('price:'));
            if (selector.startsWith('[data-departure-time=')) return controls.filter(i => i.dataset.departureTime === 'generic_ev');
            const buttonFields = {'[data-save-mode]':'saveMode','[data-target-save]':'targetSave','[data-departure-save]':'departureSave','[data-smart-toggle]':'smartToggle'};
            return controls.filter(i=>Object.hasOwn(i.dataset,buttonFields[selector]));
          };
          panel.shadowRoot.querySelector = selector => selector.includes('data-target-input') ? controls.find(i=>i.dataset.targetInput) : null;
          p.bind = () => { panel._bindSettingDrafts(); panel._attachEvents(); };
          return p;
        }
        (async () => {
          for (const scenario of ['ok','blank','fractional-soc','stale','wrongAck','offline','conflict','new-edit']) {
            const p=makePanel();
            const price=input({setting:'price:recovery_price_cents'}, 'number', '12.5','0.1');
            const soc=input({setting:'price:recovery_soc'}, 'number', '30','1');
            const button=input({saveMode:'price'},'button');
            p.controls.push(price,soc,button); p.bind();
            if (scenario === 'fractional-soc') soc.edit('25.9');
            else price.edit(scenario === 'blank' ? '' : '-2.125');
            if (scenario === 'conflict') p.price.recovery_price_cents=11;
            p.price.opportunity_price_cents=8;
            if (['stale','wrongAck','offline'].includes(scenario)) p[scenario]=true;
            let finish;
            if (scenario === 'new-edit') p.hold=new Promise(done=>{finish=done;});
            button.click(); await tick();
            if (finish) {
              assert.equal(p.panel._savingKey,'mode:price');
              price.edit('-3'); finish(); await tick();
              const draft=[...p.panel._settingDrafts.values()][0];
              assert.equal(draft.value,'-3');
              assert.equal(draft.opened.recovery_price_cents,-2.125);
              continue;
            }
            if (['blank','fractional-soc','conflict'].includes(scenario)) assert.equal(p.writes.length,0);
            else assert.deepEqual(p.writes[0], [api.price,{recovery_price_cents:-2.125}]);
            if (scenario === 'ok') {
              assert.equal(p.price.opportunity_price_cents,8);
              assert.equal(p.panel._settingDrafts.size,0);
              assert.match(p.panel._notice.message,/Price Level saved and confirmed by Home Assistant: recovery_price_cents/);
            } else {
              assert.equal(p.panel._notice.type,'error');
              assert.equal(p.panel._settingDrafts.size,1);
            }
          }
          for (const raw of ['0','0.0875']) {
            const p=makePanel(), field=input({setting:'price:recovery_price_cents'},'number','12.5','any');
            p.controls.push(field); p.bind(); field.edit(raw);
            assert.equal(await p.panel._saveMode('price'),true);
            assert.deepEqual(p.writes[0][1],{recovery_price_cents:Number(raw)});
          }
          const p=makePanel();
          const target=input({targetInput:'generic_ev'},'number','80','1');
          const targetButton=input({targetSave:'generic_ev'},'button');
          const departure=input({departureTime:'generic_ev',departureDay:'0'},'time','07:00');
          const departureButton=input({departureSave:'generic_ev'},'button');
          const toggle=input({smartToggle:'generic_ev',enabled:'true'},'button');
          p.controls.push(target,targetButton,departure,departureButton,toggle); p.bind();
          target.edit('92'); targetButton.click(); await tick();
          assert.deepEqual(p.writes[0],[api.autoSettings,{vehicle_id:'generic_ev',target_soc:92,shared_charger_profile_id:'car-a'}]);
          departure.edit('06:30'); p.car.departure_times['1']='08:00'; departureButton.click(); await tick();
          assert.deepEqual(p.writes[1][1].departure_times,{'0':'06:30','1':'08:00'});
          toggle.click(); await tick();
          assert.deepEqual(p.writes[2],[api.autoToggle,{vehicle_id:'generic_ev',enabled:true,shared_charger_profile_id:'car-a'}]);
          const current=copy(p.car);
          p.car.departure_max_grid_price_cents['1']=0;
          assert.equal(await p.panel._writeSmartSettings('generic_ev',current,{departure_max_grid_price_cents:{'2':-2.111}}),true);
          assert.deepEqual(p.writes[3][1].departure_max_grid_price_cents,{'1':0,'2':-2.111});
          for (const mode of ['conflict','changed-before','changed-after','wrong-vehicle','not-finite']) {
            const p=makePanel(), opened=copy(p.car);
            if (mode==='conflict') p.car.target_soc=55;
            if (mode==='changed-before') p.car.shared_charger_profile_id='new';
            if (mode==='changed-after') p.wrongRead=true;
            if (mode==='wrong-vehicle') opened.vehicle_id='another';
            const change=mode==='not-finite' ? {max_grid_price_cents:NaN} : {target_soc:70};
            assert.equal(await p.panel._writeSmartSettings('generic_ev',opened,change),false);
            assert.equal(p.writes.length,mode==='changed-after'?1:0);
          }
          // The HA poll can finish between painting A's button and dispatching its click.
          const race=makePanel();
          const staleButton=input({smartToggle:'generic_ev',enabled:'true'},'button');
          race.controls.push(staleButton); race.bind();
          race.car.shared_charger_profile_id='car-b';
          race.panel._data.autoSettings.generic_ev=copy(race.car);
          staleButton.click(); await tick();
          assert.deepEqual(race.writes,[]);
          assert.equal(race.panel._notice.type,'warn');
          assert.match(race.panel._notice.message,/different car was selected/);
          const guarded = makePanel();
          guarded.car.settings_precondition_supported=true;
          guarded.car.departure_max_grid_price_cents['2']=0;
          const opened=copy(guarded.car);
          guarded.car.departure_max_grid_price_cents['1']=-1.5;
          assert.equal(await guarded.panel._writeSmartSettings('generic_ev',opened,{departure_max_grid_price_cents:{...opened.departure_max_grid_price_cents,'3':0}}),true);
          assert.deepEqual(guarded.writes[0][1].expected_settings,{departure_max_grid_price_cents:{'0':-2,'1':-1.5,'2':0}});
          assert.deepEqual(guarded.writes[0][1].departure_max_grid_price_cents,{'0':-2,'1':-1.5,'2':0,'3':0});
          guarded.conflict=true;
          assert.equal(await guarded.panel._writeSmartSettings('generic_ev',copy(guarded.car),{departure_max_grid_price_cents:{'4':-2.25}}),false);
          assert.equal(guarded.writes.length,2);
          assert.match(guarded.panel._notice.message,/Weekday prices changed/);
          assert.equal(guarded.panel._notice.type,'error');
          guarded.conflict=false;
          assert.equal(await guarded.panel._writeSmartSettings('generic_ev',copy(guarded.car),{enabled:true},{toggle:true}),true);
          assert.deepEqual(guarded.writes[2][1].expected_settings,{enabled:false});
          const unshared = makePanel();
          unshared.panel._data.sharedCharger.enabled=false;
          unshared.car.shared_charger_profile_id=null;
          assert.equal(await unshared.panel._writeSmartSettings('generic_ev',copy(unshared.car),{target_soc:75}),true);
          assert.deepEqual(unshared.writes[0][1],{vehicle_id:'generic_ev',target_soc:75,shared_charger_profile_id:null});
          for (const setting of [{target_soc:0},{target_soc:44.5},{enabled:'false'},{departure_times:{'0':'25:00'}},
               {departure_max_grid_price_cents:{'8':2}}, {departure_max_grid_price_cents:{'0':false}}]) {
            const p=makePanel();
            assert.equal(await p.panel._writeSmartSettings('generic_ev',copy(p.car),setting),false);
            assert.deepEqual(p.writes,[]);
          }
          for (const scenario of ['clear','fraction','wrong-car','mismatch']) {
            const p=makePanel();
            const base={vehicle_id:'ocpp_1',battery_capacity_kwh:null,charger_fallback_battery_capacity_kwh:65,effective_battery_capacity_kwh:65};
            p.panel._data.vehicleConfigs=[base];
            const input={value:'60.125'};
            p.panel.shadowRoot.querySelector=()=>input;
            let saved;
            p.panel._hass.callApi=async(method,path,payload)=>{
              assert.equal(path, api.vehicleConfig);
              if(method==='GET') return {success:true,configs:[scenario==='mismatch'?base:(saved??base)]};
              saved={...base,charger_fallback_battery_capacity_kwh:payload.battery_capacity_kwh,effective_battery_capacity_kwh:payload.battery_capacity_kwh??80};
              return {success:true,config:scenario==='wrong-car'?{...saved,vehicle_id:'wrong'}:saved};
            };
            assert.equal(await p.panel._saveVehicleCapacity('ocpp_1',scenario==='clear'),['clear','fraction'].includes(scenario));
            assert.equal(saved.charger_fallback_battery_capacity_kwh,scenario==='clear'?null:60.125);
          }
          const d=makePanel();
          const a=input({targetInput:'generic_ev'},'number','80');
          d.controls.push(a); d.bind(); a.edit('91');
          d.controls.length=0;
          const again=input({targetInput:'generic_ev'},'number','80');
          d.controls.push(again); d.bind(); assert.equal(again.value,'91');
          d.panel._data.autoSettings.generic_ev.shared_charger_profile_id='car-b';
          const other=input({targetInput:'generic_ev'},'number','65');
          d.controls.splice(0,1,other); d.bind(); assert.equal(other.value,'65');
          const s=makePanel();
          const create={operation:'save',vehicle_id:null,name:'B',soc_entity:'sensor.b',battery_capacity_kwh:55.5};
          s.offline=true;
          assert.equal(await s.panel._runCommand('shared:save',api.sharedCharger,create,'Saved'),false);
          s.offline=false;
          assert.equal(await s.panel._runCommand('shared:save',api.sharedCharger,create,'Saved'),true);
          assert.equal(s.writes.length,1);
          console.log(JSON.stringify({ok:true}));
        })().catch(err=>{console.error(err);process.exit(1);});
        """
    )
    assert result == {"ok": True}


def test_ev_smart_policy_editor_prices_strategies_weekdays_and_shared_profile_changes():
    """The Smart Schedule policy editor writes only edited defaults/weekdays.

    Monday 22:00 in Brisbane with a Tuesday 07:00 departure plans on Tuesday's
    rules. A different shared-charger car selected mid-edit discards the stale
    draft instead of writing it to the newly selected car.
    """
    result = _run(
        "const fs = require('node:fs'); const vm = require('node:vm');"
        + BROWSER_STUBS
        + f"const strategy = {json.dumps(str(STRATEGY_PATH))};"
        + r"""
        const RealDate = Date;
        class FixedDate extends RealDate {
          constructor(...args) { if (args.length) super(...args); else super('2026-11-23T12:00:00Z'); }
          static now() { return RealDate.parse('2026-11-23T12:00:00Z'); }
        }
        context.Date = FixedDate;
        context.CSS = {escape: x => x};
        context.setImmediate = setImmediate;
        vm.createContext(context);
        vm.runInContext(fs.readFileSync(strategy, 'utf8'), context);
        const Panel = vm.runInContext('PowerSyncEVPanel', context);
        const api = vm.runInContext('EV_PANEL_PATHS', context);
        const assert = require('node:assert/strict');
        const copy = x => JSON.parse(JSON.stringify(x));
        const tick = () => new Promise(resolve => setImmediate(resolve));
        const baseCar = () => ({
          vehicle_id: 'generic_ev', shared_charger_profile_id: 'car-a', settings_precondition_supported: true,
          enabled: true, target_soc: 80, priority: 'cost_optimized', max_grid_price_cents: 25,
          departure_times: {'1': '07:00', '5': '09:00'}, departure_priorities: {'5': 'solar_only'},
          departure_max_grid_price_cents: {'0': -2, '5': 18.25},
          min_battery_to_start: 20, preserve_home_battery: false, consume_battery_level: 0,
          stop_at_battery_floor: true, limit_grid_import: false,
          departure_min_battery_to_start: {}, departure_preserve_home_battery: {},
          departure_consume_battery_level: {}, departure_stop_at_battery_floor: {}, departure_limit_grid_import: {},
        });
        function control(dataset, type, value) {
          return {dataset, type, value, checked: value === true, validity: {}, listeners: {}, closest: () => null, focus() {},
            addEventListener(name, fn) { this.listeners[name] = fn; },
            edit(next) { if (type === 'checkbox') this.checked = next; else this.value = next; this.listeners.input({target: this}); },
            click() { return this.listeners.click({currentTarget: this}); }};
        }
        function makePanel(car = baseCar()) {
          const panel = new Panel();
          const p = {panel, car, writes: [], loads: 0, conflict: null};
          panel._data = {autoSettings: {generic_ev: copy(car)}, autoStatus: {settings: {generic_ev: copy(car)}},
            sharedCharger: {available: true, enabled: true, selected_vehicle_id: 'car-a', profiles: []}};
          panel._scheduleRender = () => {};
          panel._loadData = async () => { p.loads += 1; };
          panel._hass = {config: {currency: 'AUD', time_zone: 'Australia/Brisbane'}, callApi: async (verb, path, payload) => {
            if (verb === 'GET') return {success: true, settings: {generic_ev: copy(p.car)}};
            p.writes.push([path, copy(payload)]);
            if (p.conflict) throw {status_code: 409, body: {success: false, ...p.conflict}};
            const {expected_settings, ...update} = payload;
            p.car = {...p.car, ...update};
            return {success: true, settings: copy(p.car)};
          }};
          const controls = [];
          p.controls = controls;
          panel.shadowRoot.querySelectorAll = selector => {
            if (selector.includes(', ')) return controls.filter(c => c.type !== 'button');
            if (selector.startsWith('[data-smart-policy=')) return controls.filter(c => c.dataset.smartPolicy);
            if (selector === '[data-policy-save]') return controls.filter(c => c.dataset.policySave);
            return [];
          };
          panel.shadowRoot.querySelector = () => null;
          p.field = (field, day, type, value) => {
            const dataset = {smartPolicy: 'generic_ev', policyField: field, label: field};
            if (day != null) dataset.policyDay = day;
            const c = control(dataset, type, value); controls.push(c); return c;
          };
          p.save = control({policySave: 'generic_ev', vehicleName: 'Model Y'}, 'button');
          controls.push(p.save);
          p.bind = () => { panel._bindSettingDrafts(); panel._attachEvents(); };
          return p;
        }
        (async () => {
          const out = {};
          // Rendering: labelled default, unit, inherited weekday cells, applicability.
          const view = makePanel();
          view.panel._hass = {config: {currency: 'AUD', time_zone: 'Australia/Brisbane'}};
          out.html = view.panel._smartPolicyHtml('generic_ev', baseCar(), 'Model Y');
          out.day = view.panel._policyDay(baseCar());
          out.noDeparture = view.panel._policyDay({...baseCar(), departure_times: {}});
          out.onlyPassedToday = view.panel._policyDay({...baseCar(), departure_times: {'0': '07:00'}});
          const legacy = baseCar();
          delete legacy.departure_max_grid_price_cents;
          delete legacy.settings_precondition_supported;
          out.legacyHtml = view.panel._smartPolicyHtml('generic_ev', legacy, 'Model Y');

          // Save: default price, Tuesday price, Wednesday strategy and clearing Monday.
          const p = makePanel();
          const price = p.field('max_grid_price_cents', null, 'number', '25');
          const tue = p.field('max_grid_price_cents', '1', 'number', '');
          const mon = p.field('max_grid_price_cents', '0', 'number', '-2');
          const wed = p.field('priority', '2', 'select-one', '');
          p.bind();
          price.edit('12.25'); tue.edit('-1.5'); mon.edit(''); wed.edit('time_critical');
          p.save.click(); await tick(); await tick();
          out.write = p.writes[0];
          out.message = p.panel._notice.message;
          out.draftsAfter = p.panel._settingDrafts.size;

          // Zero is an explicit limit, not inheritance.
          const zero = makePanel();
          const sat = zero.field('max_grid_price_cents', '5', 'number', '18.25');
          zero.bind(); sat.edit('0'); zero.save.click(); await tick(); await tick();
          out.zero = zero.writes[0][1].departure_max_grid_price_cents;

          // Mutually exclusive battery choices are rejected before any request.
          const both = makePanel();
          const keep = both.field('preserve_home_battery', null, 'checkbox', false);
          const use = both.field('consume_battery_level', null, 'number', '0');
          both.bind(); keep.edit(true); use.edit('30');
          both.save.click(); await tick();
          out.both = {writes: both.writes.length, type: both.panel._notice.type, message: both.panel._notice.message};

          // A poll shows another car selected on the shared charger mid-edit.
          const poll = makePanel();
          const pollPrice = poll.field('max_grid_price_cents', null, 'number', '25');
          poll.bind(); pollPrice.edit('5');
          poll.panel._data.autoSettings.generic_ev.shared_charger_profile_id = 'car-b';
          out.pruned = poll.panel._pruneStaleProfileDrafts();
          out.pollDrafts = poll.panel._settingDrafts.size;
          out.pollNotice = poll.panel._notice;

          // The car changes between the preflight read and the POST: HA returns 409.
          const server = makePanel();
          server.conflict = {code: 'shared_profile_changed', error: 'Shared charger selection is changing.'};
          const serverPrice = server.field('max_grid_price_cents', null, 'number', '25');
          server.bind(); serverPrice.edit('7.5');
          server.save.click(); await tick(); await tick();
          out.server = {writes: server.writes.map(w => w[1]), drafts: server.panel._settingDrafts.size,
            notice: server.panel._notice, loads: server.loads};

          // An older integration without per-day prices never receives the map.
          const old = makePanel(legacy);
          const oldDefault = old.field('max_grid_price_cents', null, 'number', '25');
          old.bind(); oldDefault.edit('30');
          old.save.click(); await tick(); await tick();
          out.old = old.writes.map(w => w[1]);
          console.log(JSON.stringify(out));
        })().catch(err => { console.error(err); process.exit(1); });
        """
    )
    html = result["html"]
    assert "Maximum grid price (c/kWh)" in html
    assert 'value="25"' in html
    assert "Default strategy" in html and "Weekday overrides" in html
    for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
        assert f'<div class="day-name">{day}</div>' in html
    assert 'placeholder="Default 25"' in html
    assert "Default (Cheapest)" in html
    assert "Meet Deadline" in html and "Prefer Solar" in html and "Solar Only" in html
    assert "Next departure Tuesday 07:00: planning uses Tuesday&#39;s settings" in html or \
        "Next departure Tuesday 07:00: planning uses Tuesday's settings" in html
    assert "separate from export credit" in html
    assert "Save charging rules for Model Y" in html
    assert result["day"] == {"day": "1", "time": "07:00"}
    assert result["noDeparture"] == {"day": "0", "time": None}
    assert result["onlyPassedToday"] == {"day": "0", "time": "07:00"}
    assert "Per-day price limits need a newer PowerSync version." in result["legacyHtml"]
    assert "Max grid price (c/kWh)" not in result["legacyHtml"]

    path, payload = result["write"]
    assert path == "power_sync/ev/auto_schedule/settings"
    assert payload["vehicle_id"] == "generic_ev"
    assert payload["shared_charger_profile_id"] == "car-a"
    assert payload["max_grid_price_cents"] == 12.25
    assert payload["departure_max_grid_price_cents"] == {"1": -1.5, "5": 18.25}
    assert payload["departure_priorities"] == {"2": "time_critical", "5": "solar_only"}
    assert payload["expected_settings"] == {
        "max_grid_price_cents": 25,
        "departure_max_grid_price_cents": {"0": -2, "5": 18.25},
        "departure_priorities": {"5": "solar_only"},
    }
    assert "target_soc" not in payload and "departure_times" not in payload
    assert "confirmed by Home Assistant" in result["message"]
    assert "Meet Deadline days may buy grid energy above the price limit" in result["message"]
    assert result["draftsAfter"] == 0
    assert result["zero"] == {"0": -2, "5": 0}

    assert result["both"]["writes"] == 0 and result["both"]["type"] == "error"
    assert result["pruned"] is True and result["pollDrafts"] == 0
    assert result["pollNotice"]["type"] == "warn"
    assert "different car was selected" in result["pollNotice"]["message"]

    server = result["server"]
    assert len(server["writes"]) == 1 and server["writes"][0]["shared_charger_profile_id"] == "car-a"
    assert server["drafts"] == 0
    assert server["notice"]["type"] == "warn" and "Not saved" in server["notice"]["message"]
    assert server["loads"] == 1

    assert result["old"] == [{"vehicle_id": "generic_ev", "max_grid_price_cents": 30, "shared_charger_profile_id": "car-a"}]
