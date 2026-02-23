/**
 * PowerSync Dynamic Dashboard Strategy
 *
 * A Lovelace strategy that dynamically generates dashboard cards based on
 * which power_sync entities actually exist in the HA instance.
 *
 * Usage in dashboard YAML:
 *   strategy:
 *     type: custom:power-sync-strategy
 *   views: []
 *
 * Optional config:
 *   strategy:
 *     type: custom:power-sync-strategy
 *     entity_prefix: "power_sync"   # override entity prefix (default: auto-detect)
 */

// ─── PowerSyncChart Custom Element ──────────────────────────────
// Self-contained SVG chart that reads data from HA entity attributes.
// Replaces apexcharts-card data_generator usage (broken in apexcharts v2.2.0+).
// Two modes: 'tou' (24h schedule) and 'forecast' (48h from entity arrays).

class PowerSyncChart extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = null;
    this._hass = null;
  }

  setConfig(config) {
    this._config = config;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 4;
  }

  _render() {
    if (!this._config || !this._hass) return;

    const config = this._config;
    const hass = this._hass;
    const mode = config.mode || 'forecast';

    // Gather all series data
    let allSeries;
    if (mode === 'tou') {
      allSeries = this._getTouData(config, hass);
    } else {
      allSeries = this._getForecastData(config, hass);
    }

    // Compute chart dimensions
    const W = 600, H = 220;
    const pad = { top: 30, right: 20, bottom: 50, left: 55 };
    const chartW = W - pad.left - pad.right;
    const chartH = H - pad.top - pad.bottom;

    // Compute x-axis range
    let xMin = Infinity, xMax = -Infinity;
    for (const s of allSeries) {
      for (const [t] of s.data) {
        if (t < xMin) xMin = t;
        if (t > xMax) xMax = t;
      }
    }
    if (!isFinite(xMin) || !isFinite(xMax) || xMin === xMax) {
      xMin = Date.now();
      xMax = xMin + 3600000;
    }

    // Compute y-axis range
    const yMultiplier = config.yMultiplier || 1;
    let rawMin = Infinity, rawMax = -Infinity;
    for (const s of allSeries) {
      for (const [, v] of s.data) {
        const scaled = v * yMultiplier;
        if (scaled < rawMin) rawMin = scaled;
        if (scaled > rawMax) rawMax = scaled;
      }
    }
    if (!isFinite(rawMin)) { rawMin = 0; rawMax = 1; }
    if (rawMin === rawMax) { rawMin -= 1; rawMax += 1; }

    // Add 5% padding above max so lines don't touch the top edge
    const dataRange = rawMax - rawMin;
    rawMax += dataRange * 0.05;

    // Apply explicit yMin/yMax if set
    if (config.yMin !== undefined) rawMin = config.yMin * yMultiplier;
    if (config.yMax !== undefined) rawMax = config.yMax * yMultiplier;

    // Nice tick calculation
    const yRange = rawMax - rawMin;
    const tickTarget = 5;
    const rawStep = yRange / tickTarget;
    // Guard against zero/negative step (all data identical after padding)
    const safeStep = rawStep > 0 ? rawStep : 1;
    const mag = Math.pow(10, Math.floor(Math.log10(safeStep)));
    const residual = safeStep / mag;
    let niceStep;
    if (residual <= 1.5) niceStep = 1 * mag;
    else if (residual <= 3) niceStep = 2 * mag;
    else if (residual <= 7) niceStep = 5 * mag;
    else niceStep = 10 * mag;

    const yMin = Math.floor(rawMin / niceStep) * niceStep;
    const yMax = Math.ceil(rawMax / niceStep) * niceStep;
    const ticks = [];
    for (let v = yMin; v <= yMax + niceStep * 0.01; v += niceStep) {
      ticks.push(Math.round(v * 1000) / 1000);
    }
    // Safety: cap at 20 ticks to prevent runaway loops from floating point
    if (ticks.length > 20) ticks.length = 20;

    // Coordinate transforms
    const xScale = (t) => pad.left + ((t - xMin) / (xMax - xMin)) * chartW;
    const yScale = (v) => pad.top + chartH - ((v - yMin) / (yMax - yMin)) * chartH;

    // Build SVG content
    let svg = '';

    // Grid lines
    for (const tick of ticks) {
      const y = yScale(tick);
      svg += `<line x1="${pad.left}" y1="${y}" x2="${W - pad.right}" y2="${y}" stroke="var(--divider-color, #e0e0e0)" stroke-width="0.5" stroke-dasharray="4,3"/>`;
      const label = config.yUnit === '¢'
        ? tick.toFixed(tick === Math.round(tick) ? 0 : 1) + '¢'
        : tick.toFixed(tick === Math.round(tick) ? 0 : 1) + ' ' + (config.yUnit || '');
      svg += `<text x="${pad.left - 8}" y="${y + 4}" text-anchor="end" font-size="11" fill="var(--secondary-text-color, #888)">${label}</text>`;
    }

    // X-axis labels
    const spanHours = (xMax - xMin) / 3600000;
    let xTickInterval;
    if (spanHours <= 6) xTickInterval = 1;
    else if (spanHours <= 12) xTickInterval = 2;
    else if (spanHours <= 24) xTickInterval = 3;
    else if (spanHours <= 36) xTickInterval = 6;
    else xTickInterval = 8;

    const startDate = new Date(xMin);
    const firstHour = new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate(), Math.ceil(startDate.getHours() / xTickInterval) * xTickInterval);
    for (let t = firstHour.getTime(); t <= xMax; t += xTickInterval * 3600000) {
      const x = xScale(t);
      if (x < pad.left || x > W - pad.right) continue;
      const d = new Date(t);
      let label;
      if (spanHours > 24) {
        const day = d.toLocaleDateString([], { weekday: 'short' });
        label = day + ' ' + String(d.getHours()).padStart(2, '0') + ':00';
      } else {
        label = String(d.getHours()).padStart(2, '0') + ':00';
      }
      svg += `<line x1="${x}" y1="${pad.top}" x2="${x}" y2="${pad.top + chartH}" stroke="var(--divider-color, #e0e0e0)" stroke-width="0.3"/>`;
      svg += `<text x="${x}" y="${H - pad.bottom + 18}" text-anchor="middle" font-size="10" fill="var(--secondary-text-color, #888)">${label}</text>`;
    }

    // Chart border
    svg += `<rect x="${pad.left}" y="${pad.top}" width="${chartW}" height="${chartH}" fill="none" stroke="var(--divider-color, #e0e0e0)" stroke-width="0.5"/>`;

    // Series paths
    for (const series of allSeries) {
      if (series.data.length === 0) continue;
      const step = config.stepLine;
      let pathD = '';

      for (let i = 0; i < series.data.length; i++) {
        const [t, v] = series.data[i];
        const x = xScale(t);
        const y = yScale(v * yMultiplier);
        if (i === 0) {
          pathD += `M${x},${y}`;
        } else if (step) {
          const prevX = xScale(series.data[i - 1][0]);
          const prevY = yScale(series.data[i - 1][1] * yMultiplier);
          pathD += `H${x}V${y}`;
        } else {
          pathD += `L${x},${y}`;
        }
      }

      // Fill area if requested
      if (series.fill) {
        const baseline = yScale(Math.max(0, yMin));
        const first = series.data[0];
        const last = series.data[series.data.length - 1];
        const fillD = pathD + `L${xScale(last[0])},${baseline}L${xScale(first[0])},${baseline}Z`;
        svg += `<path d="${fillD}" fill="${series.color}" opacity="0.2"/>`;
      }

      // Stroke
      svg += `<path d="${pathD}" fill="none" stroke="${series.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    }

    // "Now" marker line for forecast mode
    if (mode === 'forecast') {
      const nowX = xScale(Date.now());
      if (nowX >= pad.left && nowX <= W - pad.right) {
        svg += `<line x1="${nowX}" y1="${pad.top}" x2="${nowX}" y2="${pad.top + chartH}" stroke="var(--primary-color, #03a9f4)" stroke-width="1" stroke-dasharray="4,2" opacity="0.6"/>`;
        svg += `<text x="${nowX}" y="${pad.top - 4}" text-anchor="middle" font-size="9" fill="var(--primary-color, #03a9f4)">Now</text>`;
      }
    }

    // Title
    const title = config.title || '';
    svg += `<text x="${W / 2}" y="16" text-anchor="middle" font-size="13" font-weight="600" fill="var(--primary-text-color, #333)">${this._escSvg(title)}</text>`;

    // Legend
    const legendY = H - 8;
    const legendItems = allSeries.map(s => ({ name: s.name, color: s.color }));
    const legendTotalWidth = legendItems.length * 90;
    let legendX = (W - legendTotalWidth) / 2;
    for (const item of legendItems) {
      svg += `<rect x="${legendX}" y="${legendY - 8}" width="12" height="3" rx="1.5" fill="${item.color}"/>`;
      svg += `<text x="${legendX + 16}" y="${legendY - 4}" font-size="11" fill="var(--secondary-text-color, #888)">${this._escSvg(item.name)}</text>`;
      legendX += 90;
    }

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
        }
        .card {
          background: var(--ha-card-background, var(--card-background-color, white));
          border-radius: var(--ha-card-border-radius, 12px);
          box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,0.1));
          padding: 12px;
          overflow: hidden;
        }
        svg {
          width: 100%;
          height: auto;
        }
        .no-data {
          text-align: center;
          color: var(--secondary-text-color, #888);
          padding: 24px 0;
          font-size: 14px;
        }
      </style>
      <div class="card">
        ${allSeries.every(s => s.data.length === 0)
          ? `<div class="no-data">${this._escHtml(title)}<br>No data available</div>`
          : `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">${svg}</svg>`
        }
      </div>
    `;
  }

  _getTouData(config, hass) {
    const entityId = config.entity;
    const stateObj = entityId ? hass.states[entityId] : null;
    if (!stateObj) return (config.series || []).map(s => ({ name: s.name, color: s.color, fill: false, data: [] }));

    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const currentDow = now.getDay();
    const endOfDay = today.getTime() + 24 * 3600000 - 1;

    return (config.series || []).map(s => {
      const key = s.key;
      let data = [];

      // Format 1: schedule array with {time, buy, sell}
      const schedule = stateObj.attributes?.schedule;
      if (Array.isArray(schedule) && schedule.length > 0) {
        data = schedule.map(entry => {
          const [hours, mins] = String(entry.time).split(':').map(Number);
          const ts = today.getTime() + hours * 3600000 + (mins || 0) * 60000;
          return [ts, entry[key] || 0];
        });
        if (data.length > 0) {
          data.push([endOfDay, data[data.length - 1][1]]);
        }
        return { name: s.name, color: s.color, fill: false, data };
      }

      // Format 2: tou_schedule array with periods and windows
      const touSchedule = stateObj.attributes?.tou_schedule;
      if (Array.isArray(touSchedule) && touSchedule.length > 0) {
        const hourlyPrices = new Array(24).fill(null);
        touSchedule.forEach(period => {
          const windows = period.windows || [];
          windows.forEach(w => {
            if (currentDow >= w.from_day && currentDow <= w.to_day) {
              const fromHour = w.from_hour || 0;
              const toHour = w.to_hour || 24;
              if (fromHour <= toHour) {
                for (let h = fromHour; h < toHour && h < 24; h++) {
                  hourlyPrices[h] = period[key];
                }
              } else {
                for (let h = fromHour; h < 24; h++) hourlyPrices[h] = period[key];
                for (let h = 0; h < toHour; h++) hourlyPrices[h] = period[key];
              }
            }
          });
        });
        const defaultPrice = stateObj.attributes?.[key + '_price'] || touSchedule[0]?.[key] || 0;
        for (let h = 0; h < 24; h++) {
          if (hourlyPrices[h] === null) hourlyPrices[h] = defaultPrice;
          data.push([today.getTime() + h * 3600000, hourlyPrices[h]]);
        }
        data.push([endOfDay, hourlyPrices[23]]);
        return { name: s.name, color: s.color, fill: false, data };
      }

      // Format 3: flat price attribute
      const price = stateObj.attributes?.[key + '_price'];
      if (price !== undefined) {
        data = [
          [today.getTime(), price],
          [endOfDay, price],
        ];
      }

      return { name: s.name, color: s.color, fill: false, data };
    });
  }

  _getForecastData(config, hass) {
    const interval = (config.intervalMinutes || 5) * 60 * 1000;
    const now = Date.now();
    const start = Math.floor(now / interval) * interval;

    return (config.series || []).map(s => {
      const stateObj = s.entity ? hass.states[s.entity] : null;
      const attr = s.attribute || 'forecast_values_kw';
      const values = stateObj?.attributes?.[attr];
      let data = [];
      if (Array.isArray(values)) {
        data = values.map((v, i) => [start + i * interval, v]);
      }
      return { name: s.name, color: s.color, fill: !!s.fill, data };
    });
  }

  _escSvg(str) {
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  _escHtml(str) {
    return this._escSvg(str);
  }
}

if (!customElements.get('power-sync-chart')) {
  customElements.define('power-sync-chart', PowerSyncChart);
}

// ─── Dashboard Strategy ─────────────────────────────────────────

class PowerSyncStrategy {
  static async generate(config, hass) {
    // Check for HACS custom elements — use synchronous check first (instant if loaded),
    // then check Lovelace resources as fallback (installed but not yet loaded).
    // Never block dashboard generation on element loading — always generate cards
    // and let HA show "custom element not found" if truly missing.
    const requiredCards = [
      { element: 'button-card', name: 'button-card', hacs: 'button-card' },
      { element: 'apexcharts-card', name: 'apexcharts-card', hacs: 'apexcharts-card' },
      { element: 'power-flow-card-plus', name: 'power-flow-card-plus', hacs: 'power-flow-card-plus' },
    ];

    // Check if resource is registered in Lovelace (works even before element loads)
    const lovelaceResources = [];
    try {
      const lr = await hass.callWS({ type: 'lovelace/resources' });
      if (Array.isArray(lr)) lovelaceResources.push(...lr);
    } catch (_) { /* YAML mode — skip */ }

    const loaded = {};
    for (const c of requiredCards) {
      // Already registered as custom element
      if (customElements.get(c.element)) {
        loaded[c.element] = true;
        continue;
      }
      // Registered as Lovelace resource (installed via HACS, just not loaded yet)
      const inResources = lovelaceResources.some(r =>
        r.url && r.url.includes(c.element.replace(/-/g, ''))
      );
      loaded[c.element] = inResources;
    }

    const missing = requiredCards.filter(c => !loaded[c.element]);
    // Always generate cards — HA handles missing custom elements gracefully
    const hasApex = true;
    const hasButton = true;
    const hasFlowCard = true;

    // Entity resolver — tries power_sync_ prefixed first, then bare name.
    // Handles mixed installs where some entities have the prefix and others don't.
    const e = (name) => {
      const prefixed = `sensor.power_sync_${name}`;
      if (hass.states[prefixed]) return prefixed;
      const bare = `sensor.${name}`;
      if (hass.states[bare]) return bare;
      // Default to prefixed (modern convention)
      return prefixed;
    };

    // Entity existence + availability helper
    const has = (id) => {
      const s = hass.states[id];
      return s && s.state !== 'unavailable' && s.state !== 'unknown';
    };

    // Shorthand: resolve then check
    const hasE = (name) => has(e(name));

    // ── 3-column layout: left (controls/status), center (flow/charts), right (prices/energy) ──
    const left = [];
    const center = [];
    const right = [];

    // --- Left Column: Price Gauges ---
    if (hasE('current_import_price')) {
      left.push(_priceGauges(e));
    }

    // --- Left Column: Battery Controls (requires button-card) ---
    if (hasButton && (hasE('battery_level') || hasE('battery_power'))) {
      left.push(_batteryControls());
    }

    // --- Left Column: Optimizer Status (requires button-card) ---
    if (hasButton && hasE('optimization_status')) {
      left.push(_optimizerStatus(e));
    }

    // --- Center Column: Power Flow (requires power-flow-card-plus) ---
    if (hasFlowCard && hasE('solar_power')) {
      center.push(_powerFlow(e));
    }

    // --- Right Column: Price Chart (Amber/Octopus 24h) — requires apexcharts ---
    if (hasApex && hasE('current_import_price')) {
      right.push(_priceChart(e));
    }

    // --- Right Column: TOU Schedule (uses PowerSyncChart) ---
    if (hasE('tariff_schedule')) {
      right.push(_touSchedule(e));
    }

    // --- Center Column: LP Forecast Summary ---
    if (hasE('lp_solar_forecast')) {
      center.push(_lpForecastSummary(e));
    }

    // --- Center Column: LP Price Chart (48h) ---
    if (hasE('lp_solar_forecast')) {
      center.push(_lpPriceChart(e));
    }

    // --- Right Column: LP Solar & Load Chart (48h) ---
    if (hasE('lp_solar_forecast')) {
      right.push(_lpSolarLoadChart(e));
    }

    // --- Left Column: Curtailment Status (requires button-card) ---
    const hasDC = hasE('solar_curtailment');
    const hasAC = hasE('inverter_status');
    if (hasButton && (hasDC || hasAC)) {
      left.push(_curtailmentStatus(e, hasDC, hasAC));
    }

    // --- Left Column: AC Inverter Controls (requires button-card) ---
    if (hasButton && hasAC) {
      left.push(_acInverterControls(e));
    }

    // --- Left Column: FoxESS Sensors ---
    if (hasE('pv1_power')) {
      left.push(_foxessSensors(e));
    }

    // --- Left Column: Battery Health (requires button-card) ---
    if (hasButton && hasE('battery_health')) {
      left.push(_batteryHealth(e));
    }

    // --- Center Column: Energy Charts — Solar & Grid ---
    if (hasApex && hasE('solar_power')) {
      center.push(_energyChart('Solar', e('solar_power'), '#FFD700', { min: '~0' }));
      center.push(_energyChart('Grid', e('grid_power'), '#F44336', {}));
    }

    // --- Right Column: Energy Charts — Battery & Home ---
    if (hasApex && hasE('solar_power')) {
      right.push(_energyChart('Battery', e('battery_power'), '#2196F3', {}));
    }
    if (hasApex && hasE('home_load')) {
      right.push(_energyChart('Home', e('home_load'), '#9C27B0', { min: '~0' }));
    }

    // --- Left Column: Demand Charge ---
    if (hasE('in_demand_charge_period')) {
      left.push(_demandCharge(e));
    }

    // --- Left Column: AEMO Spike ---
    if (hasE('aemo_price')) {
      left.push(_aemoSpike(e));
    }

    // --- Left Column: Flow Power ---
    if (hasE('flow_power_price')) {
      left.push(_flowPower(e));
    }

    // --- Left Column: Missing dependency warning ---
    if (missing.length > 0) {
      left.push({
        type: 'markdown',
        content:
          '**Note:** Some dashboard cards are hidden because these HACS frontend dependencies were not detected:\n\n' +
          missing.map(c => `- **${c.name}** — search "${c.hacs}" in HACS Frontend`).join('\n') + '\n\n' +
          'Install them via [HACS](https://hacs.xyz/) and refresh your browser.',
      });
    }

    // ── Build responsive layout ──
    const columns = [left, center, right].filter(col => col.length > 0);
    let cards;

    if (columns.length <= 1) {
      // Single column — flat list for narrow/simple installs
      cards = left.concat(center, right);
    } else {
      // Multi-column grid via horizontal-stack + vertical-stack
      cards = [{
        type: 'horizontal-stack',
        cards: columns.map(col => ({
          type: 'vertical-stack',
          cards: col,
        })),
      }];
    }

    return {
      views: [{
        title: 'Energy Dashboard',
        path: 'energy',
        icon: 'mdi:lightning-bolt',
        cards,
      }],
    };
  }
}

// ─── Helpers ─────────────────────────────────────────────────

// ─── Section Builders ────────────────────────────────────────

function _priceGauges(e) {
  return {
    type: 'horizontal-stack',
    cards: [
      {
        type: 'gauge',
        entity: e('current_import_price'),
        name: 'Import',
        unit: '$/kWh',
        min: 0,
        max: 0.6,
        needle: true,
        severity: { green: 0, yellow: 0.25, red: 0.4 },
        card_mod: { style: 'ha-card { height: 140px; }' },
      },
      {
        type: 'gauge',
        entity: e('current_export_price'),
        name: 'Export Earnings',
        unit: '$/kWh',
        min: -0.1,
        max: 0.3,
        needle: true,
        severity: { red: -0.1, yellow: 0, green: 0.05 },
        card_mod: { style: 'ha-card { height: 140px; }' },
      },
      {
        type: 'gauge',
        entity: e('battery_level'),
        name: 'Battery',
        unit: '%',
        min: 0,
        max: 100,
        needle: true,
        severity: { red: 0, yellow: 30, green: 60 },
        card_mod: { style: 'ha-card { height: 140px; }' },
      },
    ],
  };
}

function _batteryControls() {
  const chipStyle = (bg) => ({
    card: [
      { height: '36px' },
      { 'border-radius': '18px' },
      { padding: '0px 12px' },
      { background: bg },
    ],
    grid: [
      { 'grid-template-areas': '"i n"' },
      { 'grid-template-columns': '20px 1fr' },
      { 'align-items': 'center' },
      { padding: '2px 2px' },
    ],
    icon: [
      { 'grid-area': 'i' },
      { 'justify-self': 'start' },
      { width: '24px' },
      { height: '24px' },
      { color: 'var(--green-color, #4CAF50)' },
    ],
    name: [
      { 'grid-area': 'n' },
      { 'font-size': '14px' },
      { 'padding-left': '12px' },
      { 'font-weight': '600' },
    ],
  });

  const blueChip = chipStyle('rgba(var(--rgb-blue-color, 33, 150, 243), 0.1)');
  const orangeChip = chipStyle('rgba(var(--rgb-orange-color, 255, 152, 0), 0.1)');

  return {
    type: 'vertical-stack',
    cards: [
      {
        square: false,
        type: 'grid',
        columns: 4,
        cards: [
          {
            type: 'custom:button-card',
            entity: 'select.power_sync_force_charge_duration',
            show_name: true,
            show_icon: true,
            icon: 'mdi:timer-outline',
            name: "[[[ return (states['select.power_sync_force_charge_duration'] ? states['select.power_sync_force_charge_duration'].state : '30') + ' min' ]]]",
            styles: blueChip,
            tap_action: { action: 'more-info' },
          },
          {
            type: 'custom:button-card',
            name: 'Charge',
            icon: 'mdi:battery-charging',
            styles: blueChip,
            tap_action: {
              action: 'call-service',
              service: 'power_sync.force_charge',
              data: {
                duration: "[[[ return (states['select.power_sync_force_charge_duration'] ? states['select.power_sync_force_charge_duration'].state : '30'); ]]]",
              },
              confirmation: {
                text: "[[[ return 'Force charge for ' + (states['select.power_sync_force_charge_duration'] ? states['select.power_sync_force_charge_duration'].state : '30') + ' minutes?' ]]]",
              },
            },
          },
          {
            type: 'custom:button-card',
            entity: 'select.power_sync_force_discharge_duration',
            show_name: true,
            show_icon: true,
            icon: 'mdi:timer-outline',
            name: "[[[ return (states['select.power_sync_force_discharge_duration'] ? states['select.power_sync_force_discharge_duration'].state : '30') + ' min' ]]]",
            styles: orangeChip,
            tap_action: { action: 'more-info' },
          },
          {
            type: 'custom:button-card',
            name: 'Discharge',
            icon: 'mdi:battery-arrow-down',
            styles: {
              ...orangeChip,
              name: [
                { 'grid-area': 'n' },
                { 'font-size': '12px' },
                { 'padding-left': '12px' },
                { 'font-weight': '600' },
              ],
            },
            tap_action: {
              action: 'call-service',
              service: 'power_sync.force_discharge',
              data: {
                duration: "[[[ return (states['select.power_sync_force_discharge_duration'] ? states['select.power_sync_force_discharge_duration'].state : '30'); ]]]",
              },
              confirmation: {
                text: "[[[ return 'Force discharge for ' + (states['select.power_sync_force_discharge_duration'] ? states['select.power_sync_force_discharge_duration'].state : '30') + ' minutes?' ]]]",
              },
            },
          },
        ],
      },
      {
        square: false,
        type: 'grid',
        columns: 1,
        cards: [
          {
            type: 'custom:button-card',
            name: 'Restore',
            icon: 'mdi:battery-sync',
            styles: {
              card: [
                { height: '40px' },
                { 'border-radius': '18px' },
                { padding: '4px 12px' },
                { background: 'rgba(var(--rgb-green-color, 76, 175, 80), 0.1)' },
              ],
              grid: [
                { 'grid-template-areas': '"i n"' },
                { 'grid-template-columns': '24px 1fr' },
              ],
              icon: [
                { 'grid-area': 'i' },
                { width: '24px' },
                { color: 'var(--green-color, #4CAF50)' },
              ],
              name: [
                { 'grid-area': 'n' },
                { 'text-align': 'left' },
                { 'padding-left': '8px' },
                { 'font-weight': '600' },
              ],
            },
            tap_action: {
              action: 'call-service',
              service: 'power_sync.restore_normal',
              confirmation: { text: 'Restore normal battery operation?' },
            },
          },
        ],
      },
    ],
  };
}

function _optimizerStatus(e) {
  const statusEntity = e('optimization_status');
  const nextEntity = e('optimization_next_action');
  return {
    type: 'custom:button-card',
    entity: statusEntity,
    name: 'Optimizer',
    show_icon: true,
    show_name: true,
    show_label: true,
    label: `[[[
      const current = states['${statusEntity}'];
      const next = states['${nextEntity}'];
      if (!current || current.state === 'unavailable' || current.state === 'unknown')
        return 'Not available';
      const action = (current.state || 'idle').replace('_', ' ');
      const powerW = Number(current.attributes?.power_w ?? 0);
      const powerStr = Math.abs(powerW) >= 1000
        ? (powerW / 1000).toFixed(1) + ' kW'
        : Math.round(powerW) + ' W';
      let line1 = action.charAt(0).toUpperCase() + action.slice(1);
      if (powerW) line1 += ' @ ' + powerStr;
      if (next && next.state && next.state !== 'unknown' && next.state !== 'unavailable') {
        const nextAction = (next.state || '').replace('_', ' ');
        const nextTime = next.attributes?.time;
        if (nextAction && nextTime) {
          const d = new Date(nextTime);
          const timeStr = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
          return line1 + '  \\u2192  ' + nextAction + ' at ' + timeStr;
        }
      }
      return line1;
    ]]]`,
    icon: `[[[
      const s = states['${statusEntity}']?.state;
      if (s === 'charge') return 'mdi:battery-charging';
      if (s === 'discharge' || s === 'export') return 'mdi:battery-arrow-down';
      if (s === 'self_consumption') return 'mdi:home-battery';
      return 'mdi:battery-sync';
    ]]]`,
    styles: {
      card: [
        { 'border-radius': '16px' },
        { padding: '12px' },
        {
          background: `[[[
            const s = states['${statusEntity}']?.state;
            if (s === 'charge') return 'linear-gradient(135deg, rgba(33, 150, 243, 0.15) 0%, rgba(33, 150, 243, 0.05) 100%)';
            if (s === 'discharge' || s === 'export') return 'linear-gradient(135deg, rgba(255, 152, 0, 0.15) 0%, rgba(255, 152, 0, 0.05) 100%)';
            if (s === 'self_consumption') return 'linear-gradient(135deg, rgba(76, 175, 80, 0.15) 0%, rgba(76, 175, 80, 0.05) 100%)';
            return 'linear-gradient(135deg, rgba(158, 158, 158, 0.10) 0%, rgba(158, 158, 158, 0.05) 100%)';
          ]]]`,
        },
      ],
      grid: [
        { 'grid-template-areas': '"i n" "i l"' },
        { 'grid-template-columns': 'min-content 1fr' },
        { 'column-gap': '10px' },
        { 'row-gap': '2px' },
        { 'align-items': 'center' },
      ],
      icon: [
        { width: '28px' },
        {
          color: `[[[
            const s = states['${statusEntity}']?.state;
            if (s === 'charge') return 'var(--blue-color, #2196F3)';
            if (s === 'discharge' || s === 'export') return 'var(--orange-color, #FF9800)';
            if (s === 'self_consumption') return 'var(--green-color, #4CAF50)';
            return 'var(--disabled-text-color)';
          ]]]`,
        },
      ],
      name: [
        { 'justify-self': 'start' },
        { 'font-weight': '700' },
        { 'font-size': '16px' },
      ],
      label: [
        { 'justify-self': 'start' },
        { opacity: '0.85' },
        { 'font-size': '14px' },
      ],
    },
    tap_action: { action: 'more-info' },
  };
}

function _powerFlow(e) {
  return {
    type: 'custom:power-flow-card-plus',
    entities: {
      battery: {
        entity: e('battery_power'),
        state_of_charge: e('battery_level'),
        name: 'Battery',
        color_icon: true,
        display_state: 'two_way',
      },
      grid: {
        entity: e('grid_power'),
        name: 'Grid',
        color_icon: true,
        display_state: 'two_way',
      },
      solar: {
        entity: e('solar_power'),
        name: 'Solar',
        color_icon: true,
        display_state: 'one_way',
      },
      home: {
        entity: e('home_load'),
        name: 'Home',
        color_icon: true,
      },
    },
    watt_threshold: 0,
    kw_decimals: 2,
    min_flow_rate: 0.75,
    max_flow_rate: 6,
    display_zero_lines: false,
    clickable_entities: true,
    use_new_flow_rate_model: true,
  };
}

function _priceChart(e) {
  return {
    type: 'custom:apexcharts-card',
    header: { show: true, title: 'Electricity Prices - 24 Hours', show_states: false },
    graph_span: '24h',
    span: { start: 'day' },
    yaxis: [{
      id: 'price',
      min: '~0',
      apex_config: {
        forceNiceScale: true,
        tickAmount: 5,
        labels: {
          formatter: "EVAL:function(val) { return (val * 100).toFixed(0) + '¢'; }",
        },
      },
    }],
    series: [
      {
        entity: e('current_import_price'),
        name: 'Import',
        type: 'line',
        color: '#FF9800',
        yaxis_id: 'price',
        stroke_width: 2,
        extend_to: 'now',
        group_by: { func: 'avg', duration: '5min' },
      },
      {
        entity: e('current_export_price'),
        name: 'Export Earnings',
        type: 'line',
        color: '#4CAF50',
        yaxis_id: 'price',
        stroke_width: 2,
        extend_to: 'now',
        group_by: { func: 'avg', duration: '5min' },
      },
    ],
    apex_config: {
      chart: { height: 200 },
      stroke: { curve: 'smooth' },
      legend: { show: true, position: 'bottom' },
      tooltip: {
        x: { format: 'HH:mm' },
        y: {
          formatter: "EVAL:function(value) { if (value === null || value === undefined) return ''; const cents = value * 100; if (Math.abs(cents) >= 1000) { return '$' + value.toFixed(2); } return cents.toFixed(0) + '¢'; }",
        },
      },
    },
  };
}

function _touSchedule(e) {
  return {
    type: 'custom:power-sync-chart',
    title: 'TOU Schedule',
    entity: e('tariff_schedule'),
    mode: 'tou',
    stepLine: true,
    yUnit: '¢',
    series: [
      { key: 'buy', name: 'Buy Price', color: '#FF9800' },
      { key: 'sell', name: 'Sell Price', color: '#4CAF50' },
    ],
  };
}

function _lpForecastSummary(e) {
  return {
    type: 'horizontal-stack',
    cards: [
      { type: 'entity', entity: e('lp_solar_forecast'), name: 'Solar Forecast', icon: 'mdi:solar-power-variant' },
      { type: 'entity', entity: e('lp_load_forecast'), name: 'Load Forecast', icon: 'mdi:home-lightning-bolt' },
      { type: 'entity', entity: e('lp_import_price_forecast'), name: 'Import Price Avg', icon: 'mdi:cash-clock' },
      { type: 'entity', entity: e('lp_export_price_forecast'), name: 'Export Price Avg', icon: 'mdi:cash-clock' },
    ],
  };
}

function _lpSolarLoadChart(e) {
  return {
    type: 'custom:power-sync-chart',
    title: 'LP Forecast - Solar & Load (48h)',
    mode: 'forecast',
    intervalMinutes: 5,
    yUnit: 'kW',
    yMin: 0,
    series: [
      { entity: e('lp_solar_forecast'), attribute: 'forecast_values_kw', name: 'Solar', color: '#FFD700', fill: true },
      { entity: e('lp_load_forecast'), attribute: 'forecast_values_kw', name: 'Load', color: '#9C27B0' },
    ],
  };
}

function _lpPriceChart(e) {
  return {
    type: 'custom:power-sync-chart',
    title: 'LP Forecast - Import & Export Prices (48h)',
    mode: 'forecast',
    intervalMinutes: 5,
    stepLine: true,
    yUnit: '¢',
    yMultiplier: 100,
    series: [
      { entity: e('lp_import_price_forecast'), attribute: 'price_values', name: 'Import', color: '#FF9800' },
      { entity: e('lp_export_price_forecast'), attribute: 'price_values', name: 'Export', color: '#4CAF50' },
    ],
  };
}

function _curtailmentStatus(e, hasDC, hasAC) {
  const cards = [];
  const dcEntity = e('solar_curtailment');
  const acEntity = e('inverter_status');

  if (hasDC) {
    cards.push({
      type: 'custom:button-card',
      entity: dcEntity,
      name: 'DC Solar (Tesla)',
      show_icon: true,
      show_name: true,
      show_label: true,
      label: `[[[
        const state = states['${dcEntity}']?.state;
        if (state === 'Active') return 'CURTAILED - Export blocked';
        return 'Normal - Export allowed';
      ]]]`,
      icon: `[[[
        const state = states['${dcEntity}']?.state;
        return state === 'Active'
          ? 'mdi:solar-power-variant-outline'
          : 'mdi:solar-power-variant';
      ]]]`,
      styles: {
        card: [
          { 'border-radius': '16px' },
          { padding: '12px' },
          {
            background: `[[[
              const state = states['${dcEntity}']?.state;
              return state === 'Active'
                ? 'linear-gradient(135deg, rgba(244, 67, 54, 0.15) 0%, rgba(244, 67, 54, 0.05) 100%)'
                : 'linear-gradient(135deg, rgba(76, 175, 80, 0.15) 0%, rgba(76, 175, 80, 0.05) 100%)';
            ]]]`,
          },
        ],
        grid: [
          { 'grid-template-areas': '"i n" "i l"' },
          { 'grid-template-columns': 'min-content 1fr' },
          { 'column-gap': '10px' },
          { 'row-gap': '2px' },
          { 'align-items': 'center' },
        ],
        icon: [
          { width: '28px' },
          {
            color: `[[[
              const state = states['${dcEntity}']?.state;
              return state === 'Active' ? 'var(--red-color)' : 'var(--green-color)';
            ]]]`,
          },
        ],
        name: [
          { 'justify-self': 'start' },
          { 'font-weight': '700' },
          { 'font-size': '16px' },
        ],
        label: [
          { 'justify-self': 'start' },
          { opacity: '0.85' },
          { 'font-size': '14px' },
        ],
      },
      tap_action: { action: 'more-info' },
    });
  }

  if (hasAC) {
    cards.push({
      type: 'custom:button-card',
      entity: acEntity,
      name: 'AC Inverter',
      show_icon: true,
      show_name: true,
      show_label: true,
      label: `[[[
        const stateObj = states['${acEntity}'];
        const raw = (stateObj?.state ?? 'unknown').toLowerCase();
        const power = stateObj?.attributes?.power_limit_percent;
        const powerW = Number(stateObj?.attributes?.power_output_w ?? 0);
        const brand = stateObj?.attributes?.brand;
        const running = (stateObj?.attributes?.running_state ?? '').toLowerCase();
        const isNight = states['sun.sun']?.state === 'below_horizon';
        const isSleep = isNight && ((powerW < 100) || (running === 'stopped')) && !['offline','error','unavailable','unknown'].includes(raw);
        const state = isSleep ? 'sleep' : raw;
        if (state === 'unavailable' || state === 'unknown') return 'Not configured';
        if (state === 'curtailed') return 'CURTAILED' + (power ? ' - ' + power + '%' : '');
        if (state === 'sleep') return 'Sleep' + (powerW > 0 ? ' (PID recovery)' : '');
        if (state === 'offline') return 'Offline - Cannot reach';
        if (state === 'error') return 'Error - Check logs';
        const title = brand ? (brand.charAt(0).toUpperCase() + brand.slice(1)) : 'Online';
        if (powerW) return title + ' - ' + Math.round(powerW) + 'W';
        if (power) return title + ' - ' + power + '%';
        return title;
      ]]]`,
      icon: `[[[
        const raw = (states['${acEntity}']?.state ?? 'unknown').toLowerCase();
        const powerW = Number(states['${acEntity}']?.attributes?.power_output_w ?? 0);
        const running = (states['${acEntity}']?.attributes?.running_state ?? '').toLowerCase();
        const isNight = states['sun.sun']?.state === 'below_horizon';
        const isSleep = isNight && ((powerW < 100) || (running === 'stopped')) && !['offline','error','unavailable','unknown'].includes(raw);
        if (isSleep) return 'mdi:moon-waning-crescent';
        if (raw === 'error') return 'mdi:alert-octagon';
        return 'mdi:solar-panel';
      ]]]`,
      styles: {
        card: [
          { 'border-radius': '16px' },
          { padding: '12px' },
          {
            background: `[[[
              const stateObj = states['${acEntity}'];
              const raw = (stateObj?.state ?? 'unknown').toLowerCase();
              const powerW = Number(stateObj?.attributes?.power_output_w ?? 0);
              const running = (stateObj?.attributes?.running_state ?? '').toLowerCase();
              const isNight = states['sun.sun']?.state === 'below_horizon';
              const isSleep = isNight && ((powerW < 100) || (running === 'stopped')) && !['offline','error','unavailable','unknown'].includes(raw);
              const state = isSleep ? 'sleep' : raw;
              if (state === 'curtailed') return 'linear-gradient(135deg, rgba(244, 67, 54, 0.15) 0%, rgba(244, 67, 54, 0.05) 100%)';
              if (state === 'sleep') return 'linear-gradient(135deg, rgba(96, 125, 139, 0.15) 0%, rgba(96, 125, 139, 0.05) 100%)';
              if (state === 'offline' || state === 'error') return 'linear-gradient(135deg, rgba(255, 152, 0, 0.15) 0%, rgba(255, 152, 0, 0.05) 100%)';
              if (state === 'unavailable' || state === 'unknown') return 'linear-gradient(135deg, rgba(158, 158, 158, 0.10) 0%, rgba(158, 158, 158, 0.05) 100%)';
              return 'linear-gradient(135deg, rgba(76, 175, 80, 0.15) 0%, rgba(76, 175, 80, 0.05) 100%)';
            ]]]`,
          },
        ],
        grid: [
          { 'grid-template-areas': '"i n" "i l"' },
          { 'grid-template-columns': 'min-content 1fr' },
          { 'column-gap': '10px' },
          { 'row-gap': '2px' },
          { 'align-items': 'center' },
        ],
        icon: [
          { width: '28px' },
          {
            color: `[[[
              const raw = (states['${acEntity}']?.state ?? 'unknown').toLowerCase();
              const powerW = Number(states['${acEntity}']?.attributes?.power_output_w ?? 0);
              const running = (states['${acEntity}']?.attributes?.running_state ?? '').toLowerCase();
              const isNight = states['sun.sun']?.state === 'below_horizon';
              const isSleep = isNight && ((powerW < 100) || (running === 'stopped')) && !['offline','error','unavailable','unknown'].includes(raw);
              const state = isSleep ? 'sleep' : raw;
              if (state === 'curtailed') return 'var(--red-color)';
              if (state === 'sleep') return 'var(--blue-grey-color)';
              if (state === 'offline' || state === 'error') return 'var(--orange-color)';
              if (state === 'unavailable' || state === 'unknown') return 'var(--disabled-text-color)';
              return 'var(--green-color)';
            ]]]`,
          },
        ],
        name: [
          { 'justify-self': 'start' },
          { 'font-weight': '700' },
          { 'font-size': '16px' },
        ],
        label: [
          { 'justify-self': 'start' },
          { opacity: '0.85' },
          { 'font-size': '14px' },
        ],
      },
      tap_action: { action: 'more-info' },
    });
  }

  return {
    type: 'horizontal-stack',
    cards,
  };
}

function _acInverterControls(e) {
  const acEntity = e('inverter_status');
  const btnStyle = (bg, iconColor) => ({
    card: [
      { height: '40px' },
      { 'border-radius': '18px' },
      { padding: '0px 12px' },
      { background: bg },
    ],
    grid: [
      { 'grid-template-areas': '"i n"' },
      { 'grid-template-columns': 'min-content auto' },
    ],
    icon: [
      { color: iconColor },
      { width: '20px' },
    ],
    name: [
      { 'font-size': '12px' },
      { 'font-weight': '600' },
      { 'white-space': 'nowrap' },
    ],
  });

  return {
    type: 'conditional',
    conditions: [
      { condition: 'state', entity: acEntity, state_not: 'unavailable' },
      { condition: 'state', entity: acEntity, state_not: 'unknown' },
    ],
    card: {
      type: 'grid',
      columns: 3,
      square: false,
      cards: [
        {
          type: 'custom:button-card',
          name: 'Load Follow',
          icon: 'mdi:home-lightning-bolt',
          styles: btnStyle('rgba(var(--rgb-orange-color, 255, 152, 0), 0.12)', 'var(--orange-color, #FF9800)'),
          tap_action: {
            action: 'call-service',
            service: 'power_sync.curtail_inverter',
            data: { mode: 'load_following' },
            confirmation: { text: 'Limit inverter to home load only?' },
          },
        },
        {
          type: 'custom:button-card',
          name: 'Shutdown',
          icon: 'mdi:power-plug-off',
          styles: btnStyle('rgba(var(--rgb-red-color, 244, 67, 54), 0.12)', 'var(--red-color, #F44336)'),
          tap_action: {
            action: 'call-service',
            service: 'power_sync.curtail_inverter',
            data: { mode: 'shutdown' },
            confirmation: { text: 'Fully shut down inverter (0% output)?' },
          },
        },
        {
          type: 'custom:button-card',
          name: 'Restore',
          icon: 'mdi:power-plug',
          styles: btnStyle('rgba(var(--rgb-green-color, 76, 175, 80), 0.12)', 'var(--green-color, #4CAF50)'),
          tap_action: {
            action: 'call-service',
            service: 'power_sync.restore_inverter',
            confirmation: { text: 'Restore inverter to normal operation?' },
          },
        },
      ],
    },
  };
}

function _foxessSensors(e) {
  const entities = [
    { entity: e('pv1_power'), name: 'PV1 Power' },
    { entity: e('pv2_power'), name: 'PV2 Power' },
  ];
  // Only add CT2 if it exists (not all FoxESS models have it)
  entities.push({ entity: e('ct2_power'), name: 'CT2 Power' });
  entities.push({ entity: e('work_mode'), name: 'Work Mode' });
  entities.push({ entity: e('min_soc'), name: 'Min SOC' });
  entities.push({ entity: e('daily_battery_charge_foxess'), name: 'Daily Charge' });
  entities.push({ entity: e('daily_battery_discharge_foxess'), name: 'Daily Discharge' });

  return {
    type: 'entities',
    title: 'FoxESS Inverter',
    show_header_toggle: false,
    entities,
  };
}

function _batteryHealth(e) {
  const healthEntity = e('battery_health');

  const healthGauge = (name, attrPath) => ({
    type: 'custom:button-card',
    entity: healthEntity,
    name,
    show_icon: false,
    show_name: true,
    show_state: true,
    state_display: `[[[
      const v = ${attrPath};
      if (!v || ['unknown','unavailable','none'].includes(String(v).toLowerCase())) return '';
      const n = Number(v);
      if (!Number.isFinite(n)) return '';
      return n.toFixed(1) + ' %';
    ]]]`,
    styles: {
      card: [
        { height: '90px' },
        { 'border-radius': '12px' },
        { padding: '10px' },
        {
          display: `[[[
            const v = ${attrPath};
            if (!v || ['unknown','unavailable','none'].includes(String(v).toLowerCase())) return 'none';
            const n = Number(v);
            return Number.isFinite(n) ? 'block' : 'none';
          ]]]`,
        },
      ],
      name: [
        { 'font-weight': '700' },
        { 'font-size': '13px' },
      ],
      state: [
        { 'font-size': '22px' },
        { 'font-weight': '800' },
        { 'margin-top': '6px' },
      ],
    },
  });

  return {
    type: 'vertical-stack',
    cards: [
      {
        type: 'custom:button-card',
        name: 'Battery Health',
        show_icon: false,
        show_name: true,
        styles: {
          card: [
            { height: '36px' },
            { 'border-radius': '12px' },
            { padding: '0px 12px' },
            { background: 'rgba(var(--rgb-primary-color, 3, 169, 244), 0.10)' },
          ],
          name: [
            { 'justify-self': 'start' },
            { 'font-weight': '800' },
            { 'font-size': '14px' },
            { 'letter-spacing': '0.5px' },
          ],
        },
      },
      {
        type: 'grid',
        columns: 4,
        square: false,
        cards: [
          healthGauge('Overall', `states['${healthEntity}']?.state`),
          healthGauge('Battery 1', `states['${healthEntity}']?.attributes?.battery_1_health_percent`),
          healthGauge('Battery 2', `states['${healthEntity}']?.attributes?.battery_2_health_percent`),
          healthGauge('Battery 3', `states['${healthEntity}']?.attributes?.battery_3_health_percent`),
        ],
      },
      {
        type: 'markdown',
        content: `{% set original = state_attr('${healthEntity}', 'original_capacity_kwh') %}
{% set current = state_attr('${healthEntity}', 'current_capacity_kwh') %}
{% set scan = state_attr('${healthEntity}', 'last_scan') %}
{%- if states('${healthEntity}') not in ['unavailable', 'unknown'] %}
**Capacity:** {{ current }} / {{ original }} kWh | **Last scan:** {{ scan[:10] if scan else 'N/A' }}
{%- else %}
Scan from the PowerSync Mobile app while connected to Powerwall WiFi.
{%- endif %}`,
      },
    ],
  };
}

function _energyChart(title, entity, color, yaxisOpts) {
  const yaxis = {
    id: 'y',
    ...(yaxisOpts || {}),
    apex_config: {
      forceNiceScale: true,
      tickAmount: 4,
      labels: {
        formatter: "EVAL:function(val) { return val.toFixed(1) + ' kW'; }",
      },
    },
  };
  return {
    type: 'custom:apexcharts-card',
    header: { show: true, title, show_states: true },
    graph_span: '24h',
    span: { start: 'day' },
    yaxis: [yaxis],
    series: [{
      entity,
      name: title,
      type: 'area',
      color,
      stroke_width: 2,
      extend_to: 'now',
      yaxis_id: 'y',
      group_by: { func: 'avg', duration: '5min' },
    }],
    apex_config: {
      chart: { height: 150 },
    },
  };
}

function _demandCharge(e) {
  return {
    type: 'entities',
    title: 'Demand Charge',
    show_header_toggle: false,
    entities: [
      { entity: e('in_demand_charge_period'), name: 'In Demand Period' },
      { entity: e('peak_demand_this_cycle'), name: 'Peak Demand (This Cycle)' },
      { entity: e('demand_charge_cost'), name: 'Demand Charge Cost' },
    ],
  };
}

function _aemoSpike(e) {
  return {
    type: 'entities',
    title: 'AEMO Spike Monitor',
    show_header_toggle: false,
    entities: [
      { entity: e('aemo_price'), name: 'AEMO Price' },
      { entity: e('aemo_spike_status'), name: 'Spike Status' },
    ],
  };
}

function _flowPower(e) {
  return {
    type: 'entities',
    title: 'Flow Power',
    show_header_toggle: false,
    entities: [
      { entity: e('flow_power_price'), name: 'Import Price' },
      { entity: e('flow_power_export_price'), name: 'Export Price' },
      { entity: e('flow_power_twap'), name: 'TWAP 30-Day Average' },
      { entity: e('flow_power_network_tariff'), name: 'Network Tariff' },
    ],
  };
}

// ─── Registration ────────────────────────────────────────────

customElements.define('ll-strategy-dashboard-power-sync-strategy', PowerSyncStrategy);
