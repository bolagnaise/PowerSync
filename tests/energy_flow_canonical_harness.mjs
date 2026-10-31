// Execute the shipped card, strategy and scene maps with read-only HA fakes.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {fileURLToPath, pathToFileURL} from 'node:url';

const frontend = fileURLToPath(new URL('../custom_components/power_sync/frontend/', import.meta.url));
const registry = new Map();
const timers = new Map();
let timerId = 0;
let now = Date.parse('2026-10-31T09:00:00Z');
Date.now = () => now;
globalThis.setTimeout = (fn, delay) => {
  timers.set(++timerId, {fn, at: now + delay});
  return timerId;
};
globalThis.clearTimeout = id => timers.delete(id);
globalThis.HTMLElement = class {
  attachShadow() { return this.shadowRoot = {querySelector: () => null}; }
};
globalThis.customElements = {get: key => registry.get(key), define: (key, value) => registry.set(key, value)};
globalThis.window = {customCards: []};
globalThis.requestAnimationFrame = () => 0;
await import(pathToFileURL(`${frontend}/power-sync-energy-flow.js`));
const Card = registry.get('power-sync-energy-flow');
const source = fs.readFileSync(`${frontend}/power-sync-strategy.js`, 'utf8');
const helpers = source.slice(source.indexOf('function _loadIncludesGenericEv('), source.indexOf('function _powerFlow('));
const flowConfig = new Function(`${helpers}\nreturn _teslaStyleFlow;`)();
const state = (entity_id, value, attributes = {}) => ({entity_id, state: value, attributes, last_updated: new Date(now).toISOString()});
const row = (id, extra = {}) => ({
  loadpoint_id: id, vehicle_id: id, vehicle_name: id === 'vin-a' ? 'TESSY' : 'W3',
  connected: true, site_presence: 'home', actual_charging: false,
  status: 'connected_idle', current_power_kw: 0, soc: id === 'vin-a' ? 69 : 58,
  confidence: 'observed', ...extra,
});
const snapshot = rows => ({success: true, loadpoints: rows});
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
};
async function advance(ms) {
  now += ms;
  for (const [id, timer] of [...timers]) {
    if (timer.at <= now) {timers.delete(id); timer.fn();}
  }
  await flush();
}
function create(api, {manual = false} = {}) {
  const calls = [];
  const hass = {
    states: {
      'sun.sun': state('sun.sun', 'above_horizon'),
      'weather.home': state('weather.home', 'sunny'),
      'sensor.power_sync_ev_power': state('sensor.power_sync_ev_power', '0', {
        unit_of_measurement: 'kW', vehicle_id: 'vin-a', vehicle_name: 'TESSY',
        is_connected: true, is_charging: false, site_presence: 'home', vehicle_count: 2,
      }),
      'sensor.power_sync_ev_battery_level': state('sensor.power_sync_ev_battery_level', '69'),
    },
    callApi: (method, path) => {calls.push([method, path]); return api();},
    callWS: async () => [],
  };
  const config = flowConfig(key => `sensor.power_sync_${key}`, hass, () => null);
  if (manual) delete config.canonical_ev_loadpoints;
  const card = new Card();
  card._scheduleRender = () => {};
  card.setConfig(config);
  card.hass = hass;
  card.connectedCallback?.();
  return {card, hass, config, calls};
}
function render(card) {
  const result = {texts: {}, hidden: {}, paths: []};
  card.shadowRoot.querySelector = id => ['#ev-node-group', '#ev2-node-group'].includes(id)
    ? {classList: {toggle: (cls, hidden) => {result.hidden[id] = hidden;}}} : null;
  card._setText = (id, value) => {result.texts[id] = value;};
  card._setBackground = background => {result.background = background;};
  for (const key of ['_applySceneFlowPaths', '_applySceneFlowComponents', '_toggleNode', '_beginFlowUpdate', '_commitFlowUpdate', '_syncNodeHalos']) card[key] = () => {};
  card._activatePath = (id, cls, power) => {if (power > 0) result.paths.push({id, cls, power});};
  card._renderDynamic();
  return result;
}

const scenario = process.argv[2];
if (scenario === 'generated-idle' || scenario === 'generated-charging') {
  const charging = scenario === 'generated-charging';
  const {card, calls} = create(async () => snapshot([
    row('vin-b', {actual_charging: charging, current_power_kw: charging ? 3 : 0}),
    row('vin-a', {actual_charging: charging, current_power_kw: charging ? 7 : 0}),
  ]));
  await flush();
  const data = card._collectEvData();
  assert.equal(data.vehicles.length, 2, 'generated flow must show both distinct canonical cars');
  assert.deepEqual(calls, [['GET', 'power_sync/ev/loadpoints/status']]);
  const view = render(card);
  assert.equal(view.background, '/power_sync/frontend/backgrounds/scene_day_clear_dual_charging.png');
  assert.equal(view.hidden['#ev-node-group'], false);
  assert.equal(view.hidden['#ev2-node-group'], false);
  assert.equal(view.texts['#flow-ev-label'], 'TESSY');
  assert.equal(view.texts['#flow-ev2-label'], 'W3');
  assert.equal(view.texts['#flow-ev-pct'], '69%');
  assert.equal(view.texts['#flow-ev2-pct'], '58%');
  assert.equal(view.texts['#flow-ev-power'], charging ? '7.0 kW' : '0 W');
  assert.equal(view.texts['#flow-ev2-power'], charging ? '3.0 kW' : '0 W');
} else if (['away', 'unplugged', 'empty'].includes(scenario)) {
  const rows = scenario === 'empty' ? [] : [row('vin-a'), row('vin-b', scenario === 'away'
    ? {site_presence: 'away', actual_charging: true, current_power_kw: 7}
    : {connected: false, actual_charging: true, current_power_kw: 7})];
  const {card} = create(async () => snapshot(rows));
  await flush();
  const expected = scenario === 'empty' ? 0 : 1;
  assert.equal(card._collectEvData().vehicles.length, expected);
  const view = render(card);
  assert.ok(!view.background.includes('dual'));
  assert.equal(view.hidden['#ev2-node-group'], true);
  if (expected === 0) {
    assert.equal(view.hidden['#ev-node-group'], true);
    assert.ok(view.background.endsWith('_idle.png'));
  }
} else if (scenario === 'unknown-power') {
  const {card} = create(async () => snapshot([
    row('vin-a', {actual_charging: true, current_power_kw: null, soc: null, confidence: 'unknown'}),
    row('vin-b', {actual_charging: true, current_power_kw: 7, confidence: 'commanded'}),
  ]));
  await flush();
  const view = render(card);
  assert.equal(view.texts['#flow-ev-power'], '--');
  assert.equal(view.texts['#flow-ev2-power'], '--');
  assert.equal(view.texts['#flow-ev-pct'], '--%');
  assert.equal(card._collectEvData().totalDrawPower, 0);
} else if (scenario === 'duplicates-stable') {
  let rows = [row('vin-b'), row('VIN-A', {loadpoint_id: 'fleet-a', vehicle_name: 'TESSY', current_power_kw: 1}), row('vin-a', {loadpoint_id: 'ble-a', current_power_kw: 1})];
  const {card} = create(async () => snapshot(rows));
  await flush();
  assert.equal(card._collectEvData().vehicles.length, 2);
  assert.equal(card._collectEvData().totalDrawPower, 1000);
  const before = card._collectEvData().vehicles.map(v => v.identity);
  rows = rows.slice().reverse();
  await advance(30000);
  assert.deepEqual(card._collectEvData().vehicles.map(v => v.identity), before);
  assert.ok(before.every(Boolean));
} else if (scenario === 'failed-fallback' || scenario === 'malformed-fallback') {
  const {card, calls} = create(async () => {
    if (scenario === 'failed-fallback') throw new Error('API unavailable');
    return {success: false, loadpoints: []};
  });
  await flush();
  assert.equal(card._collectEvData().vehicles.length, 1);
  assert.equal(card._collectEvData().vehicles[0].labelText, 'TESSY');
  await advance(29999);
  assert.equal(calls.length, 1);
  await advance(1);
  assert.equal(calls.length, 2);
} else if (scenario === 'manual-no-poll') {
  const {card, calls} = create(async () => snapshot([row('vin-a'), row('vin-b')]), {manual: true});
  await flush();
  await advance(120000);
  assert.equal(calls.length, 0);
  assert.equal(card._collectEvData().vehicles.length, 1);
} else if (scenario === 'cadence-overlap-disconnect') {
  const pending = deferred();
  const {card, hass, calls} = create(() => pending.promise);
  for (let i = 0; i < 10; i++) card.hass = {...hass};
  await advance(120000);
  assert.equal(calls.length, 1, 'pending read must never overlap another read');
  card.disconnectedCallback();
  pending.resolve(snapshot([row('vin-a'), row('vin-b')]));
  await flush();
  assert.equal(card._canonicalEvSnapshot, null);
  assert.equal(timers.size, 0, 'disconnected card must not poll');
  await advance(120000);
  assert.equal(calls.length, 1);
} else if (scenario === 'config-change-in-flight') {
  const pending = deferred();
  const {card, config, calls} = create(() => pending.promise);
  card.setConfig({...config, canonical_ev_loadpoints: false});
  pending.resolve(snapshot([row('vin-a'), row('vin-b')]));
  await flush();
  assert.equal(card._canonicalEvSnapshot, null);
  assert.equal(card._collectEvData().vehicles.length, 1);
  await advance(120000);
  assert.equal(calls.length, 1);
} else if (scenario === 'enabled-config-change-in-flight') {
  const old = deferred();
  let reads = 0;
  const {card, config, calls} = create(() => ++reads === 1
    ? old.promise : Promise.resolve(snapshot([row('vin-b')])));
  card.setConfig({...config, ev_label: 'Changed configuration'});
  old.resolve(snapshot([row('vin-a'), row('vin-b')]));
  await flush();
  assert.equal(calls.length, 2);
  assert.deepEqual(card._collectEvData().vehicles.map(v => v.identity), ['vin-b']);
} else if (scenario === 'reconnect-in-flight') {
  const old = deferred();
  let reads = 0;
  const {card, calls} = create(() => ++reads === 1 ? old.promise : Promise.resolve(snapshot([row('vin-a')])));
  card.disconnectedCallback();
  card.connectedCallback();
  assert.equal(calls.length, 1);
  old.resolve(snapshot([row('vin-a'), row('vin-b')]));
  await flush();
  assert.equal(calls.length, 2);
  assert.equal(card._collectEvData().vehicles.length, 1, 'old connection response must not restore the second car');
} else if (scenario === 'cache-expiry' || scenario === 'cache-expiry-pending') {
  const pending = deferred();
  let reads = 0;
  const {card} = create(() => ++reads === 1
    ? Promise.resolve(snapshot([row('vin-a'), row('vin-b')])) : pending.promise);
  await flush();
  assert.equal(card._collectEvData().vehicles.length, 2);
  if (scenario === 'cache-expiry-pending') {
    await advance(30000);
    assert.equal(reads, 2);
    await advance(60000);
    assert.equal(card._canonicalEvSnapshot, null, 'expired cache must clear even while a read hangs');
  } else {
    now += 90001;
  }
  assert.equal(card._collectEvData().vehicles.length, 1, 'expired browser cache must use entity fallback');
} else if (scenario === 'connection-change-in-flight') {
  const old = deferred();
  let reads = 0;
  const {card, hass, calls} = create(() => ++reads === 1
    ? old.promise : Promise.resolve(snapshot([row('vin-a')])));
  card.hass = {...hass, connection: {}};
  assert.equal(calls.length, 1);
  old.resolve(snapshot([row('vin-a'), row('vin-b')]));
  await flush();
  assert.equal(calls.length, 2);
  assert.equal(card._collectEvData().vehicles.length, 1);
} else if (scenario === 'selector-independent') {
  const {card} = create(async () => snapshot([row('vin-a'), row('vin-b')]));
  await flush();
  const labels = card._collectEvData().vehicles.map(v => v.labelText);
  card.hass = {...card._hass, states: {...card._hass.states,
    'sensor.power_sync_ev_power': state('sensor.power_sync_ev_power', '7', {
      unit_of_measurement: 'kW', vehicle_id: 'vin-b', vehicle_name: 'W3',
      is_connected: true, is_charging: true, site_presence: 'home',
    }),
  }};
  assert.deepEqual(card._collectEvData().vehicles.map(v => v.labelText), labels);
} else if (scenario === 'conflicting-duplicate-presence') {
  const {card} = create(async () => snapshot([
    row('vin-a'), row('VIN-A', {site_presence: 'away'}), row('vin-b'),
  ]));
  await flush();
  assert.deepEqual(card._collectEvData().vehicles.map(v => v.identity), ['vin-b']);
} else if (scenario === 'two-car-weather-scenes') {
  const {card, hass} = create(async () => snapshot([row('vin-a'), row('vin-b')]));
  await flush();
  for (const period of ['day', 'night']) {
    for (const weather of ['sunny', 'cloudy', 'rainy', 'snowy', 'lightning']) {
      hass.states['sun.sun'].state = period === 'day' ? 'above_horizon' : 'below_horizon';
      hass.states['weather.home'].state = weather;
      const view = render(card);
      assert.ok(view.background.includes(`scene_${period}_`));
      assert.ok(view.background.endsWith('_dual_charging.png'));
      assert.equal(view.hidden['#ev2-node-group'], false);
      const file = view.background.split('/').pop();
      assert.ok(card._sceneFlowComponentProfile(file)['ev2-label']);
      assert.ok(card._sceneFlowPathMap()[file]['line-wallbox-ev2']);
    }
  }
} else if (scenario === 'distinct-car-power-flows') {
  const {card, hass} = create(async () => snapshot([
    row('vin-a', {actual_charging: true, current_power_kw: 7}),
    row('vin-b', {actual_charging: true, current_power_kw: 3}),
  ]));
  for (const [name, power] of [['solar_power', 0], ['grid_power', 10], ['battery_power', 0], ['home_load', 0]]) {
    const id = `sensor.power_sync_${name}`;
    hass.states[id] = state(id, String(power), {unit_of_measurement: 'kW'});
  }
  await flush();
  const view = render(card);
  assert.equal(card._collectEvData().totalDrawPower, 10000);
  assert.equal(view.paths.find(path => path.id === 'line-wallbox-ev')?.power, 7000);
  assert.equal(view.paths.find(path => path.id === 'line-wallbox-ev2')?.power, 3000);
} else if (scenario === 'generated-layouts') {
  // The board loader is the only substituted dependency; run real generation
  // and _boardCards for both layout branches rather than asserting source text.
  const Strategy = new Function(`${source}\nreturn PowerSyncStrategy;`)();
  const {hass} = create(async () => snapshot([]));
  hass.states['sensor.power_sync_solar_power'] = state('sensor.power_sync_solar_power', '1', {unit_of_measurement: 'kW'});
  for (const board of [false, true]) {
    const GeneratedStrategy = new Function('board', `${source}\n_loadPowerSyncBoard = async () => board; return PowerSyncStrategy;`)(board);
    const config = await GeneratedStrategy.generate({}, hass);
    const find = node => {
      if (!node || typeof node !== 'object') return null;
      if (node.type === 'custom:power-sync-energy-flow') return node;
      for (const value of Object.values(node)) {
        const found = Array.isArray(value) ? value.map(find).find(Boolean) : find(value);
        if (found) return found;
      }
      return null;
    };
    assert.equal(find(config)?.canonical_ev_loadpoints, true, `missing canonical mode in ${board ? 'board' : 'classic'} layout`);
  }
  assert.ok(Strategy);
} else {
  throw new Error(`Unknown scenario: ${scenario}`);
}
console.log(`PASS ${scenario}`);
