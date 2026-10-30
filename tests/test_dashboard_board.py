"""Regression tests for the approved dashboard board (power-sync-dashboard.js).

The strategy hands every card to <power-sync-board>; these tests run the real
frontend sources under node with minimal browser stubs.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess


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
      keys: cards.map(c => c.key),
      wide: cards.filter(c => c.size === 'wide').map(c => c.key),
      natives: [...new Set(cards.map(c => c.native).filter(Boolean))],
      missingRender: cards.filter(c => !c.native && !c.card).map(c => c.key),
      optRows: (cards.find(c => c.key === 'optset')?.config.rows || []).map(r => r.entity),
      pwRows: (cards.find(c => c.key === 'pwset')?.config.rows || []).map(r => r.entity),
      controls: cards.find(c => c.key === 'controls')?.config.entities || null,
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
    assert out["controls"]["chargeDuration"] == "select.power_sync_force_charge_duration"
    assert out["tariffMeta"] == "GloBird FOUR4FREE"


def test_generate_falls_back_to_classic_layout_without_board():
    out = _run_strategy(board_missing=True)
    assert out["type"] == "custom:power-sync-layout"


def _run_strategy(board_missing: bool) -> dict:
    result = subprocess.run(
        ["node", "-e", _strategy_script(board_missing), str(STRATEGY_PATH)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _board_script(body: str) -> str:
    return (
        "const fs = require('fs'); const vm = require('vm');"
        + BROWSER_STUBS
        + r"""
  vm.createContext(context);
  const src = fs.readFileSync(process.argv[1], 'utf8').replace(/^export \{[^}]*\};?\s*$/m, '');
  vm.runInContext(src + '\n;globalThis.__t = { PowerSyncBoard, PsTodayCard, PsControlsCard, psSchedule, psWindows, psActionKey, PS_LAYOUT_KEY, PS_DEFAULT_LAYOUT };', context);
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


def test_discharge_uses_custom_time_only_when_select_is_custom():
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
  card.hass = makeHass('Custom', 90);
  press(card, 'discharge');
  press(card, 'charge');
  setTimeout(() => console.log(JSON.stringify({ calls })), 20);
""")
    assert out["calls"] == [
        ["force_discharge", {"duration": "30"}],
        ["force_discharge", {"duration": "90"}],
        ["force_charge", {"duration": "30"}],
    ]
