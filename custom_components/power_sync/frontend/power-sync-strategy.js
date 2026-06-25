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
// Native responsive chart card for history, forecast, and TOU schedule data.

const OPTIMIZER_POWER_AXIS_EXPONENT = 0.7;

class PowerSyncChart extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = null;
    this._hass = null;
    this._resizeObserver = null;
    this._renderQueued = false;
    this._historyCache = new Map();
    this._historyRequestKey = null;
    this._hiddenSeries = new Set();
    this._lastRenderSignature = '';
  }

  setConfig(config) {
    this._config = config;
    this._historyCache.clear();
    this._historyRequestKey = null;
    this._lastRenderSignature = '';
    const validKeys = new Set((config.series || []).map((series, index) => this._seriesKey(series, index)));
    for (const key of Array.from(this._hiddenSeries)) {
      if (!validKeys.has(key)) this._hiddenSeries.delete(key);
    }
    this._scheduleRender();
  }

  set hass(hass) {
    this._hass = hass;
    this._scheduleRenderIfChanged();
  }

  getCardSize() {
    return 4;
  }

  connectedCallback() {
    if (!this._resizeObserver && 'ResizeObserver' in window) {
      this._resizeObserver = new ResizeObserver(() => this._scheduleRenderIfChanged());
      this._resizeObserver.observe(this);
    }
    this._scheduleRender();
  }

  disconnectedCallback() {
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
      this._resizeObserver = null;
    }
  }

  _scheduleRender() {
    if (this._renderQueued) return;
    this._renderQueued = true;
    requestAnimationFrame(() => {
      this._renderQueued = false;
      this._render();
    });
  }

  _scheduleRenderIfChanged() {
    const signature = this._renderSignature();
    if (signature !== this._lastRenderSignature) {
      this._scheduleRender();
    }
  }

  _renderSignature() {
    if (!this._config || !this._hass) return '';
    const config = this._config;
    const mode = config.mode || 'forecast';
    const width = this.getBoundingClientRect().width || Number(config.width) || 0;
    const compact = width < 520 ? 'compact' : 'wide';
    const intervalMs = Math.max(60000, Number(config.intervalMinutes || 5) * 60000);
    const clockBucket = mode === 'forecast'
      ? Math.floor(Date.now() / intervalMs)
      : Math.floor(Date.now() / 300000);
    const series = (config.series || []).map((seriesConfig, index) => ({
      key: this._seriesKey(seriesConfig, index),
      entity: seriesConfig.entity,
      attribute: seriesConfig.attribute,
      dataKey: seriesConfig.key,
      name: seriesConfig.name,
      color: seriesConfig.color,
      fill: !!seriesConfig.fill,
      strokeWidth: seriesConfig.strokeWidth,
      minValue: seriesConfig.minValue,
      maxValue: seriesConfig.maxValue,
      state: this._chartEntitySignature(mode, seriesConfig, config),
      cache: mode === 'history' ? this._historyCacheSignature(seriesConfig.entity) : undefined,
    }));

    return JSON.stringify({
      mode,
      clockBucket,
      compact,
      title: config.title,
      height: config.height,
      width: config.width,
      yUnit: config.yUnit,
      yUnitCompact: config.yUnitCompact,
      yMultiplier: config.yMultiplier,
      yMin: config.yMin,
      yMax: config.yMax,
      zeroBaseline: config.zeroBaseline,
      hideZeroTickLabel: config.hideZeroTickLabel,
      stepLine: config.stepLine,
      historyHours: config.historyHours,
      historyRange: config.historyRange,
      entity: mode === 'tou' ? this._touEntitySignature(config) : undefined,
      hiddenSeries: Array.from(this._hiddenSeries).sort(),
      series,
    });
  }

  _chartEntitySignature(mode, seriesConfig, config) {
    if (mode === 'forecast') {
      return this._stateSignature(seriesConfig.entity, [seriesConfig.attribute || 'forecast_values_kw']);
    }
    if (mode === 'history') {
      return this._stateSignature(seriesConfig.entity);
    }
    if (mode === 'tou') {
      return this._touEntitySignature(config);
    }
    return '';
  }

  _touEntitySignature(config) {
    const keys = (config.series || [])
      .map(seriesConfig => seriesConfig.key)
      .filter(Boolean)
      .flatMap(key => [`${key}_price`]);
    return this._stateSignature(config.entity, ['schedule', 'tou_schedule', ...keys]);
  }

  _stateSignature(entityId, attributeNames = []) {
    if (!entityId) return '';
    const state = this._hass?.states?.[entityId];
    if (!state) return `${entityId}:missing`;
    const attrs = attributeNames.map(name => state.attributes?.[name]);
    return JSON.stringify([entityId, state.state, state.last_updated, state.last_changed, ...attrs]);
  }

  _historyCacheSignature(entityId) {
    const points = this._historyCache.get(entityId) || [];
    if (!points.length) return `${entityId || ''}:empty`;
    const first = points[0];
    const last = points[points.length - 1];
    return JSON.stringify([entityId, points.length, first?.[0], first?.[1], last?.[0], last?.[1]]);
  }

  _render() {
    if (!this._config || !this._hass) return;
    this._lastRenderSignature = this._renderSignature();

    const config = this._config;
    const hass = this._hass;
    const mode = config.mode || 'forecast';
    if (mode === 'history') {
      this._loadHistoryData(config, hass);
    }

    let allSeries;
    if (mode === 'tou') {
      allSeries = this._getTouData(config, hass);
    } else if (mode === 'history') {
      allSeries = this._getHistoryData(config, hass);
    } else {
      allSeries = this._getForecastData(config, hass);
    }

    allSeries = allSeries.map((series, index) => ({
      ...series,
      _key: this._seriesKey(series, index),
      hidden: this._hiddenSeries.has(this._seriesKey(series, index)),
    }));
    const visibleSeries = allSeries.filter((series) => !series.hidden);
    const chartSeries = visibleSeries;

    const box = this.getBoundingClientRect();
    const W = Math.max(320, Math.round(box.width || config.width || 640));
    const compact = W < 520;
    const H = Math.max(190, Math.round(config.height || (compact ? 220 : 250)));
    const yUnit = String(config.yUnit || '');
    const needsWideYAxis = /\/kWh$/i.test(yUnit) || yUnit.length >= 5;
    const pad = {
      top: 16,
      right: compact ? 12 : 20,
      bottom: compact ? 34 : 42,
      left: needsWideYAxis ? (compact ? 68 : 82) : (compact ? 42 : 56),
    };
    const chartW = W - pad.left - pad.right;
    const chartH = H - pad.top - pad.bottom;

    let xMin = Infinity, xMax = -Infinity;
    const fixedWindow = mode === 'history' ? this._historyWindow(config) : null;
    if (fixedWindow) {
      xMin = fixedWindow.startMs;
      xMax = fixedWindow.endMs;
    }
    for (const s of chartSeries) {
      for (const [t] of s.data) {
        if (!fixedWindow && t < xMin) xMin = t;
        if (!fixedWindow && t > xMax) xMax = t;
      }
    }
    if (!isFinite(xMin) || !isFinite(xMax) || xMin === xMax) {
      xMin = Date.now();
      xMax = xMin + 3600000;
    }

    const configuredYMultiplier = Number(config.yMultiplier ?? 1);
    const yMultiplier = Number.isFinite(configuredYMultiplier) && configuredYMultiplier !== 0
      ? configuredYMultiplier
      : 1;
    let rawMin = Infinity, rawMax = -Infinity;
    for (const s of chartSeries) {
      for (const [, v] of s.data) {
        const scaled = v * yMultiplier;
        if (scaled < rawMin) rawMin = scaled;
        if (scaled > rawMax) rawMax = scaled;
      }
    }
    if (!isFinite(rawMin)) { rawMin = 0; rawMax = 1; }
    if (rawMin === rawMax) { rawMin -= 1; rawMax += 1; }

    const dataRange = rawMax - rawMin;
    rawMin -= dataRange * 0.08;
    rawMax += dataRange * 0.08;
    if (config.zeroBaseline || rawMin < 0) {
      rawMin = Math.min(rawMin, 0);
      rawMax = Math.max(rawMax, 0);
    }

    if (config.yMin !== undefined) rawMin = config.yMin * yMultiplier;
    if (config.yMax !== undefined) rawMax = config.yMax * yMultiplier;

    const niceStep = this._niceStep((rawMax - rawMin) / (compact ? 4 : 5));
    const yMin = Math.floor(rawMin / niceStep) * niceStep;
    const yMax = Math.ceil(rawMax / niceStep) * niceStep;
    const ticks = [];
    for (let v = yMin; v <= yMax + niceStep * 0.01; v += niceStep) {
      ticks.push(Math.round(v * 1000) / 1000);
    }
    if (ticks.length > 20) ticks.length = 20;

    const xScale = (t) => pad.left + ((t - xMin) / (xMax - xMin)) * chartW;
    const yScale = (v) => pad.top + chartH - ((v - yMin) / (yMax - yMin)) * chartH;

    let svg = '';

    for (const tick of ticks) {
      const y = yScale(tick);
      const isZero = Math.abs(tick) < 0.0001;
      svg += `<line x1="${pad.left}" y1="${y}" x2="${W - pad.right}" y2="${y}" stroke="${isZero ? 'var(--primary-text-color, #333)' : 'var(--divider-color, #e0e0e0)'}" stroke-width="${isZero ? 0.9 : 0.45}" stroke-dasharray="${isZero ? '0' : '4,3'}" opacity="${isZero ? 0.35 : 0.65}"/>`;
      const unit = config.yUnit || '';
      const compactUnit = config.yUnitCompact || ['c', 'p', 'ct', 'c/kWh', 'p/kWh', 'ct/kWh'].includes(unit);
      const label = this._formatValue(tick, unit, compactUnit);
      if (!(config.hideZeroTickLabel && isZero)) {
        svg += `<text x="${pad.left - 6}" y="${y + 4}" text-anchor="end" font-size="${compact ? 10 : 11}" fill="var(--secondary-text-color, #888)">${this._escSvg(label)}</text>`;
      }
    }

    const spanHours = (xMax - xMin) / 3600000;
    let xTickInterval;
    if (spanHours <= 6) xTickInterval = compact ? 2 : 1;
    else if (spanHours <= 12) xTickInterval = compact ? 3 : 2;
    else if (spanHours <= 24) xTickInterval = compact ? 6 : 3;
    else if (spanHours <= 36) xTickInterval = compact ? 8 : 6;
    else xTickInterval = compact ? 12 : 8;

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
      svg += `<line x1="${x}" y1="${pad.top}" x2="${x}" y2="${pad.top + chartH}" stroke="var(--divider-color, #e0e0e0)" stroke-width="0.3" opacity="0.55"/>`;
      svg += `<text x="${x}" y="${H - pad.bottom + 19}" text-anchor="middle" font-size="${compact ? 9 : 10}" fill="var(--secondary-text-color, #888)">${this._escSvg(label)}</text>`;
    }

    svg += `<rect x="${pad.left}" y="${pad.top}" width="${chartW}" height="${chartH}" fill="var(--card-background-color, transparent)" opacity="0.02" stroke="var(--divider-color, #e0e0e0)" stroke-width="0.5" rx="8"/>`;

    for (const series of chartSeries) {
      if (series.data.length === 0) continue;
      const step = config.stepLine !== undefined ? config.stepLine : mode === 'history';
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

      if (series.fill) {
        const baseline = yScale(Math.max(0, yMin));
        const first = series.data[0];
        const last = series.data[series.data.length - 1];
        const fillD = pathD + `L${xScale(last[0])},${baseline}L${xScale(first[0])},${baseline}Z`;
        svg += `<path d="${fillD}" fill="${series.color}" opacity="${series.fillOpacity ?? 0.16}"/>`;
      }

      svg += `<path d="${pathD}" fill="none" stroke="${series.color}" stroke-width="${series.strokeWidth || 2.25}" stroke-linejoin="round" stroke-linecap="round"/>`;

      const marker = mode === 'tou'
        ? this._pointAt(series.data, Date.now())
        : series.data[series.data.length - 1];
      if (marker) {
        svg += `<circle cx="${xScale(marker[0])}" cy="${yScale(marker[1] * yMultiplier)}" r="${compact ? 3 : 4}" fill="${series.color}" stroke="var(--ha-card-background, var(--card-background-color, white))" stroke-width="2"/>`;
      }
    }

    if (mode === 'forecast' || mode === 'history' || mode === 'tou') {
      const nowX = xScale(Date.now());
      if (nowX >= pad.left && nowX <= W - pad.right) {
        svg += `<line x1="${nowX}" y1="${pad.top}" x2="${nowX}" y2="${pad.top + chartH}" stroke="var(--primary-color, #03a9f4)" stroke-width="1" stroke-dasharray="4,2" opacity="0.6"/>`;
      }
    }

    const title = config.title || '';
    const legend = allSeries.map((s) => this._legendItem(s, yMultiplier, config)).join('');
    const empty = chartSeries.length === 0 || chartSeries.every(s => s.data.length === 0);
    const accent = (chartSeries.find(s => s.color) || allSeries.find(s => s.color))?.color || 'var(--primary-color, #03a9f4)';

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          min-width: 0;
          --ps-chart-accent: ${accent};
        }
        .card {
          background: var(--ha-card-background, var(--card-background-color, white));
          border-radius: var(--ha-card-border-radius, 12px);
          box-shadow: var(--ha-card-box-shadow, none);
          border: var(--ha-card-border-width, 1px) solid var(--ha-card-border-color, var(--divider-color, rgba(0,0,0,0.12)));
          border-top: 4px solid var(--ps-chart-accent);
          padding: 14px 14px 12px;
          overflow: hidden;
          box-sizing: border-box;
          position: relative;
        }
        .card::before {
          content: '';
          position: absolute;
          inset: 0;
          background: linear-gradient(135deg, var(--ps-chart-accent) 0%, transparent 34%);
          opacity: 0.055;
          pointer-events: none;
        }
        .head {
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 12px;
          margin-bottom: 8px;
          position: relative;
          z-index: 1;
        }
        .title {
          min-width: 0;
          color: var(--primary-text-color, #333);
          font-size: 15px;
          font-weight: 700;
          line-height: 1.25;
        }
        .legend {
          display: flex;
          flex-wrap: wrap;
          justify-content: flex-end;
          gap: 6px 10px;
          min-width: 120px;
          max-width: 58%;
        }
        .legend-item {
          display: inline-flex;
          align-items: center;
          gap: 5px;
          min-width: 0;
          color: var(--secondary-text-color, #888);
          font: inherit;
          font-size: 11.5px;
          line-height: 1.2;
          white-space: nowrap;
          padding: 3px 6px;
          border-radius: 999px;
          border: 0;
          background: color-mix(in srgb, var(--secondary-background-color, transparent) 65%, transparent);
          cursor: pointer;
          transition: opacity 120ms ease, background 120ms ease;
        }
        .legend-item:hover {
          background: color-mix(in srgb, var(--secondary-background-color, transparent) 82%, var(--ps-chart-accent));
        }
        .legend-item.is-hidden {
          opacity: 0.42;
        }
        .swatch {
          width: 14px;
          height: 3px;
          border-radius: 999px;
          flex: 0 0 auto;
        }
        .value {
          color: var(--primary-text-color, #333);
          font-weight: 600;
        }
        svg {
          width: 100%;
          height: ${H}px;
          display: block;
          position: relative;
          z-index: 1;
        }
        .chart-wrap {
          position: relative;
          z-index: 1;
          isolation: isolate;
          touch-action: none;
        }
        .tooltip-line {
          position: absolute;
          top: 0;
          bottom: 0;
          width: 1px;
          background: var(--primary-color, #03a9f4);
          opacity: 0;
          pointer-events: none;
          transform: translateX(-0.5px);
          z-index: 2;
        }
        .tooltip {
          position: absolute;
          min-width: 140px;
          max-width: min(240px, calc(100% - 16px));
          padding: 8px 10px;
          border-radius: 8px;
          background: rgba(var(--rgb-card-background-color, 255, 255, 255), 0.22);
          color: var(--primary-text-color, #333);
          box-shadow: 0 8px 22px rgba(0, 0, 0, 0.28);
          border: 1px solid var(--divider-color, rgba(255,255,255,0.18));
          font-size: 12px;
          line-height: 1.35;
          opacity: 0;
          pointer-events: none;
          transform: translate(-50%, -100%);
          z-index: 4;
        }
        .tooltip-time {
          font-weight: 700;
          margin-bottom: 5px;
        }
        .tooltip-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          white-space: nowrap;
        }
        .tooltip-name {
          display: inline-flex;
          align-items: center;
          min-width: 0;
          gap: 6px;
          color: var(--secondary-text-color, #888);
        }
        .tooltip-dot {
          width: 7px;
          height: 7px;
          border-radius: 999px;
          flex: 0 0 auto;
        }
        .tooltip-value {
          font-weight: 700;
        }
        .no-data {
          text-align: center;
          color: var(--secondary-text-color, #888);
          padding: 36px 0 40px;
          font-size: 14px;
          position: relative;
          z-index: 1;
        }
        @media (max-width: 520px) {
          .head {
            display: block;
          }
          .legend {
            max-width: none;
            justify-content: flex-start;
            margin-top: 8px;
          }
        }
      </style>
      <div class="card">
        <div class="head">
          <div class="title">${this._escHtml(title)}</div>
          <div class="legend">${legend}</div>
        </div>
        ${empty
          ? `<div class="no-data">${this._escHtml(title)}<br>No data available</div>`
          : `<div class="chart-wrap">
              <svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">${svg}</svg>
              <div class="tooltip-line"></div>
              <div class="tooltip"></div>
            </div>`
        }
      </div>
    `;

    this._attachLegendToggles(allSeries);
    if (!empty) {
      this._attachTooltip({
        allSeries: chartSeries,
        chartW,
        config,
        pad,
        spanHours,
        W,
        xMax,
        xMin,
        yMultiplier,
      });
    }
  }

  _attachTooltip({ allSeries, chartW, config, pad, spanHours, W, xMax, xMin, yMultiplier }) {
    const wrap = this.shadowRoot.querySelector('.chart-wrap');
    const svg = this.shadowRoot.querySelector('svg');
    const line = this.shadowRoot.querySelector('.tooltip-line');
    const tooltip = this.shadowRoot.querySelector('.tooltip');
    if (!wrap || !svg || !line || !tooltip) return;

    const hide = () => {
      line.style.opacity = '0';
      tooltip.style.opacity = '0';
    };

    const nearestPoint = (data, targetT) => {
      let best = null;
      let bestDistance = Infinity;
      for (const point of data) {
        const distance = Math.abs(point[0] - targetT);
        if (distance < bestDistance) {
          best = point;
          bestDistance = distance;
        }
      }
      return best;
    };

    const move = (event) => {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      const localX = ((event.clientX - rect.left) / rect.width) * W;
      if (localX < pad.left || localX > W - pad.right) {
        hide();
        return;
      }

      const targetT = xMin + ((localX - pad.left) / chartW) * (xMax - xMin);
      const points = allSeries
        .map((series) => ({ series, point: nearestPoint(series.data, targetT) }))
        .filter((item) => item.point && Number.isFinite(item.point[1]));
      if (points.length === 0) {
        hide();
        return;
      }

      const anchorT = points.reduce((best, item) => (
        Math.abs(item.point[0] - targetT) < Math.abs(best - targetT) ? item.point[0] : best
      ), points[0].point[0]);
      const anchorX = pad.left + ((anchorT - xMin) / (xMax - xMin)) * chartW;
      const cssX = (anchorX / W) * rect.width;
      const rows = points.map(({ series, point }) => `
        <div class="tooltip-row">
          <span class="tooltip-name">
            <span class="tooltip-dot" style="background:${series.color}"></span>
            <span>${this._escHtml(series.name || '')}</span>
          </span>
          <span class="tooltip-value">${this._escHtml(this._formatValue(point[1] * yMultiplier, config.yUnit, config.yUnitCompact))}</span>
        </div>
      `).join('');

      tooltip.innerHTML = `
        <div class="tooltip-time">${this._escHtml(this._formatTooltipTime(anchorT, spanHours))}</div>
        ${rows}
      `;

      line.style.left = `${cssX}px`;
      line.style.opacity = '0.75';
      tooltip.style.left = `${Math.min(Math.max(cssX, 78), rect.width - 78)}px`;
      tooltip.style.top = `${Math.max(34, rect.height - pad.bottom - 8)}px`;
      tooltip.style.opacity = '1';
    };

    svg.addEventListener('pointermove', move);
    svg.addEventListener('pointerleave', hide);
    svg.addEventListener('pointercancel', hide);
  }

  _attachLegendToggles(allSeries) {
    const buttons = this.shadowRoot.querySelectorAll('.legend-item[data-series-key]');
    buttons.forEach((button) => {
      button.addEventListener('click', () => {
        const key = button.dataset.seriesKey;
        if (!key) return;
        if (this._hiddenSeries.has(key)) {
          this._hiddenSeries.delete(key);
        } else {
          this._hiddenSeries.add(key);
        }
        this._scheduleRender();
      });
    });
  }

  _formatTooltipTime(timestamp, spanHours) {
    const d = new Date(timestamp);
    const options = spanHours > 24
      ? { weekday: 'short', hour: '2-digit', minute: '2-digit' }
      : { hour: '2-digit', minute: '2-digit' };
    return d.toLocaleString([], options);
  }

  _legendItem(series, yMultiplier, config) {
    const rawValue = config.mode === 'tou'
      ? this._currentValue(series.data)
      : this._lastValue(series.data);
    const value = rawValue === null ? '' : this._formatValue(rawValue * yMultiplier, config.yUnit, config.yUnitCompact);
    const pressed = series.hidden ? 'false' : 'true';
    return `
      <button class="legend-item${series.hidden ? ' is-hidden' : ''}" type="button" data-series-key="${this._escAttr(series._key)}" aria-pressed="${pressed}">
        <span class="swatch" style="background:${series.color}"></span>
        <span>${this._escHtml(series.name || '')}</span>
        ${value ? `<span class="value">${this._escHtml(value)}</span>` : ''}
      </button>
    `;
  }

  _seriesKey(series, index) {
    return String(series.entity || series.key || series.name || `series_${index}`);
  }

  _lastValue(data) {
    for (let i = data.length - 1; i >= 0; i--) {
      const value = Number(data[i][1]);
      if (Number.isFinite(value)) return value;
    }
    return null;
  }

  _pointAt(data, timestamp) {
    if (!Array.isArray(data) || data.length === 0 || !Number.isFinite(timestamp)) return null;
    let current = null;
    for (const point of data) {
      if (!Array.isArray(point) || point.length < 2) continue;
      const t = Number(point[0]);
      const v = Number(point[1]);
      if (!Number.isFinite(t) || !Number.isFinite(v)) continue;
      if (t > timestamp) break;
      current = [t, v];
    }
    if (current) return [timestamp, current[1]];
    const first = data.find(point => Number.isFinite(Number(point?.[0])) && Number.isFinite(Number(point?.[1])));
    return first ? [Number(first[0]), Number(first[1])] : null;
  }

  _currentValue(data) {
    const point = this._pointAt(data, Date.now());
    return point ? Number(point[1]) : null;
  }

  _formatValue(value, unit, compactUnit) {
    if (!Number.isFinite(value)) return '';
    const decimals = Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 10 ? 1 : 2;
    const suffix = unit ? `${compactUnit ? '' : ' '}${unit}` : '';
    return `${value.toFixed(decimals)}${suffix}`;
  }

  _niceStep(rawStep) {
    const safeStep = rawStep > 0 ? rawStep : 1;
    const mag = Math.pow(10, Math.floor(Math.log10(safeStep)));
    const residual = safeStep / mag;
    if (residual <= 1.5) return 1 * mag;
    if (residual <= 3) return 2 * mag;
    if (residual <= 7) return 5 * mag;
    return 10 * mag;
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

  _getHistoryData(config, hass) {
    const window = this._historyWindow(config);
    const now = window.nowMs;
    const start = window.startMs;
    const end = window.endMs;
    return (config.series || []).map(s => {
      const stateObj = s.entity ? hass.states[s.entity] : null;
      const cached = this._historyCache.get(s.entity) || [];
      const rawData = cached.length
        ? cached
        : this._statePoint(stateObj, now);
      const data = this._projectHistoryToNow(rawData, stateObj, now, start, end);
      const filtered = this._filterSeriesData(data, s);
      return { name: s.name, color: s.color, fill: !!s.fill, strokeWidth: s.strokeWidth, data: filtered.filter(([t]) => t >= start && t <= end) };
    });
  }

  _filterSeriesData(data, seriesConfig) {
    const minValue = Number(seriesConfig?.minValue);
    const maxValue = Number(seriesConfig?.maxValue);
    const hasMin = Number.isFinite(minValue);
    const hasMax = Number.isFinite(maxValue);
    if (!hasMin && !hasMax) return data;
    return (Array.isArray(data) ? data : []).filter(([, value]) => (
      (!hasMin || value >= minValue) && (!hasMax || value <= maxValue)
    ));
  }

  _projectHistoryToNow(data, stateObj, now, start, end) {
    const points = (Array.isArray(data) ? data : [])
      .map(([t, v]) => [Number(t), Number(v)])
      .filter(([t, v]) => Number.isFinite(t) && Number.isFinite(v))
      .sort((a, b) => a[0] - b[0]);
    const value = Number(stateObj?.state);
    if (!Number.isFinite(value) || now < start || now > end) return points;

    if (points.length === 0) {
      return [[Math.max(start, now - 3600000), value], [now, value]];
    }

    const projected = points.filter(([t]) => t <= now);
    if (projected.length === 0) {
      return [[start, value], [now, value]];
    }

    const last = projected[projected.length - 1];
    if (last[0] < now) {
      projected.push([now, value]);
    } else {
      projected[projected.length - 1] = [last[0], value];
    }
    return projected;
  }

  _statePoint(stateObj, now) {
    const value = Number(stateObj?.state);
    if (!Number.isFinite(value)) return [];
    const changed = Date.parse(stateObj?.last_updated || stateObj?.last_changed || '');
    const t = Number.isFinite(changed) ? changed : now;
    return [[Math.max(t, now - 3600000), value], [now, value]];
  }

  async _loadHistoryData(config, hass) {
    if (!hass || typeof hass.callApi !== 'function') return;
    const entities = (config.series || []).map(s => s.entity).filter(Boolean);
    if (!entities.length) return;
    const window = this._historyWindow(config);
    const start = new Date(window.startMs);
    const end = new Date(window.queryEndMs);
    const key = `${entities.join(',')}|${window.startMs}|${window.endMs}|${Math.floor(window.queryEndMs / 300000)}`;
    if (this._historyRequestKey === key) return;
    this._historyRequestKey = key;

    try {
      const query = new URLSearchParams({
        filter_entity_id: entities.join(','),
        end_time: end.toISOString(),
        no_attributes: '1',
        significant_changes_only: '0',
      });
      const response = await hass.callApi('GET', `history/period/${start.toISOString()}?${query.toString()}`);
      const next = new Map();
      if (Array.isArray(response)) {
        for (const series of response) {
          if (!Array.isArray(series) || series.length === 0) continue;
          const entityId = series[0]?.entity_id;
          if (!entityId) continue;
          const points = series
            .map((p) => [Date.parse(p.last_updated || p.last_changed), Number(p.state)])
            .filter(([t, v]) => Number.isFinite(t) && Number.isFinite(v));
          next.set(entityId, points);
        }
      }
      for (const entityId of entities) {
        this._historyCache.set(entityId, next.get(entityId) || []);
      }
      this._scheduleRender();
    } catch (err) {
      // History is an enhancement; the chart still renders current state fallback.
    }
  }

  _historyWindow(config) {
    const now = new Date();
    const nowMs = now.getTime();
    if (config.historyRange === 'today') {
      const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      const end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 1);
      return {
        startMs: start.getTime(),
        endMs: end.getTime(),
        queryEndMs: Math.min(nowMs, end.getTime()),
        nowMs,
      };
    }
    const spanHours = config.historyHours || 24;
    const startMs = nowMs - spanHours * 3600000;
    return {
      startMs,
      endMs: nowMs,
      queryEndMs: nowMs,
      nowMs,
    };
  }

  _escSvg(str) {
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  _escHtml(str) {
    return this._escSvg(str);
  }

  _escAttr(str) {
    return this._escSvg(str);
  }
}

if (!customElements.get('power-sync-chart')) {
  customElements.define('power-sync-chart', PowerSyncChart);
}

// ─── PowerSyncForecastSummary Custom Element ─────────────────────
// Compact metric summary for LP forecast entities. Built-in HA entity cards
// truncate long forecast names and price units too aggressively in a stack.

class PowerSyncForecastSummary extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = null;
    this._hass = null;
  }

  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 1;
  }

  _render() {
    if (!this._config || !this._hass) return;

    const items = Array.isArray(this._config.items) ? this._config.items : [];
    this.shadowRoot.innerHTML = `
      <style>
        ha-card {
          padding: 14px;
          overflow: hidden;
        }
        .grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(158px, 1fr));
          gap: 12px;
        }
        .metric {
          min-width: 0;
          padding: 12px 14px;
          border-radius: 10px;
          background: rgba(127, 127, 127, 0.08);
          border: 1px solid var(--divider-color);
        }
        .header {
          display: grid;
          grid-template-columns: 22px minmax(0, 1fr);
          gap: 8px;
          align-items: center;
          min-width: 0;
          color: var(--secondary-text-color);
        }
        ha-icon {
          width: 20px;
          height: 20px;
          color: var(--primary-color);
        }
        .name {
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          font-size: 13px;
          font-weight: 700;
          line-height: 1.2;
          letter-spacing: 0;
        }
        .reading {
          display: flex;
          flex-wrap: wrap;
          align-items: baseline;
          gap: 7px;
          min-width: 0;
          margin-top: 10px;
          color: var(--primary-text-color);
        }
        .value {
          flex: 0 1 auto;
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          font-size: 30px;
          font-weight: 500;
          line-height: 1;
          letter-spacing: 0;
        }
        .unit {
          flex: 0 0 auto;
          color: var(--secondary-text-color);
          font-size: 13px;
          font-weight: 700;
          line-height: 1.1;
          white-space: nowrap;
          letter-spacing: 0;
        }
        @media (max-width: 640px) {
          ha-card {
            padding: 12px;
          }
          .grid {
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 10px;
          }
          .metric {
            padding: 11px 12px;
          }
          .value {
            font-size: 26px;
          }
          .unit {
            font-size: 12px;
          }
        }
      </style>
      <ha-card>
        <div class="grid">
          ${items.map((item) => this._renderMetric(item)).join('')}
        </div>
      </ha-card>
    `;
  }

  _renderMetric(item) {
    const stateObj = this._hass.states[item.entity];
    const reading = this._formatReading(item, stateObj);
    return `
      <div class="metric">
        <div class="header">
          <ha-icon icon="${this._escAttr(item.icon || 'mdi:chart-line')}"></ha-icon>
          <div class="name">${this._escHtml(item.name || stateObj?.attributes?.friendly_name || item.entity)}</div>
        </div>
        <div class="reading">
          <span class="value">${this._escHtml(reading.value)}</span>
          <span class="unit">${this._escHtml(reading.unit)}</span>
        </div>
      </div>
    `;
  }

  _formatReading(item, stateObj) {
    if (!stateObj || ['unknown', 'unavailable'].includes(stateObj.state)) {
      return { value: '--', unit: item.unit || '' };
    }

    const raw = Number(stateObj.state);
    if (!Number.isFinite(raw)) {
      return { value: stateObj.state, unit: stateObj.attributes?.unit_of_measurement || item.unit || '' };
    }

    if (item.price) {
      const meta = _priceMeta(this._hass, item.entity);
      const value = raw * 100;
      const decimals = Math.abs(value) >= 10 ? 1 : 2;
      return { value: value.toFixed(decimals), unit: meta.minorPriceUnit };
    }

    const decimals = Number.isInteger(item.decimals)
      ? item.decimals
      : (Math.abs(raw) >= 100 ? 0 : 1);
    return {
      value: raw.toFixed(decimals),
      unit: stateObj.attributes?.unit_of_measurement || item.unit || '',
    };
  }

  _escHtml(value) {
    return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  _escAttr(value) {
    return this._escHtml(value);
  }
}

if (!customElements.get('power-sync-forecast-summary')) {
  customElements.define('power-sync-forecast-summary', PowerSyncForecastSummary);
}

// ─── PowerSyncBatteryHealth Custom Element ──────────────────────
// A compact, data-dense health card for aggregate and per-pack capacity data.

class PowerSyncBatteryHealth extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = null;
    this._hass = null;
  }

  setConfig(config) {
    this._config = config || {};
    this._render();
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

    const entity = this._config.entity;
    const stateObj = this._hass.states?.[entity];
    const attrs = stateObj?.attributes || {};
    const health = this._number(stateObj?.state);
    const original = this._number(attrs.original_capacity_kwh);
    const current = this._number(attrs.current_capacity_kwh);
    const soh = this._number(attrs.state_of_health_percent);
    const source = attrs.source;
    const sourceLabel = this._sourceLabel(source);
    const scanLabel = this._dateLabel(attrs.last_scan);
    const packs = this._packRows(attrs);
    const hasCapacity = Number.isFinite(current) && Number.isFinite(original);
    const calculatedHealth = hasCapacity && original > 0 ? (current / original) * 100 : NaN;
    const displayHealth = Number.isFinite(health) ? health : (Number.isFinite(soh) ? soh : calculatedHealth);
    const hasFollower = packs.some(pack => pack.role === 'follower' || pack.isFollower);
    const available = Number.isFinite(displayHealth) || hasCapacity || packs.length > 0;

    this.shadowRoot.innerHTML = `
      <style>
        ha-card {
          overflow: hidden;
          padding: 0;
        }
        .shell {
          display: grid;
          gap: 16px;
          padding: 18px;
          min-width: 0;
        }
        .header {
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          gap: 12px;
          align-items: center;
          min-width: 0;
        }
        .title {
          min-width: 0;
          color: var(--primary-text-color);
          font-size: 20px;
          font-weight: 800;
          line-height: 1.15;
          letter-spacing: 0;
        }
        .subtitle {
          min-width: 0;
          margin-top: 3px;
          color: var(--secondary-text-color);
          font-size: 12px;
          font-weight: 700;
          line-height: 1.25;
          letter-spacing: 0;
          text-transform: uppercase;
        }
        .header ha-icon {
          width: 28px;
          height: 28px;
          color: var(--primary-color);
        }
        .summary {
          display: grid;
          grid-template-columns: minmax(118px, 0.72fr) minmax(0, 1fr);
          gap: 16px;
          align-items: stretch;
          min-width: 0;
        }
        .score {
          display: grid;
          align-content: center;
          min-width: 0;
          padding: 16px;
          border-radius: 8px;
          background: linear-gradient(135deg, color-mix(in srgb, var(--primary-color, #03a9f4) 20%, transparent), rgba(127, 127, 127, 0.08));
          border: 1px solid color-mix(in srgb, var(--primary-color, #03a9f4) 28%, var(--divider-color));
        }
        .score-value {
          min-width: 0;
          overflow-wrap: anywhere;
          color: var(--primary-text-color);
          font-size: 40px;
          font-weight: 850;
          line-height: 0.95;
          letter-spacing: 0;
        }
        .score-value small {
          font-size: 20px;
          font-weight: 800;
        }
        .score-label {
          margin-top: 8px;
          color: var(--secondary-text-color);
          font-size: 12px;
          font-weight: 700;
          line-height: 1.2;
          letter-spacing: 0;
        }
        .details {
          display: grid;
          align-content: center;
          gap: 12px;
          min-width: 0;
        }
        .capacity-line {
          display: flex;
          flex-wrap: wrap;
          align-items: baseline;
          gap: 6px;
          min-width: 0;
          color: var(--primary-text-color);
          font-size: 15px;
          font-weight: 800;
          line-height: 1.25;
          letter-spacing: 0;
        }
        .capacity-line span {
          color: var(--secondary-text-color);
          font-size: 13px;
          font-weight: 700;
        }
        .bar {
          position: relative;
          overflow: hidden;
          height: 12px;
          border-radius: 999px;
          background: rgba(127, 127, 127, 0.16);
        }
        .bar::after {
          content: "";
          position: absolute;
          inset: 0 auto 0 86.96%;
          width: 2px;
          background: color-mix(in srgb, var(--primary-text-color) 42%, transparent);
        }
        .fill {
          position: absolute;
          inset: 0 auto 0 0;
          width: var(--fill);
          max-width: 100%;
          border-radius: inherit;
          background: linear-gradient(90deg, #43a047, #f6bf26);
        }
        .meta {
          display: flex;
          flex-wrap: wrap;
          gap: 7px;
          min-width: 0;
        }
        .pill {
          min-width: 0;
          max-width: 100%;
          padding: 5px 8px;
          border-radius: 999px;
          background: rgba(127, 127, 127, 0.10);
          color: var(--secondary-text-color);
          font-size: 12px;
          font-weight: 700;
          line-height: 1.15;
          letter-spacing: 0;
        }
        .packs {
          display: grid;
          gap: 8px;
          min-width: 0;
        }
        .pack {
          display: grid;
          grid-template-columns: minmax(124px, 1fr) minmax(110px, 0.85fr) auto;
          gap: 10px;
          align-items: center;
          min-width: 0;
          padding: 10px 0;
          border-top: 1px solid var(--divider-color);
        }
        .pack-name {
          min-width: 0;
          color: var(--primary-text-color);
          font-size: 13px;
          font-weight: 800;
          line-height: 1.25;
          letter-spacing: 0;
          overflow-wrap: anywhere;
        }
        .pack-meta {
          margin-top: 2px;
          color: var(--secondary-text-color);
          font-size: 11px;
          font-weight: 700;
          line-height: 1.25;
          letter-spacing: 0;
        }
        .pack-bar {
          min-width: 0;
        }
        .pack-value {
          color: var(--primary-text-color);
          font-size: 18px;
          font-weight: 850;
          line-height: 1;
          letter-spacing: 0;
          white-space: nowrap;
        }
        .note {
          color: var(--secondary-text-color);
          font-size: 12px;
          font-style: italic;
          line-height: 1.35;
          letter-spacing: 0;
        }
        .empty {
          padding: 6px 0 2px;
          color: var(--secondary-text-color);
          font-size: 13px;
          font-weight: 700;
          line-height: 1.35;
          letter-spacing: 0;
        }
        @media (max-width: 640px) {
          .shell {
            padding: 14px;
            gap: 14px;
          }
          .summary {
            grid-template-columns: 1fr;
          }
          .score-value {
            font-size: 34px;
          }
          .pack {
            grid-template-columns: minmax(0, 1fr) auto;
          }
          .pack-bar {
            grid-column: 1 / -1;
            grid-row: 2;
          }
        }
      </style>
      <ha-card>
        <div class="shell">
          <div class="header">
            <div>
              <div class="title">Battery Health</div>
              <div class="subtitle">${this._escHtml(this._subtitle(source, packs.length))}</div>
            </div>
            <ha-icon icon="mdi:battery-heart-variant"></ha-icon>
          </div>
          ${available ? `
            <div class="summary">
              <div class="score">
                <div class="score-value">${this._formatPercent(displayHealth, true)}</div>
                <div class="score-label">${source === 'inverter_modbus' ? 'State of health' : 'Measured vs rated capacity'}</div>
              </div>
              <div class="details">
                ${hasCapacity ? `
                  <div class="capacity-line">${this._formatKwh(current)} <span>available of ${this._formatKwh(original)} rated</span></div>
                  ${this._renderBar(displayHealth)}
                ` : this._renderBar(displayHealth)}
                <div class="meta">
                  ${sourceLabel ? `<div class="pill">Source: ${this._escHtml(sourceLabel)}</div>` : ''}
                  ${scanLabel ? `<div class="pill">Last scan: ${this._escHtml(scanLabel)}</div>` : ''}
                  ${packs.length ? `<div class="pill">${packs.length} ${packs.length === 1 ? 'pack' : 'packs'}</div>` : ''}
                </div>
              </div>
            </div>
            ${packs.length ? `<div class="packs">${packs.map(pack => this._renderPack(pack)).join('')}</div>` : ''}
            ${hasFollower ? '<div class="note">Follower capacity is inferred from aggregate gateway data.</div>' : ''}
          ` : '<div class="empty">No battery health data available yet.</div>'}
        </div>
      </ha-card>
    `;
  }

  _packRows(attrs) {
    const count = Math.min(Number(attrs.battery_count || 0) || 8, 8);
    const rows = [];
    for (let index = 1; index <= count; index++) {
      const health = this._number(attrs[`battery_${index}_health_percent`]);
      if (!Number.isFinite(health)) continue;
      const role = String(attrs[`battery_${index}_role`] || '').toLowerCase();
      const isFollower = attrs[`battery_${index}_is_follower`] === true;
      const isExpansion = attrs[`battery_${index}_is_expansion`] === true;
      rows.push({
        index,
        health,
        label: attrs[`battery_${index}_label`] || this._fallbackPackLabel(index, role, isFollower, isExpansion),
        role,
        isFollower,
        capacityKwh: this._number(attrs[`battery_${index}_original_kwh`]),
      });
    }
    return rows;
  }

  _fallbackPackLabel(index, role, isFollower, isExpansion) {
    if (role === 'leader') return 'Leader Powerwall';
    if (role === 'follower' || isFollower) return 'Follower Powerwall';
    if (role === 'expansion' || isExpansion) return `Expansion Pack ${index}`;
    return `Powerwall ${index}`;
  }

  _renderPack(pack) {
    const capacity = Number.isFinite(pack.capacityKwh)
      ? `${this._formatKwh(pack.capacityKwh)} measured`
      : this._roleLabel(pack);
    return `
      <div class="pack">
        <div>
          <div class="pack-name">${this._escHtml(pack.label)}</div>
          <div class="pack-meta">${this._escHtml(capacity)}</div>
        </div>
        <div class="pack-bar">${this._renderBar(pack.health)}</div>
        <div class="pack-value">${this._formatPercent(pack.health, false)}</div>
      </div>
    `;
  }

  _renderBar(value) {
    if (!Number.isFinite(value)) return '';
    const fill = Math.max(0, Math.min(100, (value / 115) * 100));
    return `<div class="bar" aria-hidden="true"><div class="fill" style="--fill:${fill.toFixed(2)}%"></div></div>`;
  }

  _roleLabel(pack) {
    if (pack.role === 'leader') return 'Leader';
    if (pack.role === 'follower' || pack.isFollower) return 'Follower';
    if (pack.role === 'expansion') return 'Expansion';
    return `Pack ${pack.index}`;
  }

  _subtitle(source, packCount) {
    if (source === 'inverter_modbus') return 'Inverter reported state of health';
    if (packCount > 0) return 'Gateway capacity scan';
    return 'Capacity summary';
  }

  _sourceLabel(source) {
    const labels = {
      ha_local_tedapi: 'local gateway',
      ha_fleet_api_relay: 'Fleet API relay',
      mobile_app_tedapi: 'mobile local scan',
      mobile_app: 'mobile app',
      mobile_app_cloud_rsa: 'mobile cloud RSA',
      fleet_api: 'Fleet API',
      inverter_modbus: 'inverter Modbus',
    };
    return labels[source] || source || '';
  }

  _dateLabel(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value).slice(0, 10);
    return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }

  _formatKwh(value) {
    if (!Number.isFinite(value)) return '-- kWh';
    return `${value.toFixed(1)} kWh`;
  }

  _formatPercent(value, includeSmallUnit) {
    if (!Number.isFinite(value)) return '--';
    const unit = includeSmallUnit ? '<small>%</small>' : '%';
    return `${value.toFixed(1)}${unit}`;
  }

  _number(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : NaN;
  }

  _escHtml(value) {
    return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
}

if (!customElements.get('power-sync-battery-health')) {
  customElements.define('power-sync-battery-health', PowerSyncBatteryHealth);
}

// ─── PowerSyncOptimizationPlan Custom Element ───────────────────
// API-backed Smart Optimization card that mirrors the mobile 24-hour view.

const OPTIMIZATION_PLAN_FETCH_INTERVAL_MS = 45000;
const OPTIMIZATION_PLAN_PENDING_RETRY_MS = 5000;
const OPTIMIZATION_PLAN_CACHE = window.__powerSyncOptimizationPlanCache || new Map();
window.__powerSyncOptimizationPlanCache = OPTIMIZATION_PLAN_CACHE;

class PowerSyncOptimizationPlan extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = null;
    this._hass = null;
    this._data = null;
    this._error = null;
    this._loading = false;
    this._lastFetch = 0;
    this._renderQueued = false;
    this._resizeObserver = null;
    this._lastRenderSignature = '';
  }

  setConfig(config) {
    const previousPath = this._optimizationPath();
    this._config = config || {};
    const nextPath = this._optimizationPath();
    if (previousPath !== nextPath) {
      this._data = null;
      this._error = null;
      this._lastFetch = 0;
    }
    this._lastRenderSignature = '';
    this._restoreCachedData(nextPath);
    this._scheduleRender();
    this._maybeLoadData(true);
  }

  set hass(hass) {
    this._hass = hass;
    this._maybeLoadData(false);
    this._scheduleRenderIfChanged();
  }

  connectedCallback() {
    if (!this._resizeObserver && 'ResizeObserver' in window) {
      this._resizeObserver = new ResizeObserver(() => this._scheduleRenderIfChanged());
      this._resizeObserver.observe(this);
    }
    this._maybeLoadData(true);
    this._scheduleRender();
  }

  disconnectedCallback() {
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
      this._resizeObserver = null;
    }
  }

  getCardSize() {
    return 6;
  }

  _optimizationPath() {
    return this._config?.optimizationPath || 'power_sync/optimization';
  }

  _hasDetailedSchedule(data = this._data) {
    const schedule = data?.schedule || {};
    return Array.isArray(schedule.timestamps) && schedule.timestamps.length > 0;
  }

  _restoreCachedData(path = this._optimizationPath(), force = false) {
    if (force) return false;
    const cached = OPTIMIZATION_PLAN_CACHE.get(path);
    if (!cached?.data) return false;
    const fetchedAt = cached.fetchedAt || 0;
    const maxAge = this._hasDetailedSchedule(cached.data)
      ? OPTIMIZATION_PLAN_FETCH_INTERVAL_MS
      : OPTIMIZATION_PLAN_PENDING_RETRY_MS;
    if (Date.now() - fetchedAt >= maxAge) return false;
    this._data = cached.data;
    this._error = null;
    this._lastFetch = fetchedAt;
    return true;
  }

  _maybeLoadData(force) {
    if (!this._config || !this._hass || typeof this._hass.callApi !== 'function') return;
    const now = Date.now();
    if (this._loading) return;
    if (this._data && !force) {
      const maxAge = this._hasDetailedSchedule()
        ? OPTIMIZATION_PLAN_FETCH_INTERVAL_MS
        : OPTIMIZATION_PLAN_PENDING_RETRY_MS;
      if (now - this._lastFetch < maxAge) return;
    }

    const path = this._optimizationPath();
    if (this._restoreCachedData(path, force)) {
      this._scheduleRender();
      return;
    }

    const cached = OPTIMIZATION_PLAN_CACHE.get(path);
    if (cached?.promise) {
      this._adoptLoadPromise(path, cached.promise);
      return;
    }

    this._loadData(path);
  }

  _adoptLoadPromise(path, promise) {
    this._loading = true;
    promise
      .then((response) => {
        if (path !== this._optimizationPath()) return;
        const cached = OPTIMIZATION_PLAN_CACHE.get(path);
        this._data = response || null;
        this._error = null;
        this._lastFetch = cached?.fetchedAt || Date.now();
      })
      .catch((err) => {
        if (path !== this._optimizationPath()) return;
        this._error = err?.message || 'Optimization API unavailable';
      })
      .finally(() => {
        this._loading = false;
        this._scheduleRender();
      });
  }

  async _loadData(path = this._optimizationPath()) {
    this._loading = true;
    const request = this._hass.callApi('GET', path);
    const previous = OPTIMIZATION_PLAN_CACHE.get(path);
    OPTIMIZATION_PLAN_CACHE.set(path, { ...(previous || {}), promise: request });
    try {
      const response = await request;
      this._data = response || null;
      this._error = null;
      this._lastFetch = Date.now();
      OPTIMIZATION_PLAN_CACHE.set(path, {
        data: this._data,
        fetchedAt: this._lastFetch,
      });
    } catch (err) {
      this._error = err?.message || 'Optimization API unavailable';
      const cached = OPTIMIZATION_PLAN_CACHE.get(path);
      if (cached?.promise === request) {
        if (previous?.data) {
          OPTIMIZATION_PLAN_CACHE.set(path, previous);
        } else {
          OPTIMIZATION_PLAN_CACHE.delete(path);
        }
      }
    } finally {
      this._loading = false;
      this._scheduleRender();
    }
  }

  _scheduleRender() {
    if (this._renderQueued) return;
    this._renderQueued = true;
    requestAnimationFrame(() => {
      this._renderQueued = false;
      this._render();
    });
  }

  _scheduleRenderIfChanged() {
    const signature = this._renderSignature();
    if (signature !== this._lastRenderSignature) {
      this._scheduleRender();
    }
  }

  _entityStateSignature(entityId, attributeNames = []) {
    const state = this._hass?.states?.[entityId];
    if (!state) return `${entityId || ''}:missing`;
    const attrs = attributeNames.map(name => state.attributes?.[name]);
    return JSON.stringify([entityId, state.state, ...attrs]);
  }

  _renderSignature() {
    const width = this.getBoundingClientRect().width || 0;
    const compact = width < 560 ? 'compact' : 'wide';
    const priceMeta = _priceMeta(this._hass, this._config?.importPriceEntity);
    return JSON.stringify({
      path: this._optimizationPath(),
      fetched: this._lastFetch,
      loading: this._loading,
      error: this._error,
      compact,
      priceMeta,
      forceCharge: this._entityStateSignature(this._config?.forceChargeEntity, ['windows']),
      forceDischarge: this._entityStateSignature(this._config?.forceDischargeEntity, ['windows']),
    });
  }

  _render() {
    if (!this._config) return;
    this._lastRenderSignature = this._renderSignature();

    const model = this._buildModel();
    const hasSchedule = model.points.length > 0;
    const priceMeta = _priceMeta(this._hass, this._config.importPriceEntity);
    const box = this.getBoundingClientRect();
    const compact = (box.width || 640) < 560;
    const actions = hasSchedule ? this._actionRangesFromApi() : this._fallbackActionRanges();
    const batteryWindows = this._batteryWindowsFromActions(actions, model);

    this.shadowRoot.innerHTML = `
      <style>
        ha-card {
          padding: 16px;
          overflow: hidden;
        }
        .header {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          gap: 12px;
          margin-bottom: 12px;
        }
        .title {
          margin: 0;
          color: var(--primary-text-color);
          font-size: 18px;
          font-weight: 800;
          line-height: 1.15;
          letter-spacing: 0;
        }
        .subtitle {
          margin-top: 4px;
          color: var(--secondary-text-color);
          font-size: 12px;
          font-weight: 600;
          line-height: 1.3;
        }
        .refresh {
          flex: 0 0 auto;
          width: 34px;
          height: 34px;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--ha-card-background, var(--card-background-color, #fff));
          color: var(--primary-text-color);
          cursor: pointer;
          display: grid;
          place-items: center;
        }
        .refresh ha-icon {
          width: 19px;
          height: 19px;
        }
        .chips {
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
          margin-bottom: 14px;
        }
        .chip {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          max-width: 100%;
          padding: 7px 9px;
          border-radius: 999px;
          background: rgba(127, 127, 127, 0.09);
          border: 1px solid var(--divider-color);
          color: var(--primary-text-color);
          font-size: 12px;
          font-weight: 700;
          line-height: 1.1;
          min-width: 0;
        }
        .chip span {
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .chip.warn {
          color: var(--warning-color, #ff9800);
          background: rgba(255, 152, 0, 0.11);
          border-color: rgba(255, 152, 0, 0.35);
        }
        .notice {
          margin: 0 0 14px;
          padding: 10px 12px;
          border-radius: 10px;
          color: var(--secondary-text-color);
          background: rgba(127, 127, 127, 0.08);
          border: 1px solid var(--divider-color);
          font-size: 12px;
          line-height: 1.35;
        }
        .notice.warn {
          color: var(--warning-color, #ff9800);
          background: rgba(255, 152, 0, 0.10);
          border-color: rgba(255, 152, 0, 0.30);
        }
        .section-title {
          margin: 16px 0 8px;
          color: var(--secondary-text-color);
          font-size: 12px;
          font-weight: 800;
          letter-spacing: 0;
          text-transform: uppercase;
        }
        .chart-wrap {
          position: relative;
          border-radius: 12px;
          border: 1px solid var(--divider-color);
          background: rgba(127, 127, 127, 0.045);
          overflow: hidden;
          isolation: isolate;
          touch-action: none;
        }
        svg {
          display: block;
          width: 100%;
          height: auto;
          position: relative;
          z-index: 1;
        }
        .chart-tooltip-line {
          position: absolute;
          top: 0;
          bottom: 0;
          width: 1px;
          background: var(--primary-color, #03a9f4);
          opacity: 0;
          pointer-events: none;
          transform: translateX(-0.5px);
          z-index: 2;
        }
        .chart-tooltip {
          position: absolute;
          min-width: 154px;
          max-width: min(260px, calc(100% - 16px));
          padding: 8px 10px;
          border-radius: 8px;
          background: rgba(var(--rgb-card-background-color, 255, 255, 255), 0.22);
          color: var(--primary-text-color);
          box-shadow: 0 8px 22px rgba(0, 0, 0, 0.28);
          border: 1px solid var(--divider-color);
          font-size: 12px;
          line-height: 1.35;
          opacity: 0;
          pointer-events: none;
          transform: translate(-50%, -100%);
          z-index: 4;
        }
        .chart-tooltip-time {
          font-weight: 800;
          margin-bottom: 5px;
        }
        .chart-tooltip-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          white-space: nowrap;
        }
        .chart-tooltip-name {
          display: inline-flex;
          align-items: center;
          min-width: 0;
          gap: 6px;
          color: var(--secondary-text-color);
        }
        .chart-tooltip-dot {
          width: 7px;
          height: 7px;
          border-radius: 999px;
          flex: 0 0 auto;
        }
        .chart-tooltip-value {
          font-weight: 800;
        }
        .legend {
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
          margin: 10px 0 0;
        }
        .legend-item {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          color: var(--secondary-text-color);
          font-size: 11px;
          font-weight: 700;
          line-height: 1.1;
        }
        .swatch {
          width: 10px;
          height: 10px;
          border-radius: 999px;
          flex: 0 0 auto;
        }
        .battery-windows {
          display: grid;
          gap: 8px;
        }
        .window-row {
          display: grid;
          grid-template-columns: auto minmax(0, 1fr) minmax(86px, auto);
          gap: 10px;
          align-items: center;
          padding: 10px 11px;
          border-radius: 10px;
          border: 1px solid var(--divider-color);
          background: var(--ha-card-background, var(--card-background-color, #fff));
          border-left-width: 4px;
          min-width: 0;
        }
        .window-row.charge {
          border-left-color: #4CAF50;
        }
        .window-row.discharge {
          border-left-color: #FF9800;
        }
        .window-row.export {
          border-left-color: #FFD54F;
        }
        .window-pill {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          min-width: 66px;
          padding: 6px 8px;
          border-radius: 999px;
          font-size: 11px;
          font-weight: 900;
          line-height: 1;
          text-transform: uppercase;
          white-space: nowrap;
        }
        .window-row.charge .window-pill {
          color: #2E7D32;
          background: rgba(76, 175, 80, 0.14);
        }
        .window-row.discharge .window-pill {
          color: #EF6C00;
          background: rgba(255, 152, 0, 0.15);
        }
        .window-row.export .window-pill {
          color: #8A6A00;
          background: rgba(255, 213, 79, 0.22);
        }
        .window-main {
          min-width: 0;
        }
        .window-time {
          color: var(--primary-text-color);
          font-size: 14px;
          font-weight: 850;
          line-height: 1.2;
          letter-spacing: 0;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .window-meta {
          margin-top: 3px;
          color: var(--secondary-text-color);
          font-size: 11px;
          font-weight: 650;
          line-height: 1.2;
        }
        .actions {
          display: grid;
          gap: 8px;
          max-height: min(58vh, 620px);
          overflow-y: auto;
          overscroll-behavior: contain;
          padding-right: 2px;
          scrollbar-gutter: stable;
        }
        .action-row {
          display: grid;
          grid-template-columns: minmax(78px, auto) minmax(0, 1fr) minmax(82px, auto) auto;
          gap: 10px;
          align-items: center;
          padding: 9px 10px;
          border-radius: 10px;
          border: 1px solid var(--divider-color);
          background: var(--ha-card-background, var(--card-background-color, #fff));
          min-width: 0;
        }
        .action-time {
          color: var(--secondary-text-color);
          font-size: 12px;
          font-weight: 800;
          white-space: nowrap;
        }
        .action-main {
          min-width: 0;
        }
        .action-label {
          font-size: 14px;
          font-weight: 800;
          line-height: 1.2;
          letter-spacing: 0;
        }
        .action-meta {
          margin-top: 2px;
          color: var(--secondary-text-color);
          font-size: 11px;
          font-weight: 600;
          line-height: 1.2;
        }
        .soc {
          color: var(--primary-text-color);
          font-size: 15px;
          font-weight: 800;
          white-space: nowrap;
        }
        .price-stats {
          min-width: 82px;
          text-align: center;
        }
        .price-avg {
          font-size: 15px;
          font-weight: 900;
          line-height: 1.1;
          letter-spacing: 0;
        }
        .price-minmax {
          display: flex;
          justify-content: center;
          align-items: center;
          gap: 4px;
          margin-top: 3px;
          color: var(--secondary-text-color);
          font-size: 10px;
          font-weight: 800;
          line-height: 1.1;
          white-space: nowrap;
        }
        .price-kind {
          margin-top: 2px;
          color: var(--secondary-text-color);
          font-size: 9px;
          font-weight: 700;
          line-height: 1.1;
          white-space: nowrap;
        }
        .empty {
          padding: 11px 12px;
          border-radius: 10px;
          color: var(--secondary-text-color);
          background: rgba(127, 127, 127, 0.08);
          border: 1px solid var(--divider-color);
          font-size: 13px;
        }
        @media (max-width: 560px) {
          ha-card {
            padding: 13px;
          }
          .header {
            margin-bottom: 10px;
          }
          .title {
            font-size: 17px;
          }
          .action-row {
            grid-template-columns: minmax(0, 1fr) minmax(78px, auto) auto;
          }
          .action-time {
            grid-column: 1 / -1;
          }
          .price-stats {
            min-width: 78px;
          }
          .window-row {
            grid-template-columns: minmax(0, 1fr) minmax(78px, auto);
          }
          .window-pill {
            grid-column: 1 / -1;
            justify-self: start;
            min-width: 0;
          }
        }
      </style>
      <ha-card>
        <div class="header">
          <div>
            <h2 class="title">24-Hour Optimizer Plan</h2>
            <div class="subtitle">${this._escHtml(hasSchedule ? 'Now to next 24 hours from Smart Optimization' : 'Entity fallback while detailed schedule is unavailable')}</div>
          </div>
          <button class="refresh" type="button" title="Refresh optimizer plan" aria-label="Refresh optimizer plan">
            <ha-icon icon="mdi:refresh"></ha-icon>
          </button>
        </div>
        ${this._renderChips(model, priceMeta)}
        ${this._renderNotice(hasSchedule)}
        <div class="section-title">Planned Battery Windows</div>
        ${this._renderBatteryWindows(batteryWindows, priceMeta)}
        ${hasSchedule ? `
          <div class="section-title">SOC and Battery Power</div>
          <div class="chart-wrap soc-power-chart">${this._renderPowerChart(model, compact)}<div class="chart-tooltip-line"></div><div class="chart-tooltip"></div></div>
          ${this._renderLegend(model.powerSeries)}
          ${model.priceSeries.length ? `
            <div class="section-title">Electricity Price (${this._escHtml(priceMeta.minorPriceUnit)})</div>
            <div class="chart-wrap price-chart">${this._renderPriceChart(model, compact, priceMeta)}<div class="chart-tooltip-line"></div><div class="chart-tooltip"></div></div>
            ${this._renderLegend(model.priceSeries, false)}
          ` : ''}
        ` : ''}
        <div class="section-title">24-Hour Action Plan</div>
        ${this._renderActions(actions, model, priceMeta)}
      </ha-card>
    `;

    this.shadowRoot.querySelector('.refresh')?.addEventListener('click', () => this._maybeLoadData(true));
    if (hasSchedule) {
      this._attachOptimizerChartTooltip('.soc-power-chart', this._powerTooltipConfig(model, compact));
      if (model.priceSeries.length) {
        this._attachOptimizerChartTooltip('.price-chart', this._priceTooltipConfig(model, compact, priceMeta));
      }
    }
  }

  _buildModel() {
    const data = this._data || {};
    const schedule = data.schedule || {};
    const timestamps = Array.isArray(schedule.timestamps) ? schedule.timestamps : [];
    const soc = Array.isArray(schedule.soc) ? schedule.soc : [];
    const charge = Array.isArray(schedule.charge_w) ? schedule.charge_w : [];
    const discharge = Array.isArray(schedule.discharge_w) ? schedule.discharge_w : [];
    const detailed = Array.isArray(schedule.battery_consume_w) && Array.isArray(schedule.battery_export_w);
    const consume = detailed ? schedule.battery_consume_w : [];
    const exportPower = detailed ? schedule.battery_export_w : [];
    const ev = Array.isArray(schedule.ev_charging_w) ? schedule.ev_charging_w : [];
    const importPrice = Array.isArray(schedule.import_price) ? schedule.import_price : [];
    const exportPrice = Array.isArray(schedule.export_price) ? schedule.export_price : [];
    const intervalMinutes = this._intervalMinutes(timestamps);
    const count = Math.min(
      Math.round(24 * 60 / intervalMinutes),
      timestamps.length,
      soc.length,
      charge.length,
      discharge.length,
    );
    const points = [];
    for (let i = 0; i < count; i++) {
      points.push({
        index: i,
        timestamp: timestamps[i],
        soc: this._percent(soc[i]),
        chargeKw: this._kw(charge[i]),
        dischargeKw: this._kw(discharge[i]),
        consumeKw: detailed ? this._kw(consume[i]) : 0,
        exportKw: detailed ? this._kw(exportPower[i]) : 0,
        evKw: this._kw(ev[i]),
        importPrice: this._minorPrice(importPrice[i]),
        exportPrice: this._minorPrice(exportPrice[i]),
      });
    }

    const hasEv = points.some(p => p.evKw > 0);
    const hasPrices = points.some(p => Number.isFinite(p.importPrice)) || points.some(p => Number.isFinite(p.exportPrice));
    const powerSeries = detailed
      ? [
          { key: 'chargeKw', label: 'Charge', color: '#4CAF50' },
          { key: 'consumeKw', label: 'Powering Home', color: '#FF9800' },
          { key: 'exportKw', label: 'Export', color: '#FFD54F' },
        ]
      : [
          { key: 'chargeKw', label: 'Charge', color: '#4CAF50' },
          { key: 'dischargeKw', label: 'Discharge', color: '#FF9800' },
        ];
    if (hasEv) {
      powerSeries.push({ key: 'evKw', label: 'EV', color: '#7E57C2' });
    }
    const reserve = this._optimizerReserve(data);
    const idleHold = this._idleHoldReserve(data);

    return {
      raw: data,
      points,
      intervalMinutes,
      reservePercent: reserve.percent,
      reserveCalculated: reserve.calculated,
      exportReservePercent: reserve.exportPercent,
      exportReserveCalculated: reserve.exportCalculated,
      idleHoldActive: idleHold.active,
      idleHoldReservePercent: idleHold.percent,
      powerSeries,
      priceSeries: hasPrices
        ? [
            { key: 'importPrice', label: 'Import', color: '#FF9800' },
            { key: 'exportPrice', label: 'Export', color: '#4CAF50' },
          ]
        : [],
      demandRanges: this._demandRanges(points, data?.demand_window),
    };
  }

  _renderChips(model, priceMeta) {
    const data = model.raw || {};
    const chips = [];
    const status = data.monitoring_mode ? 'Monitoring' : data.enabled === false ? 'Disabled' : data.status || 'Active';
    chips.push(['Mode', this._title(status)]);
    chips.push(['Now', this._actionLabel(data.current_action || 'idle')]);
    if (data.next_action && data.next_action_time) {
      chips.push(['Next', `${this._actionLabel(data.next_action)} ${this._formatTime(data.next_action_time)}`]);
    }
    if (data.last_optimization) {
      chips.push(['Optimized', this._formatTime(data.last_optimization)]);
    }
    if (model.reserveCalculated && Number.isFinite(model.reservePercent)) {
      chips.push(['Auto Reserve', `${Math.round(model.reservePercent)}%`]);
    }
    if (model.exportReserveCalculated && Number.isFinite(model.exportReservePercent)) {
      chips.push(['Export Floor', `${Math.round(model.exportReservePercent)}%`]);
    }
    if (model.idleHoldActive && Number.isFinite(model.idleHoldReservePercent)) {
      chips.push(['IDLE Hold', `${Math.round(model.idleHoldReservePercent)}%`, true]);
    }
    const breakdown = data.daily_cost_breakdown || {};
    if (Number.isFinite(Number(breakdown.predicted_remaining))) {
      chips.push(['Remaining', this._formatMoney(Number(breakdown.predicted_remaining), priceMeta.currency)]);
    }
    if (Number.isFinite(Number(data.predicted_savings))) {
      chips.push(['Savings', this._formatMoney(Number(data.predicted_savings), priceMeta.currency)]);
    }
    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    if (warnings.length) {
      chips.push(['Warnings', String(warnings.length), true]);
    }
    return `<div class="chips">${chips.map(([name, value, warn]) => `
      <div class="chip${warn ? ' warn' : ''}"><span>${this._escHtml(name)}:</span><span>${this._escHtml(value)}</span></div>
    `).join('')}</div>`;
  }

  _renderNotice(hasSchedule) {
    const warnings = Array.isArray(this._data?.warnings) ? this._data.warnings : [];
    if (warnings.length) {
      const text = warnings.map(w => w?.message || w?.title).filter(Boolean).join(' ');
      return `<div class="notice warn">${this._escHtml(text)}</div>`;
    }
    if (this._error) {
      return `<div class="notice warn">${this._escHtml(this._error)}. Showing available entity data.</div>`;
    }
    if (!hasSchedule && this._loading) {
      return '<div class="notice">Loading Smart Optimization schedule...</div>';
    }
    if (!hasSchedule) {
      return '<div class="notice">Detailed optimizer schedule is not available yet. Showing planned force windows from Home Assistant entities.</div>';
    }
    return '';
  }

  _renderPowerChart(model, compact) {
    const { W, H, pad, chartW, chartH, powerMax } = this._powerChartMetrics(model, compact);
    const x = (i) => pad.left + (i / Math.max(1, model.points.length - 1)) * chartW;
    const yPower = (value) => pad.top + chartH - this._optimizerPowerRatio(value, powerMax) * chartH;
    const ySoc = (value) => pad.top + chartH - (Math.max(0, Math.min(100, value)) / 100) * chartH;
    let svg = this._chartGrid(W, H, pad, model.points, compact, `${powerMax} kW`);

    for (const range of model.demandRanges) {
      const x1 = x(range.start);
      const x2 = x(range.end);
      svg += `<rect x="${x1}" y="${pad.top}" width="${Math.max(2, x2 - x1)}" height="${chartH}" fill="#FF6B6B" opacity="0.11"/>`;
    }

    if (Number.isFinite(model.reservePercent)) {
      const ry = ySoc(model.reservePercent);
      const reserveLabel = model.reserveCalculated ? 'Calculated Reserve' : 'Reserve';
      svg += `<rect x="${pad.left}" y="${ry}" width="${chartW}" height="${pad.top + chartH - ry}" fill="#F44336" opacity="0.06"/>`;
      svg += `<line x1="${pad.left}" y1="${ry}" x2="${W - pad.right}" y2="${ry}" stroke="#F44336" stroke-width="1" stroke-dasharray="5,3" opacity="0.75"/>`;
      svg += `<text x="${W - pad.right - 4}" y="${ry - 5}" text-anchor="end" font-size="${compact ? 9 : 10}" fill="#F44336">${this._escSvg(`${reserveLabel} ${Math.round(model.reservePercent)}%`)}</text>`;
    }
    if (model.exportReserveCalculated && Number.isFinite(model.exportReservePercent)) {
      const exportY = ySoc(model.exportReservePercent);
      const exportLabelY = Math.max(pad.top + 11, exportY - 5);
      svg += `<line x1="${pad.left}" y1="${exportY}" x2="${W - pad.right}" y2="${exportY}" stroke="#FF5252" stroke-width="1.4" stroke-dasharray="7,3" opacity="0.9"/>`;
      svg += `<text x="${pad.left + 4}" y="${exportLabelY}" text-anchor="start" font-size="${compact ? 9 : 10}" fill="#FF5252">${this._escSvg(`Export Floor ${Math.round(model.exportReservePercent)}%`)}</text>`;
    }
    if (model.idleHoldActive && Number.isFinite(model.idleHoldReservePercent)) {
      const holdY = ySoc(model.idleHoldReservePercent);
      const labelY = Math.max(pad.top + 11, holdY - 5);
      svg += `<line x1="${pad.left}" y1="${holdY}" x2="${W - pad.right}" y2="${holdY}" stroke="#FF9800" stroke-width="1.4" stroke-dasharray="2,4" opacity="0.9"/>`;
      svg += `<text x="${pad.left + 4}" y="${labelY}" text-anchor="start" font-size="${compact ? 9 : 10}" fill="#FF9800">${this._escSvg(`IDLE Hold ${Math.round(model.idleHoldReservePercent)}%`)}</text>`;
    }

    for (const series of model.powerSeries) {
      svg += `<path d="${this._stepPath(model.points, x, yPower, series.key)}" fill="none" stroke="${series.color}" stroke-width="2.1" stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/>`;
    }
    svg += `<path d="${this._linePath(model.points, x, ySoc, 'soc')}" fill="none" stroke="#42A5F5" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>`;

    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="24-hour optimizer SOC and power chart">${svg}</svg>`;
  }

  _renderPriceChart(model, compact, priceMeta) {
    const { W, H, pad, chartW, chartH, maxPrice } = this._priceChartMetrics(model, compact);
    const x = (i) => pad.left + (i / Math.max(1, model.points.length - 1)) * chartW;
    const y = (value) => pad.top + chartH - (Math.max(0, value) / maxPrice) * chartH;
    let svg = this._chartGrid(W, H, pad, model.points, compact, `${maxPrice} ${priceMeta.minorUnit}`);

    for (const range of model.demandRanges) {
      const x1 = x(range.start);
      const x2 = x(range.end);
      svg += `<rect x="${x1}" y="${pad.top}" width="${Math.max(2, x2 - x1)}" height="${chartH}" fill="#FF6B6B" opacity="0.09"/>`;
    }
    for (const series of model.priceSeries) {
      svg += `<path d="${this._stepPath(model.points, x, y, series.key)}" fill="none" stroke="${series.color}" stroke-width="2.05" stroke-linejoin="round" stroke-linecap="round"/>`;
    }

    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="24-hour import and export price chart">${svg}</svg>`;
  }

  _powerChartMetrics(model, compact) {
    const W = compact ? 520 : 720;
    const H = compact ? 240 : 270;
    const pad = { top: 18, right: 14, bottom: 34, left: compact ? 44 : 52 };
    const chartW = W - pad.left - pad.right;
    const chartH = H - pad.top - pad.bottom;
    const powerMax = Math.max(4, this._niceCeil(Math.max(
      ...model.points.flatMap(p => model.powerSeries.map(s => p[s.key] || 0)),
    )));
    return { W, H, pad, chartW, chartH, powerMax };
  }

  _optimizerPowerRatio(value, maxValue) {
    const safeMax = Math.max(Number(maxValue) || 0, 1);
    const ratio = Math.max(0, Math.min(1, (Number(value) || 0) / safeMax));
    return Math.pow(ratio, OPTIMIZER_POWER_AXIS_EXPONENT);
  }

  _priceChartMetrics(model, compact) {
    const W = compact ? 520 : 720;
    const H = compact ? 165 : 185;
    const pad = { top: 16, right: 14, bottom: 32, left: compact ? 48 : 58 };
    const chartW = W - pad.left - pad.right;
    const chartH = H - pad.top - pad.bottom;
    const maxPrice = Math.max(10, this._niceCeil(Math.max(
      ...model.points.flatMap(p => model.priceSeries.map(s => Number.isFinite(p[s.key]) ? p[s.key] : 0)),
    )));
    return { W, H, pad, chartW, chartH, maxPrice };
  }

  _powerTooltipConfig(model, compact) {
    const metrics = this._powerChartMetrics(model, compact);
    return {
      ...metrics,
      points: model.points,
      rows: (point) => {
        const rows = [
          { label: 'SOC', color: '#42A5F5', value: `${Math.round(point.soc)}%` },
          ...model.powerSeries.map(series => ({
            label: series.label,
            color: series.color,
            value: `${Number(point[series.key] || 0).toFixed(2)} kW`,
          })),
        ];
        if (Number.isFinite(model.reservePercent)) {
          rows.push({
            label: model.reserveCalculated ? 'Calculated Reserve' : 'Reserve',
            color: '#F44336',
            value: `${Math.round(model.reservePercent)}%`,
          });
        }
        if (model.exportReserveCalculated && Number.isFinite(model.exportReservePercent)) {
          rows.push({
            label: 'Export Floor',
            color: '#FF5252',
            value: `${Math.round(model.exportReservePercent)}%`,
          });
        }
        if (model.idleHoldActive && Number.isFinite(model.idleHoldReservePercent)) {
          rows.push({ label: 'IDLE Hold', color: '#FF9800', value: `${Math.round(model.idleHoldReservePercent)}%` });
        }
        return rows;
      },
    };
  }

  _priceTooltipConfig(model, compact, priceMeta) {
    const metrics = this._priceChartMetrics(model, compact);
    return {
      ...metrics,
      points: model.points,
      rows: (point) => model.priceSeries.map(series => ({
        label: series.label,
        color: series.color,
        value: this._formatMinorPrice(point[series.key], priceMeta.minorUnit),
      })),
    };
  }

  _attachOptimizerChartTooltip(selector, chart) {
    const wrap = this.shadowRoot.querySelector(selector);
    const svg = wrap?.querySelector('svg');
    const line = wrap?.querySelector('.chart-tooltip-line');
    const tooltip = wrap?.querySelector('.chart-tooltip');
    if (!wrap || !svg || !line || !tooltip || !chart?.points?.length) return;

    const hide = () => {
      line.style.opacity = '0';
      tooltip.style.opacity = '0';
    };

    const move = (event) => {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      const localX = ((event.clientX - rect.left) / rect.width) * chart.W;
      if (localX < chart.pad.left || localX > chart.W - chart.pad.right) {
        hide();
        return;
      }
      const ratio = (localX - chart.pad.left) / Math.max(1, chart.chartW);
      const index = Math.max(0, Math.min(chart.points.length - 1, Math.round(ratio * (chart.points.length - 1))));
      const point = chart.points[index];
      if (!point) {
        hide();
        return;
      }

      const anchorX = chart.pad.left + (index / Math.max(1, chart.points.length - 1)) * chart.chartW;
      const cssX = (anchorX / chart.W) * rect.width;
      const rows = chart.rows(point)
        .filter(row => row && row.value !== '')
        .map(row => `
          <div class="chart-tooltip-row">
            <span class="chart-tooltip-name">
              <span class="chart-tooltip-dot" style="background:${row.color}"></span>
              <span>${this._escHtml(row.label)}</span>
            </span>
            <span class="chart-tooltip-value">${this._escHtml(row.value)}</span>
          </div>
        `).join('');

      tooltip.innerHTML = `
        <div class="chart-tooltip-time">${this._escHtml(this._formatTime(point.timestamp))}</div>
        ${rows}
      `;
      line.style.left = `${cssX}px`;
      line.style.opacity = '0.75';
      tooltip.style.left = `${Math.min(Math.max(cssX, 84), rect.width - 84)}px`;
      const tooltipBottom = Math.max(34, rect.height - chart.pad.bottom - 8);
      if (tooltip.offsetHeight && tooltipBottom - tooltip.offsetHeight < 8) {
        tooltip.style.transform = 'translate(-50%, 0)';
        tooltip.style.top = '8px';
      } else {
        tooltip.style.transform = 'translate(-50%, -100%)';
        tooltip.style.top = `${tooltipBottom}px`;
      }
      tooltip.style.opacity = '1';
    };

    svg.addEventListener('pointermove', move);
    svg.addEventListener('pointerleave', hide);
    svg.addEventListener('pointercancel', hide);
  }

  _chartGrid(W, H, pad, points, compact, maxLabel) {
    const chartW = W - pad.left - pad.right;
    const chartH = H - pad.top - pad.bottom;
    let svg = `<rect x="${pad.left}" y="${pad.top}" width="${chartW}" height="${chartH}" fill="var(--card-background-color, transparent)" opacity="0.02" stroke="var(--divider-color, #e0e0e0)" stroke-width="0.6" rx="8"/>`;
    for (let i = 0; i <= 4; i++) {
      const y = pad.top + (chartH / 4) * i;
      svg += `<line x1="${pad.left}" y1="${y}" x2="${W - pad.right}" y2="${y}" stroke="var(--divider-color, #e0e0e0)" stroke-width="0.4" stroke-dasharray="4,3" opacity="0.65"/>`;
    }
    svg += `<text x="${pad.left - 4}" y="${pad.top + 4}" text-anchor="end" font-size="${compact ? 9 : 10}" fill="var(--secondary-text-color, #888)">${this._escSvg(maxLabel)}</text>`;
    const labelEvery = Math.max(1, Math.round((6 * 60) / this._intervalMinutes(points.map(p => p.timestamp))));
    for (let i = 0; i < points.length; i += labelEvery) {
      const x = pad.left + (i / Math.max(1, points.length - 1)) * chartW;
      svg += `<line x1="${x}" y1="${pad.top}" x2="${x}" y2="${pad.top + chartH}" stroke="var(--divider-color, #e0e0e0)" stroke-width="0.35" opacity="0.55"/>`;
      svg += `<text x="${x}" y="${H - pad.bottom + 21}" text-anchor="middle" font-size="${compact ? 9 : 10}" fill="var(--secondary-text-color, #888)">${this._escSvg(this._formatTime(points[i]?.timestamp))}</text>`;
    }
    return svg;
  }

  _renderLegend(series, includeSoc = true) {
    return `<div class="legend">
      ${includeSoc ? '<div class="legend-item"><span class="swatch" style="background:#42A5F5"></span><span>SOC</span></div>' : ''}
      ${series.map(s => `<div class="legend-item"><span class="swatch" style="background:${s.color}"></span><span>${this._escHtml(s.label)}</span></div>`).join('')}
    </div>`;
  }

  _renderBatteryWindows(windows, priceMeta) {
    if (!windows.length) {
      return '<div class="empty">No planned charge, discharge, or export windows in the next 24 hours.</div>';
    }
    return `<div class="battery-windows">${windows.slice(0, 8).map(window => {
      const info = this._actionInfo(window.action);
      const meta = [
        this._formatDuration(window.durationMinutes),
        window.socLabel,
        window.power_w > 0 ? this._formatPower(window.power_w) : '',
      ].filter(Boolean).join(' - ');
      return `
        <div class="window-row ${this._escHtml(window.action)}">
          <div class="window-pill">${this._escHtml(info.shortLabel || info.label)}</div>
          <div class="window-main">
            <div class="window-time">${this._escHtml(this._timeRange(window.timestamp, window.end_time))}</div>
            ${meta ? `<div class="window-meta">${this._escHtml(meta)}</div>` : ''}
          </div>
          ${this._renderActionPriceStats(window.priceStats, window.action, priceMeta)}
        </div>
      `;
    }).join('')}${windows.length > 8 ? `<div class="empty">+${windows.length - 8} more battery window${windows.length - 8 === 1 ? '' : 's'}</div>` : ''}</div>`;
  }

  _renderActions(actions, model, priceMeta) {
    if (!actions.length) {
      return '<div class="empty">No optimizer actions scheduled in the next 24 hours.</div>';
    }
    return `<div class="actions">${actions.map(action => {
      const info = this._actionInfo(action.action);
      const priceStats = this._priceStatsForAction(action, model);
      const power = Number(action.power_w || 0);
      const meta = [
        power > 0 ? this._formatPower(power) : '',
        action.source === 'fallback' ? 'entity fallback' : '',
      ].filter(Boolean).join(' - ');
      const socValue = Number(action.soc);
      const soc = Number.isFinite(socValue) ? `${Math.round(this._percent(socValue))}%` : '';
      return `
        <div class="action-row">
          <div class="action-time">${this._escHtml(this._timeRange(action.timestamp, action.end_time))}</div>
          <div class="action-main">
            <div class="action-label" style="color:${info.color}">${this._escHtml(info.label)}</div>
            ${meta ? `<div class="action-meta">${this._escHtml(meta)}</div>` : ''}
          </div>
          ${this._renderActionPriceStats(priceStats, action.action, priceMeta)}
          <div class="soc">${this._escHtml(soc)}</div>
        </div>
      `;
    }).join('')}</div>`;
  }

  _renderActionPriceStats(stats, action, priceMeta) {
    if (!stats) return '<div class="price-stats"></div>';
    const avgColor = this._priceAvgColor(stats.avg, action);
    return `
      <div class="price-stats">
        <div class="price-avg" style="color:${avgColor}">${this._escHtml(this._formatMinorPrice(stats.avg, priceMeta.minorUnit))}</div>
        <div class="price-minmax">
          <span style="color:#4CAF50">${this._escHtml(this._formatMinorPrice(stats.min, priceMeta.minorUnit))}</span>
          <span style="opacity:0.45">|</span>
          <span style="color:#F44336">${this._escHtml(this._formatMinorPrice(stats.max, priceMeta.minorUnit))}</span>
        </div>
        <div class="price-kind">${this._escHtml(stats.kind)} avg min max</div>
      </div>
    `;
  }

  _priceStatsForAction(action, model) {
    if (!action?.timestamp || !model?.points?.length) return null;
    const start = Date.parse(action.timestamp);
    const end = Date.parse(action.end_time || action.timestamp);
    if (!Number.isFinite(start)) return null;
    const safeEnd = Number.isFinite(end) && end > start
      ? end
      : start + (model.intervalMinutes || 5) * 60000;
    const useExport = action.action === 'discharge' || action.action === 'export';
    const key = useExport ? 'exportPrice' : 'importPrice';
    const values = model.points
      .filter(point => {
        const ts = Date.parse(point.timestamp);
        return Number.isFinite(ts) && ts >= start && ts < safeEnd;
      })
      .map(point => point[key])
      .filter(value => Number.isFinite(value));
    if (!values.length) return null;
    return {
      kind: useExport ? 'export' : 'import',
      min: Math.min(...values),
      max: Math.max(...values),
      avg: values.reduce((sum, value) => sum + value, 0) / values.length,
    };
  }

  _actionRangesFromApi() {
    const actions = Array.isArray(this._data?.next_actions) ? this._data.next_actions : [];
    return actions
      .filter(action => action?.timestamp)
      .map(action => ({ ...action }))
      .slice(0, 24);
  }

  _fallbackActionRanges() {
    const ranges = [];
    const pushWindows = (entityId, fallbackAction) => {
      const windows = this._hass?.states?.[entityId]?.attributes?.windows;
      if (!Array.isArray(windows)) return;
      for (const window of windows) {
        if (!window?.start_time || !window?.end_time) continue;
        ranges.push({
          action: window.action || fallbackAction,
          timestamp: window.start_time,
          end_time: window.end_time,
          power_w: window.power_w,
          soc: window.soc,
          source: 'fallback',
        });
      }
    };
    pushWindows(this._config.forceChargeEntity, 'charge');
    pushWindows(this._config.forceDischargeEntity, 'discharge');
    return ranges.sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
  }

  _batteryWindowsFromActions(actions, model) {
    return (actions || [])
      .filter(action => this._isBatteryWindowAction(action?.action))
      .map(action => ({
        ...action,
        durationMinutes: this._durationMinutes(action.timestamp, action.end_time, model.intervalMinutes),
        socLabel: this._socRangeForAction(action, model),
        priceStats: this._priceStatsForAction(action, model),
      }));
  }

  _isBatteryWindowAction(action) {
    return action === 'charge' || action === 'discharge' || action === 'export';
  }

  _durationMinutes(startValue, endValue, fallbackMinutes = 5) {
    const start = Date.parse(startValue);
    const end = Date.parse(endValue);
    if (Number.isFinite(start) && Number.isFinite(end) && end > start) {
      return Math.round((end - start) / 60000);
    }
    return Number.isFinite(Number(fallbackMinutes)) ? Number(fallbackMinutes) : 5;
  }

  _socRangeForAction(action, model) {
    const start = Date.parse(action?.timestamp);
    const end = Date.parse(action?.end_time || action?.timestamp);
    const points = (model?.points || []).filter(point => {
      const ts = Date.parse(point.timestamp);
      return Number.isFinite(ts) && Number.isFinite(start) && ts >= start && (!Number.isFinite(end) || ts < end);
    });
    const values = points.map(point => Number(point.soc)).filter(Number.isFinite);
    if (!values.length) {
      const fallback = Number(action?.soc);
      return Number.isFinite(fallback) ? `${Math.round(this._percent(fallback))}% SOC` : '';
    }
    const first = Math.round(values[0]);
    const last = Math.round(values[values.length - 1]);
    return first === last ? `${first}% SOC` : `${first}% -> ${last}% SOC`;
  }

  _demandRanges(points, demandWindow) {
    if (!demandWindow?.start_time || !demandWindow?.end_time || !points.length) return [];
    const start = this._clockMinutes(demandWindow.start_time);
    const end = this._clockMinutes(demandWindow.end_time);
    if (!Number.isFinite(start) || !Number.isFinite(end)) return [];
    const ranges = [];
    for (let i = 0; i < points.length; i++) {
      const current = this._timestampClockMinutes(points[i].timestamp);
      const inWindow = end <= start ? current >= start || current < end : current >= start && current < end;
      if (!inWindow) continue;
      const last = ranges[ranges.length - 1];
      if (last && last.end === i - 1) last.end = i;
      else ranges.push({ start: i, end: i });
    }
    return ranges;
  }

  _linePath(points, xScale, yScale, key) {
    return points.map((point, index) => {
      const x = xScale(index);
      const y = yScale(point[key]);
      return `${index === 0 ? 'M' : 'L'}${x},${y}`;
    }).join('');
  }

  _stepPath(points, xScale, yScale, key) {
    let path = '';
    for (let i = 0; i < points.length; i++) {
      const x = xScale(i);
      const y = yScale(Number.isFinite(points[i][key]) ? points[i][key] : 0);
      if (i === 0) path += `M${x},${y}`;
      else path += `H${x}V${y}`;
    }
    return path;
  }

  _intervalMinutes(timestamps) {
    if (Array.isArray(timestamps) && timestamps.length > 1) {
      const first = Date.parse(timestamps[0]);
      const second = Date.parse(timestamps[1]);
      const delta = (second - first) / 60000;
      if (Number.isFinite(delta) && delta > 0) return delta;
    }
    if (Array.isArray(timestamps) && timestamps.length > 300) {
      return (48 * 60) / timestamps.length;
    }
    return 5;
  }

  _percent(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return 0;
    return n <= 1 ? n * 100 : n;
  }

  _kw(value) {
    const n = Number(value);
    return Number.isFinite(n) && n > 0 ? n / 1000 : 0;
  }

  _minorPrice(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n * 100 : NaN;
  }

  _reservePercent(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return NaN;
    return n <= 1 ? n * 100 : n;
  }

  _optimizerReserve(data) {
    const recommendation = data?.reserve_recommendation || {};
    const autoApplyEnabled = data?.auto_apply_reserve_enabled === true ||
      data?.config?.auto_apply_reserve_enabled === true ||
      recommendation?.auto_apply_enabled === true;
    const appliedReserve = this._clampedPercent(recommendation?.applied_optimizer_reserve_percent);
    const exportReserve = Math.max(
      this._clampedPercent(recommendation?.applied_export_reserve_floor_percent),
      this._clampedPercent(recommendation?.home_load_export_floor_percent),
    );
    const exportCalculated = autoApplyEnabled &&
      Number.isFinite(exportReserve) &&
      Number.isFinite(appliedReserve) &&
      exportReserve > appliedReserve + 0.5;
    if (autoApplyEnabled && Number.isFinite(appliedReserve)) {
      return {
        percent: appliedReserve,
        calculated: true,
        exportPercent: exportCalculated ? exportReserve : NaN,
        exportCalculated,
      };
    }
    const configuredReserve = this._reservePercent(data?.config?.backup_reserve);
    return {
      percent: Number.isFinite(configuredReserve) ? Math.max(0, Math.min(100, configuredReserve)) : NaN,
      calculated: false,
      exportPercent: NaN,
      exportCalculated: false,
    };
  }

  _idleHoldReserve(data) {
    const active = data?.idle_hold_active === true || data?.config?.idle_hold_active === true;
    const rawPercent = this._reservePercent(
      data?.idle_hold_reserve_percent ??
      data?.config?.idle_hold_reserve_percent ??
      data?.idle_hold_reserve ??
      data?.config?.idle_hold_reserve
    );
    return {
      active,
      percent: active && Number.isFinite(rawPercent)
        ? Math.max(0, Math.min(100, rawPercent))
        : NaN,
    };
  }

  _clampedPercent(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return NaN;
    return Math.max(0, Math.min(100, n));
  }

  _niceCeil(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return 1;
    if (n <= 4) return 4;
    if (n <= 8) return 8;
    if (n <= 12) return 12;
    if (n <= 20) return 20;
    if (n <= 40) return 40;
    return Math.ceil(n / 20) * 20;
  }

  _actionInfo(action) {
    const map = {
      idle: { label: 'Idle', shortLabel: 'Idle', color: 'var(--secondary-text-color, #888)' },
      charge: { label: 'Charging', shortLabel: 'Charge', color: '#4CAF50' },
      discharge: { label: 'Discharging', shortLabel: 'Discharge', color: '#FF9800' },
      consume: { label: 'Powering Home', shortLabel: 'Home', color: '#FF9800' },
      export: { label: 'Exporting', shortLabel: 'Export', color: '#FFD54F' },
      self_consumption: { label: 'Self Consumption', shortLabel: 'Self', color: '#42A5F5' },
    };
    return map[action] || map.idle;
  }

  _actionLabel(action) {
    return this._actionInfo(action).label;
  }

  _formatPower(watts) {
    const value = Number(watts || 0);
    if (Math.abs(value) >= 1000) return `${(Math.abs(value) / 1000).toFixed(1)} kW`;
    return `${Math.round(Math.abs(value))} W`;
  }

  _formatDuration(minutes) {
    const value = Number(minutes || 0);
    if (!Number.isFinite(value) || value <= 0) return '';
    if (value < 60) return `${Math.round(value)} min`;
    const hours = Math.floor(value / 60);
    const mins = Math.round(value % 60);
    return mins ? `${hours}h ${mins}m` : `${hours}h`;
  }

  _formatMinorPrice(value, minorUnit) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '--';
    const decimals = Math.abs(n) >= 10 ? 1 : 2;
    return `${n.toFixed(decimals)}${minorUnit || ''}`;
  }

  _priceAvgColor(value, action) {
    const n = Number(value);
    if (!Number.isFinite(n)) return 'var(--secondary-text-color, #888)';
    if (action === 'discharge' || action === 'export') {
      if (n >= 20) return '#4CAF50';
      if (n >= 10) return '#FBC02D';
      if (n >= 0) return '#FF9800';
      return '#F44336';
    }
    if (n <= 10) return '#4CAF50';
    if (n <= 25) return '#FBC02D';
    if (n <= 40) return '#FF9800';
    return '#F44336';
  }

  _formatMoney(value, currency) {
    try {
      return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency || 'AUD' }).format(value);
    } catch (_) {
      return `${currency || 'AUD'} ${value.toFixed(2)}`;
    }
  }

  _formatTime(value) {
    if (!value) return '--:--';
    const text = String(value);
    if (text.length >= 16 && text[10] === 'T') return text.substring(11, 16);
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '--:--';
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  _timeRange(start, end) {
    const startText = this._formatTime(start);
    const endText = this._formatTime(end);
    return endText && endText !== '--:--' && endText !== startText ? `${startText} - ${endText}` : startText;
  }

  _clockMinutes(value) {
    const [hours, minutes] = String(value || '').split(':').map(Number);
    if (!Number.isFinite(hours)) return NaN;
    return hours * 60 + (Number.isFinite(minutes) ? minutes : 0);
  }

  _timestampClockMinutes(value) {
    const text = String(value || '');
    if (text.length >= 16 && text[10] === 'T') {
      return Number(text.substring(11, 13)) * 60 + Number(text.substring(14, 16));
    }
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return 0;
    return date.getHours() * 60 + date.getMinutes();
  }

  _title(value) {
    return String(value || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }

  _escHtml(value) {
    return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  _escSvg(value) {
    return this._escHtml(value);
  }
}

if (!customElements.get('power-sync-optimization-plan')) {
  customElements.define('power-sync-optimization-plan', PowerSyncOptimizationPlan);
}

const EV_PANEL_FETCH_INTERVAL_MS = 30000;
const EV_PANEL_CACHE = window.__powerSyncEVPanelCache || new Map();
window.__powerSyncEVPanelCache = EV_PANEL_CACHE;

const EV_PANEL_PATHS = {
  status: 'power_sync/ev/loadpoints/status',
  solar: 'power_sync/ev/solar_surplus_config',
  price: 'power_sync/ev/price_level_charging/settings',
  scheduled: 'power_sync/ev/scheduled_charging/settings',
  autoStatus: 'power_sync/ev/auto_schedule/status',
  autoToggle: 'power_sync/ev/auto_schedule/toggle',
  boost: 'power_sync/ev/boost',
};

class PowerSyncEVPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._data = null;
    this._error = null;
    this._notice = null;
    this._loading = false;
    this._savingKey = null;
    this._lastFetch = 0;
    this._renderQueued = false;
    this._lastRenderSignature = '';
    this._resizeObserver = null;
    this._pollTimer = null;
    this._selectedLoadpointId = null;
    this._durationMinutes = 60;
    this._policy = 'solar_only';
  }

  setConfig(config) {
    this._config = config || {};
    this._lastRenderSignature = '';
    this._restoreCachedData();
    this._scheduleRender();
    this._maybeLoadData(true);
  }

  set hass(hass) {
    this._hass = hass;
    this._maybeLoadData(false);
    this._scheduleRenderIfChanged();
  }

  connectedCallback() {
    if (!this._resizeObserver && 'ResizeObserver' in window) {
      this._resizeObserver = new ResizeObserver(() => this._scheduleRenderIfChanged());
      this._resizeObserver.observe(this);
    }
    if (!this._pollTimer) {
      this._pollTimer = window.setInterval(() => this._maybeLoadData(false), EV_PANEL_FETCH_INTERVAL_MS);
    }
    this._maybeLoadData(true);
    this._scheduleRender();
  }

  disconnectedCallback() {
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
      this._resizeObserver = null;
    }
    if (this._pollTimer) {
      window.clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  getCardSize() {
    return 8;
  }

  _cacheKey() {
    return this._config?.cacheKey || 'power-sync-ev-panel';
  }

  _restoreCachedData() {
    const cached = EV_PANEL_CACHE.get(this._cacheKey());
    if (!cached?.data) return false;
    const fetchedAt = cached.fetchedAt || 0;
    if (Date.now() - fetchedAt >= EV_PANEL_FETCH_INTERVAL_MS) return false;
    this._data = cached.data;
    this._error = null;
    this._lastFetch = fetchedAt;
    this._syncSelectedLoadpoint();
    return true;
  }

  _maybeLoadData(force) {
    if (!this._hass || typeof this._hass.callApi !== 'function') return;
    const now = Date.now();
    if (this._loading) return;
    if (!force && this._data && now - this._lastFetch < EV_PANEL_FETCH_INTERVAL_MS) return;
    if (!force && this._restoreCachedData()) {
      this._scheduleRender();
      return;
    }

    const cacheKey = this._cacheKey();
    const cached = EV_PANEL_CACHE.get(cacheKey);
    if (!force && cached?.promise) {
      this._adoptLoadPromise(cacheKey, cached.promise);
      return;
    }

    this._loadData(cacheKey);
  }

  _adoptLoadPromise(cacheKey, promise) {
    this._loading = true;
    promise
      .then((data) => {
        if (cacheKey !== this._cacheKey()) return;
        const cached = EV_PANEL_CACHE.get(cacheKey);
        this._data = data;
        this._error = null;
        this._lastFetch = cached?.fetchedAt || Date.now();
        this._syncSelectedLoadpoint();
      })
      .catch((err) => {
        if (cacheKey !== this._cacheKey()) return;
        this._error = err?.message || 'EV API unavailable';
      })
      .finally(() => {
        this._loading = false;
        this._scheduleRender();
      });
  }

  async _loadData(cacheKey = this._cacheKey()) {
    this._loading = true;
    const request = this._fetchBundle();
    const previous = EV_PANEL_CACHE.get(cacheKey);
    EV_PANEL_CACHE.set(cacheKey, { ...(previous || {}), promise: request });
    try {
      const data = await request;
      this._data = data;
      this._error = null;
      this._lastFetch = Date.now();
      this._syncSelectedLoadpoint();
      EV_PANEL_CACHE.set(cacheKey, {
        data: this._data,
        fetchedAt: this._lastFetch,
      });
    } catch (err) {
      this._error = err?.message || 'EV API unavailable';
      const cached = EV_PANEL_CACHE.get(cacheKey);
      if (cached?.promise === request) {
        if (previous?.data) {
          EV_PANEL_CACHE.set(cacheKey, previous);
        } else {
          EV_PANEL_CACHE.delete(cacheKey);
        }
      }
    } finally {
      this._loading = false;
      this._scheduleRender();
    }
  }

  async _fetchBundle() {
    const status = await this._hass.callApi('GET', 'power_sync/ev/loadpoints/status');
    if (status?.success === false) {
      throw new Error(status.error || 'EV status API unavailable');
    }

    const [solar, price, scheduled, autoStatus] = await Promise.allSettled([
      this._hass.callApi('GET', EV_PANEL_PATHS.solar),
      this._hass.callApi('GET', EV_PANEL_PATHS.price),
      this._hass.callApi('GET', EV_PANEL_PATHS.scheduled),
      this._hass.callApi('GET', EV_PANEL_PATHS.autoStatus),
    ]);

    const modeErrors = [];
    const unwrap = (result, key, fallback) => {
      if (result.status !== 'fulfilled') {
        modeErrors.push(result.reason?.message || `${key} API unavailable`);
        return fallback;
      }
      if (result.value?.success === false) {
        modeErrors.push(result.value.error || `${key} API unavailable`);
        return fallback;
      }
      return result.value?.[key] || fallback;
    };

    let autoScheduleStatus = null;
    if (autoStatus.status === 'fulfilled' && autoStatus.value?.success !== false) {
      autoScheduleStatus = autoStatus.value;
    } else if (autoStatus.status === 'fulfilled') {
      modeErrors.push(autoStatus.value?.error || 'auto schedule API unavailable');
    } else {
      modeErrors.push(autoStatus.reason?.message || 'auto schedule API unavailable');
    }

    return {
      status,
      solarConfig: unwrap(solar, 'config', {}),
      priceSettings: unwrap(price, 'settings', {}),
      scheduledSettings: unwrap(scheduled, 'settings', {}),
      autoStatus: autoScheduleStatus,
      modeErrors,
      fetchedAt: new Date().toISOString(),
    };
  }

  _scheduleRender() {
    if (this._renderQueued) return;
    this._renderQueued = true;
    requestAnimationFrame(() => {
      this._renderQueued = false;
      this._render();
    });
  }

  _scheduleRenderIfChanged() {
    const signature = this._renderSignature();
    if (signature !== this._lastRenderSignature) {
      this._scheduleRender();
    }
  }

  _renderSignature() {
    const width = this.getBoundingClientRect().width || 0;
    return JSON.stringify({
      width: width < 560 ? 'compact' : 'wide',
      data: this._data,
      error: this._error,
      notice: this._notice,
      loading: this._loading,
      savingKey: this._savingKey,
      selected: this._selectedLoadpointId,
      duration: this._durationMinutes,
      policy: this._policy,
      fetched: this._lastFetch,
    });
  }

  _loadpoints() {
    return Array.isArray(this._data?.status?.loadpoints) ? this._data.status.loadpoints : [];
  }

  _selectedLoadpoint() {
    const loadpoints = this._loadpoints();
    return loadpoints.find((lp) => lp.loadpoint_id === this._selectedLoadpointId) || loadpoints[0] || null;
  }

  _syncSelectedLoadpoint() {
    const loadpoints = this._loadpoints();
    if (loadpoints.length === 0) {
      this._selectedLoadpointId = null;
      return;
    }
    if (!this._selectedLoadpointId || !loadpoints.some((lp) => lp.loadpoint_id === this._selectedLoadpointId)) {
      this._selectedLoadpointId = loadpoints[0].loadpoint_id;
    }
  }

  _ownerConflict(loadpoint) {
    if (!loadpoint || loadpoint.owner !== 'powersync') return false;
    const ownerMode = String(loadpoint.owner_mode || '');
    return !!ownerMode && !ownerMode.startsWith('manual');
  }

  _canStart(loadpoint) {
    return !!loadpoint && loadpoint.connected && !this._ownerConflict(loadpoint) && !this._savingKey;
  }

  _canStop(loadpoint) {
    return !!loadpoint && !this._savingKey && (loadpoint.owner === 'powersync' || loadpoint.actual_charging || loadpoint.quick_control);
  }

  _render() {
    this._lastRenderSignature = this._renderSignature();
    this._syncSelectedLoadpoint();
    const hasApi = !!(this._hass && typeof this._hass.callApi === 'function');
    const loadpoints = this._loadpoints();
    const selected = this._selectedLoadpoint();
    const conflict = this._ownerConflict(selected);
    const canStart = this._canStart(selected);
    const canStop = this._canStop(selected);

    this.shadowRoot.innerHTML = `
      <style>
        ha-card {
          padding: 16px;
          overflow: hidden;
        }
        .header {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          gap: 12px;
          margin-bottom: 12px;
        }
        .title {
          margin: 0;
          color: var(--primary-text-color);
          font-size: 18px;
          font-weight: 800;
          line-height: 1.2;
          letter-spacing: 0;
        }
        .subtitle {
          margin-top: 4px;
          color: var(--secondary-text-color);
          font-size: 12px;
          line-height: 1.3;
          font-weight: 600;
        }
        button {
          font: inherit;
          letter-spacing: 0;
        }
        .icon-button {
          flex: 0 0 auto;
          width: 34px;
          height: 34px;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--ha-card-background, var(--card-background-color, #fff));
          color: var(--primary-text-color);
          cursor: pointer;
          display: grid;
          place-items: center;
        }
        .icon-button[disabled],
        .command[disabled],
        .segment button[disabled] {
          opacity: 0.48;
          cursor: not-allowed;
        }
        .icon-button ha-icon {
          width: 19px;
          height: 19px;
        }
        .notice {
          margin: 0 0 12px;
          padding: 10px 12px;
          border-radius: 8px;
          color: var(--secondary-text-color);
          background: rgba(127, 127, 127, 0.08);
          border: 1px solid var(--divider-color);
          font-size: 12px;
          line-height: 1.35;
        }
        .notice.warn {
          color: var(--warning-color, #ff9800);
          background: rgba(255, 152, 0, 0.10);
          border-color: rgba(255, 152, 0, 0.32);
        }
        .notice.error {
          color: var(--error-color, #db4437);
          background: rgba(219, 68, 55, 0.10);
          border-color: rgba(219, 68, 55, 0.32);
        }
        .status {
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          gap: 12px;
          align-items: start;
          padding: 12px;
          border-radius: 8px;
          border: 1px solid var(--divider-color);
          background: rgba(127, 127, 127, 0.055);
          margin-bottom: 12px;
        }
        .loadpoint-name {
          margin: 0 0 5px;
          color: var(--primary-text-color);
          font-size: 16px;
          font-weight: 800;
          line-height: 1.2;
          letter-spacing: 0;
          overflow-wrap: anywhere;
        }
        .state-line {
          color: var(--secondary-text-color);
          font-size: 12px;
          line-height: 1.35;
          font-weight: 600;
        }
        .pill {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          min-width: 78px;
          padding: 6px 8px;
          border-radius: 999px;
          color: var(--primary-text-color);
          background: rgba(127, 127, 127, 0.10);
          border: 1px solid var(--divider-color);
          font-size: 11px;
          font-weight: 800;
          line-height: 1;
          text-transform: uppercase;
        }
        .pill.on {
          color: var(--success-color, #2e7d32);
          background: rgba(76, 175, 80, 0.12);
          border-color: rgba(76, 175, 80, 0.35);
        }
        .pill.warn {
          color: var(--warning-color, #ff9800);
          background: rgba(255, 152, 0, 0.12);
          border-color: rgba(255, 152, 0, 0.35);
        }
        .metrics {
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: 8px;
          margin: 10px 0 0;
        }
        .metric {
          min-width: 0;
          padding: 8px;
          border-radius: 8px;
          background: var(--ha-card-background, var(--card-background-color, #fff));
          border: 1px solid var(--divider-color);
        }
        .metric-label {
          color: var(--secondary-text-color);
          font-size: 10px;
          font-weight: 800;
          line-height: 1.1;
          text-transform: uppercase;
        }
        .metric-value {
          margin-top: 4px;
          color: var(--primary-text-color);
          font-size: 14px;
          font-weight: 800;
          line-height: 1.15;
          overflow-wrap: anywhere;
        }
        .controls {
          display: grid;
          gap: 10px;
          margin-bottom: 14px;
        }
        .control-row {
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 8px;
        }
        .control-field {
          min-width: 0;
        }
        label {
          display: block;
          margin: 0 0 4px;
          color: var(--secondary-text-color);
          font-size: 11px;
          font-weight: 800;
          line-height: 1.1;
          text-transform: uppercase;
        }
        select,
        input {
          box-sizing: border-box;
          width: 100%;
          min-height: 36px;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--ha-card-background, var(--card-background-color, #fff));
          color: var(--primary-text-color);
          padding: 7px 9px;
          font: inherit;
          font-size: 13px;
          letter-spacing: 0;
        }
        input[type="checkbox"] {
          width: 18px;
          min-height: 18px;
          height: 18px;
          padding: 0;
        }
        .segment {
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: 6px;
        }
        .segment button {
          min-height: 38px;
          padding: 7px 8px;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--ha-card-background, var(--card-background-color, #fff));
          color: var(--primary-text-color);
          cursor: pointer;
          font-size: 12px;
          font-weight: 800;
          line-height: 1.15;
          overflow-wrap: anywhere;
        }
        .segment button.active {
          color: var(--text-primary-color, #fff);
          background: var(--primary-color, #03a9f4);
          border-color: var(--primary-color, #03a9f4);
        }
        .commands {
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: 8px;
        }
        .command {
          min-height: 40px;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          background: var(--ha-card-background, var(--card-background-color, #fff));
          color: var(--primary-text-color);
          cursor: pointer;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 7px;
          padding: 8px 10px;
          font-size: 13px;
          font-weight: 800;
          line-height: 1.1;
        }
        .command ha-icon {
          width: 18px;
          height: 18px;
        }
        .command.primary {
          color: var(--text-primary-color, #fff);
          background: var(--primary-color, #03a9f4);
          border-color: var(--primary-color, #03a9f4);
        }
        .command.danger {
          color: var(--error-color, #db4437);
          background: rgba(219, 68, 55, 0.08);
          border-color: rgba(219, 68, 55, 0.32);
        }
        .section-title {
          margin: 16px 0 8px;
          color: var(--secondary-text-color);
          font-size: 12px;
          font-weight: 800;
          letter-spacing: 0;
          text-transform: uppercase;
        }
        .mode-grid {
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 10px;
        }
        .mode-card {
          min-width: 0;
          border: 1px solid var(--divider-color);
          border-radius: 8px;
          padding: 10px;
          background: rgba(127, 127, 127, 0.045);
        }
        .mode-head {
          display: grid;
          grid-template-columns: auto minmax(0, 1fr) auto;
          align-items: center;
          gap: 8px;
          margin-bottom: 9px;
        }
        .mode-head ha-icon {
          width: 19px;
          height: 19px;
          color: var(--primary-color, #03a9f4);
        }
        .mode-title {
          color: var(--primary-text-color);
          font-size: 13px;
          font-weight: 800;
          line-height: 1.15;
          overflow-wrap: anywhere;
        }
        .mode-fields {
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 8px;
        }
        .field.check {
          display: flex;
          align-items: center;
          gap: 8px;
          min-height: 36px;
        }
        .field.check label {
          margin: 0;
          text-transform: none;
          font-size: 12px;
        }
        .mode-actions {
          display: flex;
          justify-content: flex-end;
          margin-top: 9px;
        }
        .mode-actions .command {
          min-height: 34px;
          font-size: 12px;
          padding: 7px 9px;
        }
        .smart-list {
          display: grid;
          gap: 8px;
        }
        .smart-row {
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          align-items: center;
          gap: 8px;
          min-width: 0;
          padding: 8px;
          border-radius: 8px;
          border: 1px solid var(--divider-color);
          background: var(--ha-card-background, var(--card-background-color, #fff));
        }
        .smart-name {
          color: var(--primary-text-color);
          font-size: 13px;
          font-weight: 800;
          line-height: 1.2;
          overflow-wrap: anywhere;
        }
        .smart-meta {
          margin-top: 3px;
          color: var(--secondary-text-color);
          font-size: 11px;
          font-weight: 600;
          line-height: 1.25;
        }
        @media (max-width: 560px) {
          .status,
          .control-row,
          .commands,
          .mode-grid {
            grid-template-columns: 1fr;
          }
          .metrics {
            grid-template-columns: repeat(2, minmax(0, 1fr));
          }
          .segment {
            grid-template-columns: 1fr;
          }
        }
      </style>
      <ha-card>
        <div class="header">
          <div>
            <h2 class="title">EV Charging</h2>
            <div class="subtitle">${this._subtitle()}</div>
          </div>
          <button class="icon-button" data-action="refresh" title="Refresh EV status" aria-label="Refresh EV status" ${this._loading ? 'disabled' : ''}>
            <ha-icon icon="mdi:refresh"></ha-icon>
          </button>
        </div>
        ${!hasApi ? this._noticeHtml('error', 'Home Assistant API access is unavailable for this dashboard card.') : ''}
        ${this._error ? this._noticeHtml('error', this._error) : ''}
        ${this._notice ? this._noticeHtml(this._notice.type, this._notice.message) : ''}
        ${this._data?.modeErrors?.length ? this._noticeHtml('warn', this._data.modeErrors.join(' | ')) : ''}
        ${this._loading && !this._data ? this._noticeHtml('', 'Loading EV status...') : ''}
        ${loadpoints.length === 0 && this._data ? this._noticeHtml('warn', 'No EV loadpoints detected. Configure EV charging in PowerSync options or the mobile app.') : ''}
        ${selected ? this._statusHtml(selected, conflict) : ''}
        ${selected ? this._controlsHtml(loadpoints, selected, canStart, canStop) : ''}
        ${selected && !selected.connected ? this._noticeHtml('warn', 'EV is disconnected. Start is disabled until the charger reports a connected vehicle.') : ''}
        ${conflict ? this._noticeHtml('error', `${this._title(selected.owner_mode)} already owns this loadpoint.`) : ''}
        ${this._modeSectionsHtml()}
      </ha-card>
    `;

    this._attachEvents();
  }

  _subtitle() {
    if (!this._data?.fetchedAt) return 'Status and runtime controls';
    const date = new Date(this._data.fetchedAt);
    if (Number.isNaN(date.getTime())) return 'Status and runtime controls';
    return `Updated ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
  }

  _noticeHtml(type, message) {
    const cls = type ? ` ${type}` : '';
    return `<div class="notice${cls}">${this._escHtml(message)}</div>`;
  }

  _statusHtml(loadpoint, conflict) {
    const stateClass = loadpoint.actual_charging ? 'on' : (conflict || loadpoint.blocking_reason ? 'warn' : '');
    const stateText = loadpoint.actual_charging ? 'Charging' : (loadpoint.connected ? 'Connected' : 'Disconnected');
    const countdown = this._countdown(loadpoint.expires_at);
    const owner = this._title(loadpoint.owner_mode || loadpoint.owner || 'idle');
    const source = this._sourceLabel(loadpoint);
    return `
      <div class="status">
        <div>
          <h3 class="loadpoint-name">${this._escHtml(this._loadpointLabel(loadpoint))}</h3>
          <div class="state-line">${this._escHtml(owner)}${source ? ` | ${this._escHtml(source)}` : ''}${countdown ? ` | ${this._escHtml(countdown)}` : ''}</div>
          ${loadpoint.blocking_reason ? `<div class="state-line">${this._escHtml(loadpoint.blocking_reason)}</div>` : ''}
          <div class="metrics">
            ${this._metric('Power', this._kw(loadpoint.current_power_kw))}
            ${this._metric('Amps', this._amps(loadpoint.current_amps, loadpoint))}
            ${this._metric('SoC', this._soc(loadpoint.soc))}
            ${this._metric('Source', source || '--')}
          </div>
        </div>
        <div class="pill ${stateClass}">${this._escHtml(stateText)}</div>
      </div>
    `;
  }

  _controlsHtml(loadpoints, selected, canStart, canStop) {
    const options = loadpoints.map((lp, index) => `
      <option value="${this._escAttr(lp.loadpoint_id)}" ${lp.loadpoint_id === selected.loadpoint_id ? 'selected' : ''}>
        ${this._escHtml(this._loadpointLabel(lp, index))}
      </option>
    `).join('');
    return `
      <div class="controls">
        <div class="control-row">
          <div class="control-field">
            <label for="ev-loadpoint">Loadpoint</label>
            <select id="ev-loadpoint" data-control="loadpoint">${options}</select>
          </div>
          <div class="control-field">
            <label for="ev-duration">Duration</label>
            <select id="ev-duration" data-control="duration">
              ${[30, 60, 120, 240, 360].map((minutes) => `<option value="${minutes}" ${minutes === this._durationMinutes ? 'selected' : ''}>${minutes < 60 ? `${minutes} min` : `${minutes / 60} hr`}</option>`).join('')}
            </select>
          </div>
        </div>
        <div class="segment" role="group" aria-label="EV charging source policy">
          ${this._policyButton('solar_only', 'Solar Only')}
          ${this._policyButton('limited_grid_solar', 'Limited Grid + Solar')}
          ${this._policyButton('full_grid_solar', 'Full Grid + Solar')}
        </div>
        <div class="commands">
          <button class="command primary" data-action="start" ${canStart ? '' : 'disabled'}>
            <ha-icon icon="mdi:play"></ha-icon><span>Start</span>
          </button>
          <button class="command danger" data-action="stop" ${canStop ? '' : 'disabled'}>
            <ha-icon icon="mdi:stop"></ha-icon><span>Stop</span>
          </button>
          <button class="command" data-action="boost" ${selected && selected.connected && !this._savingKey ? '' : 'disabled'}>
            <ha-icon icon="mdi:flash"></ha-icon><span>Boost</span>
          </button>
        </div>
      </div>
    `;
  }

  _policyButton(policy, label) {
    const active = this._policy === policy ? 'active' : '';
    return `<button class="${active}" data-policy="${policy}" ${this._savingKey ? 'disabled' : ''}>${this._escHtml(label)}</button>`;
  }

  _modeSectionsHtml() {
    if (!this._data) return '';
    return `
      <div class="section-title">Modes</div>
      <div class="mode-grid">
        ${this._modeCard(
          'solar',
          'Solar Surplus',
          'mdi:solar-power',
          this._data.solarConfig,
          [
            { key: 'enabled', label: 'Enabled', type: 'checkbox' },
            { key: 'household_buffer_kw', label: 'Buffer kW', type: 'number', step: '0.1' },
            { key: 'home_battery_minimum', label: 'Home SOC %', type: 'number', step: '1' },
            { key: 'sustained_surplus_minutes', label: 'Start Delay', type: 'number', step: '1' },
            { key: 'stop_delay_minutes', label: 'Stop Delay', type: 'number', step: '1' },
          ],
        )}
        ${this._modeCard(
          'price',
          'Price Level',
          'mdi:cash-clock',
          this._data.priceSettings,
          [
            { key: 'enabled', label: 'Enabled', type: 'checkbox' },
            { key: 'recovery_soc', label: 'Recovery SOC %', type: 'number', step: '1' },
            { key: 'recovery_price_cents', label: 'Recovery c/kWh', type: 'number', step: '0.1' },
            { key: 'opportunity_price_cents', label: 'Opportunity c/kWh', type: 'number', step: '0.1' },
          ],
        )}
        ${this._modeCard(
          'scheduled',
          'Scheduled Charging',
          'mdi:calendar-clock',
          this._data.scheduledSettings,
          [
            { key: 'enabled', label: 'Enabled', type: 'checkbox' },
            { key: 'start_time', label: 'Start', type: 'time' },
            { key: 'end_time', label: 'End', type: 'time' },
            { key: 'max_price_cents', label: 'Max c/kWh', type: 'number', step: '0.1' },
          ],
        )}
        ${this._smartScheduleCard()}
      </div>
    `;
  }

  _modeCard(kind, title, icon, settings, fields) {
    const enabled = !!settings?.enabled;
    const saving = this._savingKey === `mode:${kind}`;
    return `
      <div class="mode-card">
        <div class="mode-head">
          <ha-icon icon="${icon}"></ha-icon>
          <div class="mode-title">${this._escHtml(title)}</div>
          <div class="pill ${enabled ? 'on' : ''}">${enabled ? 'On' : 'Off'}</div>
        </div>
        <div class="mode-fields">
          ${fields.map((field) => this._fieldHtml(kind, settings || {}, field)).join('')}
        </div>
        <div class="mode-actions">
          <button class="command" data-save-mode="${kind}" ${saving || this._savingKey ? 'disabled' : ''}>
            <ha-icon icon="mdi:content-save"></ha-icon><span>${saving ? 'Saving' : 'Save'}</span>
          </button>
        </div>
      </div>
    `;
  }

  _fieldHtml(kind, settings, field) {
    const value = settings[field.key];
    const dataKey = `${kind}:${field.key}`;
    if (field.type === 'checkbox') {
      return `
        <div class="field check">
          <input id="${this._escAttr(dataKey)}" type="checkbox" data-setting="${this._escAttr(dataKey)}" ${value ? 'checked' : ''}>
          <label for="${this._escAttr(dataKey)}">${this._escHtml(field.label)}</label>
        </div>
      `;
    }
    const inputType = field.type || 'text';
    const step = field.step ? ` step="${this._escAttr(field.step)}"` : '';
    return `
      <div class="field">
        <label for="${this._escAttr(dataKey)}">${this._escHtml(field.label)}</label>
        <input id="${this._escAttr(dataKey)}" type="${inputType}" data-setting="${this._escAttr(dataKey)}" value="${this._escAttr(value ?? '')}"${step}>
      </div>
    `;
  }

  _smartScheduleCard() {
    const settings = this._data?.autoStatus?.settings || {};
    const entries = Object.entries(settings);
    const rows = entries.length ? entries.map(([vehicleId, vehicle], index) => {
      const enabled = !!vehicle.enabled;
      const loadpoint = this._loadpoints().find((lp) => lp.loadpoint_id === vehicleId);
      const name = loadpoint ? this._loadpointLabel(loadpoint) : `Vehicle ${index + 1}`;
      const departure = vehicle.departure_time || Object.values(vehicle.departure_times || {})[0] || 'Not set';
      return `
        <div class="smart-row">
          <div>
            <div class="smart-name">${this._escHtml(name)}</div>
            <div class="smart-meta">Target ${this._escHtml(vehicle.target_soc ?? '--')}% | Departure ${this._escHtml(departure)}</div>
          </div>
          <button class="command" data-smart-toggle="${this._escAttr(vehicleId)}" data-enabled="${enabled ? 'false' : 'true'}" ${this._savingKey ? 'disabled' : ''}>
            <ha-icon icon="${enabled ? 'mdi:toggle-switch' : 'mdi:toggle-switch-off-outline'}"></ha-icon><span>${enabled ? 'On' : 'Off'}</span>
          </button>
        </div>
      `;
    }).join('') : '<div class="notice">No smart schedule vehicles configured.</div>';

    const anyEnabled = entries.some(([, vehicle]) => !!vehicle.enabled);
    return `
      <div class="mode-card">
        <div class="mode-head">
          <ha-icon icon="mdi:calendar-star"></ha-icon>
          <div class="mode-title">Smart Schedule</div>
          <div class="pill ${anyEnabled ? 'on' : ''}">${anyEnabled ? 'On' : 'Off'}</div>
        </div>
        <div class="smart-list">${rows}</div>
      </div>
    `;
  }

  _attachEvents() {
    this.shadowRoot.querySelector('[data-action="refresh"]')?.addEventListener('click', () => this._refresh());
    this.shadowRoot.querySelector('[data-control="loadpoint"]')?.addEventListener('change', (event) => {
      this._selectedLoadpointId = event.target.value;
      this._scheduleRender();
    });
    this.shadowRoot.querySelector('[data-control="duration"]')?.addEventListener('change', (event) => {
      this._durationMinutes = Number(event.target.value) || 60;
      this._scheduleRender();
    });
    this.shadowRoot.querySelectorAll('[data-policy]').forEach((button) => {
      button.addEventListener('click', () => {
        this._policy = button.dataset.policy;
        this._scheduleRender();
      });
    });
    this.shadowRoot.querySelector('[data-action="start"]')?.addEventListener('click', () => this._startPolicy());
    this.shadowRoot.querySelector('[data-action="stop"]')?.addEventListener('click', () => this._stopLoadpoint());
    this.shadowRoot.querySelector('[data-action="boost"]')?.addEventListener('click', () => this._boostLoadpoint());
    this.shadowRoot.querySelectorAll('[data-save-mode]').forEach((button) => {
      button.addEventListener('click', () => this._saveMode(button.dataset.saveMode));
    });
    this.shadowRoot.querySelectorAll('[data-smart-toggle]').forEach((button) => {
      button.addEventListener('click', () => this._toggleSmartSchedule(button.dataset.smartToggle, button.dataset.enabled === 'true'));
    });
  }

  _refresh() {
    this._notice = null;
    this._lastFetch = 0;
    this._maybeLoadData(true);
  }

  async _startPolicy() {
    const loadpoint = this._selectedLoadpoint();
    if (!loadpoint) return;
    await this._runCommand(
      'command:start',
      this._commandPath(loadpoint.loadpoint_id),
      {
        command: 'start_policy_charging',
        policy: this._policy,
        duration_minutes: this._durationMinutes,
      },
      'Charging started',
    );
  }

  async _stopLoadpoint() {
    const loadpoint = this._selectedLoadpoint();
    if (!loadpoint) return;
    await this._runCommand(
      'command:stop',
      this._commandPath(loadpoint.loadpoint_id),
      { command: 'stop_charging' },
      'Charging stopped',
    );
  }

  async _boostLoadpoint() {
    const loadpoint = this._selectedLoadpoint();
    if (!loadpoint) return;
    await this._runCommand(
      'command:boost',
      EV_PANEL_PATHS.boost,
      {
        vehicle_id: loadpoint.loadpoint_id,
        duration_minutes: this._durationMinutes,
      },
      'Boost started',
    );
  }

  async _saveMode(kind) {
    const paths = {
      solar: EV_PANEL_PATHS.solar,
      price: EV_PANEL_PATHS.price,
      scheduled: EV_PANEL_PATHS.scheduled,
    };
    const path = paths[kind];
    if (!path) return;
    const payload = this._collectModePayload(kind);
    await this._runCommand(`mode:${kind}`, path, payload, 'Settings saved');
  }

  async _toggleSmartSchedule(vehicleId, enabled) {
    await this._runCommand(
      `smart:${vehicleId}`,
      EV_PANEL_PATHS.autoToggle,
      { vehicle_id: vehicleId, enabled },
      'Smart schedule updated',
    );
  }

  _collectModePayload(kind) {
    const payload = {};
    this.shadowRoot.querySelectorAll(`[data-setting^="${kind}:"]`).forEach((input) => {
      const key = input.dataset.setting.split(':')[1];
      if (!key) return;
      if (input.type === 'checkbox') {
        payload[key] = input.checked;
      } else if (input.type === 'number') {
        const value = Number(input.value);
        if (Number.isFinite(value)) payload[key] = value;
      } else {
        payload[key] = input.value;
      }
    });
    return payload;
  }

  async _runCommand(savingKey, path, payload, successMessage) {
    if (!this._hass || typeof this._hass.callApi !== 'function') return;
    this._savingKey = savingKey;
    this._notice = null;
    this._error = null;
    this._scheduleRender();
    try {
      const response = await this._hass.callApi('POST', path, payload);
      if (response?.success === false) {
        throw new Error(response.error || 'Command failed');
      }
      this._notice = { type: '', message: response?.data?.message || response?.message || successMessage };
      this._lastFetch = 0;
      EV_PANEL_CACHE.delete(this._cacheKey());
      await this._loadData(this._cacheKey());
    } catch (err) {
      this._notice = { type: 'error', message: err?.message || 'Command failed' };
    } finally {
      this._savingKey = null;
      this._scheduleRender();
    }
  }

  _commandPath(loadpointId) {
    return `power_sync/ev/vehicles/${encodeURIComponent(loadpointId)}/command`;
  }

  _metric(label, value) {
    return `
      <div class="metric">
        <div class="metric-label">${this._escHtml(label)}</div>
        <div class="metric-value">${this._escHtml(value)}</div>
      </div>
    `;
  }

  _loadpointLabel(loadpoint, index = null) {
    const name = String(loadpoint?.vehicle_name || loadpoint?.name || '').trim();
    if (name) return name;
    const loadpoints = this._loadpoints();
    const resolvedIndex = index ?? loadpoints.findIndex((candidate) => candidate === loadpoint);
    if (resolvedIndex >= 0) return `Loadpoint ${resolvedIndex + 1}`;
    return 'EV Loadpoint';
  }

  _sourceLabel(loadpoint) {
    const mode = loadpoint?.source_mode;
    if (mode === 'solar_only') return 'Solar only';
    if (mode === 'limited_grid_solar') return 'Limited grid + solar';
    if (mode === 'full_grid_solar' || mode === 'grid_allowed') return 'Full grid + solar';
    if (loadpoint?.source === 'solar') return 'Solar';
    if (loadpoint?.source === 'grid') return 'Grid';
    return this._title(loadpoint?.source || mode || '');
  }

  _countdown(value) {
    if (!value) return '';
    const expires = new Date(value);
    if (Number.isNaN(expires.getTime())) return '';
    const remainingMs = expires.getTime() - Date.now();
    if (remainingMs <= 0) return 'Ending now';
    const totalMinutes = Math.ceil(remainingMs / 60000);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours > 0) return `${hours}h ${minutes}m remaining`;
    return `${minutes}m remaining`;
  }

  _kw(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `${number.toFixed(number >= 10 ? 1 : 2)} kW` : '--';
  }

  _amps(value, loadpoint = null) {
    const number = Number(value);
    if (Number.isFinite(number) && number > 0) return `${Math.round(number)} A`;
    const power = Number(loadpoint?.current_power_kw);
    if (Number.isFinite(power) && power > 0.05) return '--';
    return Number.isFinite(number) ? '0 A' : '--';
  }

  _soc(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `${Math.round(number)}%` : '--';
  }

  _title(value) {
    const text = String(value || '').replace(/_/g, ' ').trim();
    return text ? text.replace(/\b\w/g, c => c.toUpperCase()) : '';
  }

  _escHtml(value) {
    return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  _escAttr(value) {
    return this._escHtml(value).replace(/'/g, '&#39;');
  }
}

if (!customElements.get('power-sync-ev-panel')) {
  customElements.define('power-sync-ev-panel', PowerSyncEVPanel);
}

// ─── PowerSyncLayout Custom Element ─────────────────────────────
// Viewport-fitting grid layout: 3 columns, fills available height.
// Chart cards flex to fill remaining space; control cards stay natural size.
// Scrolls only when content genuinely exceeds viewport.

class PowerSyncLayout extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._cards = [];
    this._items = [];
    this._lanes = [];
    this._built = false;
    this._layoutQueued = false;
    this._resizeObserver = null;
    this._customizing = false;
    this._dragItem = null;
    this._pointerDrag = null;
    this._dragPlaceholder = null;
    this._showingHidden = false;
    this._storageKey = 'power-sync-dashboard-layout-v2';
    this._hiddenStorageKey = 'power-sync-dashboard-hidden-v1';
    this._appliedLayoutSignature = '';
    this._lastLayoutWidth = 0;
    this._lastLayoutColumnCount = 0;
  }

  setConfig(config) {
    this._config = config;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._buildLayout();
    for (const c of this._cards) c.hass = hass;
  }

  disconnectedCallback() {
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
      this._resizeObserver = null;
    }
  }

  _scheduleLayout() {
    if (!this._built || this._layoutQueued) return;
    this._layoutQueued = true;
    requestAnimationFrame(() => {
      this._layoutQueued = false;
      this._balanceLayout();
    });
  }

  _columnCount() {
    const width = this.getBoundingClientRect().width || window.innerWidth || 0;
    return this._columnCountForWidth(width);
  }

  _columnCountForWidth(width) {
    const portrait = window.matchMedia?.('(orientation: portrait)')?.matches;
    if (width < 760 || (portrait && width < 1040)) return 1;
    if (width < 1280) return 2;
    return 3;
  }

  _scheduleLayoutForResize(entry) {
    const box = Array.isArray(entry?.contentBoxSize)
      ? entry.contentBoxSize[0]
      : entry?.contentBoxSize;
    const width = box?.inlineSize || entry?.contentRect?.width || this.getBoundingClientRect().width || window.innerWidth || 0;
    const columnCount = this._columnCountForWidth(width);
    const widthDelta = Math.abs(width - this._lastLayoutWidth);
    if (
      this._lastLayoutColumnCount &&
      columnCount === this._lastLayoutColumnCount &&
      widthDelta < 80
    ) {
      return;
    }
    this._scheduleLayout();
  }

  _flattenCards() {
    const columns = this._config?.columns || [];
    const ordered = columns.length === 3 ? [columns[1], columns[0], columns[2]] : columns;
    const cards = ordered.flatMap(column => column || []);
    if (!cards.some(card => card?.type === 'custom:power-sync-ev-panel')) {
      cards.push(_evPanel());
    }
    return cards;
  }

  _cardKeyParts(cardConfig) {
    const parts = [
      cardConfig.type,
      cardConfig.title,
      cardConfig.entity,
      cardConfig.name,
      cardConfig.card?.type,
      cardConfig.cards?.map(card => card.title || card.entity || card.type).join('|'),
    ].filter(Boolean);
    return parts;
  }

  _legacyCardKey(cardConfig, index) {
    return `${index}:${this._cardKeyParts(cardConfig).join(':')}`;
  }

  _cardKey(cardConfig, occurrence) {
    const base = this._cardKeyParts(cardConfig).join(':') || 'card';
    return occurrence > 0 ? `${base}#${occurrence}` : base;
  }

  _loadLayouts() {
    try {
      const saved = JSON.parse(localStorage.getItem(this._storageKey) || '{}');
      return saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : {};
    } catch (_) {
      return {};
    }
  }

  _loadHiddenKeys() {
    try {
      const saved = JSON.parse(localStorage.getItem(this._hiddenStorageKey) || '[]');
      return new Set(Array.isArray(saved) ? saved.filter(Boolean) : []);
    } catch (_) {
      return new Set();
    }
  }

  _saveHiddenKeys(keys) {
    try {
      const unique = Array.from(new Set(keys)).filter(Boolean);
      if (unique.length === 0) {
        localStorage.removeItem(this._hiddenStorageKey);
      } else {
        localStorage.setItem(this._hiddenStorageKey, JSON.stringify(unique));
      }
    } catch (_) {}
  }

  _visibleItems() {
    return this._items.filter(item => item.dataset.hidden !== 'true');
  }

  _hiddenItems() {
    return this._items.filter(item => item.dataset.hidden === 'true');
  }

  _layoutItems() {
    return this._items.filter(item => item.dataset.hidden !== 'true' || this._showingHidden);
  }

  _syncHiddenKeys() {
    const savedKeys = this._loadHiddenKeys();
    const currentKeys = new Set(this._items.map(item => item.dataset.key));
    const legacyToCurrent = new Map(this._items
      .map(item => [item.dataset.legacyKey, item.dataset.key])
      .filter(([legacyKey]) => legacyKey));
    const normalized = new Set();

    for (const savedKey of savedKeys) {
      if (currentKeys.has(savedKey)) {
        normalized.add(savedKey);
      } else {
        const migratedKey = legacyToCurrent.get(savedKey);
        if (migratedKey) normalized.add(migratedKey);
      }
    }

    for (const item of this._items) {
      if (item.dataset.hidden === 'true') normalized.add(item.dataset.key);
    }
    for (const item of this._items) {
      item.dataset.hidden = normalized.has(item.dataset.key) ? 'true' : 'false';
    }
    this._saveHiddenKeys(Array.from(normalized));
  }

  _updateToolbarState() {
    const toolbar = this.shadowRoot.querySelector('.toolbar');
    if (!toolbar) return;
    toolbar.classList.toggle('active', this._customizing);
    const toggle = toolbar.querySelector('.toggle');
    if (toggle) toggle.textContent = this._customizing ? 'Done' : 'Customize layout';

    const hiddenCount = this._hiddenItems().length;
    if (hiddenCount === 0) this._showingHidden = false;
    const restoreHidden = toolbar.querySelector('.restore-hidden');
    if (restoreHidden) {
      restoreHidden.hidden = hiddenCount === 0;
      restoreHidden.textContent = this._showingHidden
        ? `Hide hidden (${hiddenCount})`
        : `Show hidden (${hiddenCount})`;
      restoreHidden.setAttribute('aria-pressed', this._showingHidden ? 'true' : 'false');
    }

    const hideDisabled = this._visibleItems().length <= 1;
    for (const item of this._items) {
      const isHidden = item.dataset.hidden === 'true';
      item.classList.toggle('hidden-preview', isHidden && this._showingHidden);
      const hideSurface = item.querySelector('.hide-surface');
      if (hideSurface) {
        hideSurface.disabled = !isHidden && hideDisabled;
        hideSurface.textContent = isHidden ? 'Unhide' : 'Hide';
        hideSurface.setAttribute('aria-label', isHidden ? 'Unhide dashboard card' : 'Hide dashboard card');
      }
    }
  }

  _saveOrder() {
    try {
      const count = String(this._lanes.length || this._columnCount());
      const layouts = this._loadLayouts();
      layouts[count] = this._lanes.map((lane) => Array.from(lane.children)
        .filter(item => item.classList.contains('item'))
        .map(item => item.dataset.key));
      localStorage.setItem(this._storageKey, JSON.stringify(layouts));
      this._appliedLayoutSignature = this._layoutSignature(layouts[count]);
    } catch (_) {}
  }

  _setCustomizing(enabled) {
    const wasShowingHidden = this._showingHidden;
    this._customizing = enabled;
    if (!enabled) {
      this._showingHidden = false;
      for (const item of this._hiddenItems()) item.remove();
      if (wasShowingHidden) this._appliedLayoutSignature = '';
    }
    for (const item of this._items) {
      item.draggable = false;
      item.classList.toggle('customizing', enabled);
      item.classList.toggle('hidden-preview', item.dataset.hidden === 'true' && this._showingHidden);
      const dragSurface = item.querySelector('.drag-surface');
      if (dragSurface) dragSurface.draggable = false;
    }
    this._updateToolbarState();
    if (wasShowingHidden && !enabled) this._scheduleLayout();
  }

  _resetOrder() {
    this._cancelActiveDrag();
    try { localStorage.removeItem(this._storageKey); } catch (_) {}
    this._saveHiddenKeys([]);
    for (const item of this._items) item.dataset.hidden = 'false';
    this._items.sort((a, b) => Number(a.dataset.defaultIndex) - Number(b.dataset.defaultIndex));
    if (this._lanes.length) {
      this._rebuildLanes(this._lanes.length);
    }
    this._appliedLayoutSignature = '';
    this._updateToolbarState();
    this._scheduleLayout();
  }

  _hideItem(item) {
    if (item.dataset.hidden === 'true' || this._visibleItems().length <= 1) return;
    this._cancelActiveDrag();
    item.dataset.hidden = 'true';
    if (!this._showingHidden) item.remove();
    this._saveHiddenKeys(this._hiddenItems().map(hiddenItem => hiddenItem.dataset.key));
    this._appliedLayoutSignature = '';
    this._updateToolbarState();
    this._scheduleLayout();
  }

  _showHiddenItems() {
    this._cancelActiveDrag();
    if (this._hiddenItems().length === 0) return;
    this._showingHidden = !this._showingHidden;
    if (this._showingHidden) this._customizing = true;
    if (!this._showingHidden) {
      for (const item of this._hiddenItems()) item.remove();
    }
    for (const item of this._items) {
      item.classList.toggle('customizing', this._customizing);
      item.classList.toggle('hidden-preview', item.dataset.hidden === 'true' && this._showingHidden);
    }
    this._appliedLayoutSignature = '';
    this._updateToolbarState();
    this._scheduleLayout();
  }

  _unhideItem(item) {
    if (item.dataset.hidden !== 'true') return;
    this._cancelActiveDrag();
    item.dataset.hidden = 'false';
    this._saveHiddenKeys(this._hiddenItems().map(hiddenItem => hiddenItem.dataset.key));
    this._appliedLayoutSignature = '';
    this._updateToolbarState();
    this._scheduleLayout();
  }

  _toggleItemHidden(item) {
    if (item.dataset.hidden === 'true') {
      this._unhideItem(item);
    } else {
      this._hideItem(item);
    }
  }

  _renderedItemCount() {
    return this._lanes.reduce((sum, lane) => (
      sum + Array.from(lane.children).filter(child => child.classList.contains('item')).length
    ), 0);
  }

  _laneAtPoint(x, y) {
    return this._lanes.find(lane => {
      const rect = lane.getBoundingClientRect();
      return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
    });
  }

  _laneForDragPoint(x, y) {
    const directLane = this._laneAtPoint(x, y);
    if (directLane) return directLane;

    const grid = this.shadowRoot.querySelector('.grid');
    const gridRect = grid?.getBoundingClientRect();
    if (!gridRect || x < gridRect.left || x > gridRect.right || y < gridRect.top - 80) return null;

    let nearestLane = null;
    let nearestDistance = Infinity;
    for (const lane of this._lanes) {
      const rect = lane.getBoundingClientRect();
      const centerX = rect.left + rect.width / 2;
      const distance = Math.abs(x - centerX);
      if (distance < nearestDistance) {
        nearestDistance = distance;
        nearestLane = lane;
      }
    }
    return nearestLane;
  }

  _clearDragStyles(item) {
    item.classList.remove('dragging');
    item.style.position = '';
    item.style.left = '';
    item.style.top = '';
    item.style.width = '';
    item.style.height = '';
    item.style.zIndex = '';
    item.style.pointerEvents = '';
    item.style.transform = '';
  }

  _positionDragItem(event) {
    if (!this._dragItem || !this._pointerDrag?.active) return;
    const x = event.clientX - this._pointerDrag.offsetX;
    const y = event.clientY - this._pointerDrag.offsetY;
    this._dragItem.style.transform = `translate3d(${Math.round(x)}px, ${Math.round(y)}px, 0)`;
  }

  _beginPointerReorder(item, event) {
    const rect = item.getBoundingClientRect();
    const placeholder = document.createElement('div');
    placeholder.className = 'drag-placeholder';
    placeholder.style.height = `${rect.height}px`;

    item.parentElement?.insertBefore(placeholder, item.nextSibling);
    this._dragPlaceholder = placeholder;
    this._dragItem = item;

    this._pointerDrag.active = true;
    this._pointerDrag.offsetX = event.clientX - rect.left;
    this._pointerDrag.offsetY = event.clientY - rect.top;

    item.classList.add('dragging');
    item.style.position = 'fixed';
    item.style.left = '0';
    item.style.top = '0';
    item.style.width = `${rect.width}px`;
    item.style.height = `${rect.height}px`;
    item.style.zIndex = '999';
    item.style.pointerEvents = 'none';

    this._positionDragItem(event);
    this._placePlaceholder(event.clientX, event.clientY);
  }

  _placePlaceholder(x, y) {
    if (!this._dragPlaceholder) return;
    const lane = this._laneForDragPoint(x, y);
    if (!lane) return;

    const candidates = Array.from(lane.children).filter(child => (
      child !== this._dragItem &&
      child !== this._dragPlaceholder &&
      child.classList.contains('item')
    ));
    const before = candidates.find((child) => {
      const rect = child.getBoundingClientRect();
      return y < rect.top + rect.height / 2;
    });
    lane.insertBefore(this._dragPlaceholder, before || null);
  }

  _dropPointerReorder(item) {
    if (!this._dragPlaceholder) return;
    this._dragPlaceholder.parentElement?.insertBefore(item, this._dragPlaceholder);
    this._dragPlaceholder.remove();
    this._dragPlaceholder = null;
    this._clearDragStyles(item);
    this._dragItem = null;
    this._saveOrder();
  }

  _cancelActiveDrag() {
    if (!this._dragItem) {
      this._dragPlaceholder?.remove();
      this._dragPlaceholder = null;
      this._pointerDrag = null;
      return;
    }

    if (this._dragPlaceholder) {
      this._dragPlaceholder.parentElement?.insertBefore(this._dragItem, this._dragPlaceholder);
      this._dragPlaceholder.remove();
      this._dragPlaceholder = null;
    }
    this._clearDragStyles(this._dragItem);
    this._dragItem = null;
    this._pointerDrag = null;
  }

  _startPointerDrag(item, event, captureTarget = item) {
    if (!this._customizing || event.button > 0) return;
    event.preventDefault();
    this._pointerDrag = {
      active: false,
      item,
      startX: event.clientX,
      startY: event.clientY,
    };
    captureTarget.setPointerCapture?.(event.pointerId);
  }

  _updatePointerDrag(item, event) {
    if (!this._pointerDrag || this._pointerDrag.item !== item) return;
    const distance = Math.hypot(event.clientX - this._pointerDrag.startX, event.clientY - this._pointerDrag.startY);
    if (!this._pointerDrag.active && distance < 8) return;
    if (!this._pointerDrag.active) {
      this._beginPointerReorder(item, event);
      return;
    }
    event.preventDefault();
    this._positionDragItem(event);
    this._placePlaceholder(event.clientX, event.clientY);
  }

  _finishPointerDrag(item) {
    if (!this._pointerDrag || this._pointerDrag.item !== item) return;
    if (this._pointerDrag.active) {
      this._dropPointerReorder(item);
    } else {
      this._clearDragStyles(item);
    }
    this._pointerDrag = null;
  }

  _cancelPointerDrag(item) {
    if (!this._pointerDrag || this._pointerDrag.item !== item) return;
    this._cancelActiveDrag();
  }

  _savedLayout(count) {
    const layoutKey = String(count);
    const layout = this._loadLayouts()[layoutKey];
    if (!Array.isArray(layout) || layout.length !== count) return null;
    if (!layout.every(lane => Array.isArray(lane))) return null;

    const visibleItems = this._layoutItems();
    const currentKeys = new Set(visibleItems.map(item => item.dataset.key));
    const legacyToCurrent = new Map(visibleItems
      .map(item => [item.dataset.legacyKey, item.dataset.key])
      .filter(([legacyKey]) => legacyKey));
    const normalized = Array.from({ length: count }, () => []);
    const placed = new Set();
    let changed = false;

    layout.forEach((lane, laneIndex) => {
      lane.forEach((savedKey) => {
        let key = savedKey;
        if (!currentKeys.has(key)) {
          const migratedKey = legacyToCurrent.get(savedKey);
          if (!migratedKey) {
            changed = true;
            return;
          }
          key = migratedKey;
          changed = true;
        }
        if (placed.has(key)) {
          changed = true;
          return;
        }
        normalized[laneIndex].push(key);
        placed.add(key);
      });
    });

    const missingItems = visibleItems
      .filter(item => !placed.has(item.dataset.key))
      .sort((a, b) => Number(a.dataset.defaultIndex) - Number(b.dataset.defaultIndex));
    if (missingItems.length > 0) changed = true;
    for (const item of missingItems) {
      const laneIndex = normalized
        .map((lane, index) => ({ index, length: lane.length }))
        .sort((a, b) => a.length - b.length || a.index - b.index)[0].index;
      normalized[laneIndex].push(item.dataset.key);
      placed.add(item.dataset.key);
    }

    if (placed.size === currentKeys.size && !changed) return normalized;
    if (placed.size === 0) return null;

    try {
      const layouts = this._loadLayouts();
      layouts[layoutKey] = normalized;
      localStorage.setItem(this._storageKey, JSON.stringify(layouts));
    } catch (_) {}
    this._appliedLayoutSignature = '';
    return normalized;
  }

  _layoutSignature(layout) {
    return JSON.stringify(layout || []);
  }

  _currentLaneLayout() {
    return this._lanes.map((lane) => Array.from(lane.children)
      .filter(item => item.classList.contains('item'))
      .map(item => item.dataset.key));
  }

  _applyLaneLayout(layout) {
    const signature = this._layoutSignature(layout);
    if (
      this._appliedLayoutSignature === signature &&
      this._layoutSignature(this._currentLaneLayout()) === signature
    ) {
      return;
    }

    const byKey = new Map(this._layoutItems().map(item => [item.dataset.key, item]));
    const placed = new Set();
    layout.forEach((keys, laneIndex) => {
      const lane = this._lanes[laneIndex];
      if (!lane) return;
      keys.forEach((key) => {
        const item = byKey.get(key);
        if (!item || placed.has(item)) return;
        lane.appendChild(item);
        placed.add(item);
      });
    });

    const heights = this._lanes.map(lane => lane.getBoundingClientRect().height || 0);
    this._layoutItems()
      .filter(item => !placed.has(item))
      .sort((a, b) => Number(a.dataset.defaultIndex) - Number(b.dataset.defaultIndex))
      .forEach((item) => {
        const laneIndex = heights.indexOf(Math.min(...heights));
        this._lanes[laneIndex].appendChild(item);
        heights[laneIndex] += item.getBoundingClientRect().height || item.scrollHeight || 180;
      });
    this._appliedLayoutSignature = this._layoutSignature(this._currentLaneLayout());
  }

  _balanceLayout() {
    const visibleItems = this._layoutItems();
    if (!visibleItems.length) {
      const grid = this.shadowRoot.querySelector('.grid');
      if (grid) {
        grid.style.setProperty('--ps-lane-count', '1');
        grid.innerHTML = '<div class="empty">All dashboard sections are hidden.</div>';
      }
      this._lanes = [];
      return;
    }

    const count = Math.min(this._columnCount(), visibleItems.length);
    this._lastLayoutColumnCount = count;
    this._lastLayoutWidth = this.getBoundingClientRect().width || window.innerWidth || 0;
    if (this._lanes.length !== count) {
      this._rebuildLanes(count);
    }

    const renderedItemCount = this._renderedItemCount();
    if (this._customizing && renderedItemCount === visibleItems.length) return;

    const savedLayout = this._savedLayout(count);
    if (savedLayout) {
      this._applyLaneLayout(savedLayout);
      return;
    }

    const heights = new Array(count).fill(0);
    const sortedItems = [...visibleItems].sort((a, b) => Number(a.dataset.defaultIndex) - Number(b.dataset.defaultIndex));
    for (const item of sortedItems) {
      const laneIndex = heights.indexOf(Math.min(...heights));
      this._lanes[laneIndex].appendChild(item);
      heights[laneIndex] += item.getBoundingClientRect().height || item.scrollHeight || 180;
    }
  }

  _rebuildLanes(count) {
    const grid = this.shadowRoot.querySelector('.grid');
    if (!grid) return;
    grid.innerHTML = '';
    grid.style.setProperty('--ps-lane-count', String(count));
    this._lanes = [];
    this._appliedLayoutSignature = '';
    for (let i = 0; i < count; i++) {
      const lane = document.createElement('div');
      lane.className = 'lane';
      this._lanes.push(lane);
      grid.appendChild(lane);
    }
  }

  async _buildLayout() {
    if (this._built) return;
    this._built = true;

    const root = this.shadowRoot;
    const style = document.createElement('style');
    style.textContent = `
      :host {
        display: block;
        width: 100%;
        --ps-dashboard-gap: clamp(8px, 1.2vw, 14px);
      }
      .grid {
        display: grid;
        grid-template-columns: repeat(var(--ps-lane-count, 3), minmax(0, 1fr));
        gap: var(--ps-dashboard-gap);
        padding: var(--ps-dashboard-gap);
        align-items: start;
        box-sizing: border-box;
        width: 100%;
        max-width: 1900px;
        margin: 0 auto;
      }
      .lane {
        display: flex;
        flex-direction: column;
        gap: var(--ps-dashboard-gap);
        min-width: 0;
      }
      .item,
      .item > * {
        min-width: 0;
      }
      .toolbar {
        display: flex;
        justify-content: flex-end;
        gap: 8px;
        width: 100%;
        max-width: 1900px;
        margin: 0 auto;
        padding: var(--ps-dashboard-gap) var(--ps-dashboard-gap) 0;
        box-sizing: border-box;
      }
      .toolbar button {
        border: 1px solid var(--divider-color, rgba(255,255,255,0.16));
        border-radius: 8px;
        background: var(--ha-card-background, var(--card-background-color, #1c1c1c));
        color: var(--primary-text-color, #fff);
        font: inherit;
        font-size: 12px;
        font-weight: 600;
        line-height: 1;
        padding: 8px 10px;
        cursor: pointer;
      }
      .toolbar.active .toggle {
        border-color: var(--primary-color, #03a9f4);
        color: var(--primary-color, #03a9f4);
      }
      .item {
        position: relative;
      }
      .item.customizing {
        cursor: default;
        outline: 1px dashed color-mix(in srgb, var(--primary-color, #03a9f4) 60%, transparent);
        outline-offset: 3px;
        border-radius: 10px;
      }
      .drag-surface {
        display: none;
        position: absolute;
        top: 8px;
        right: 8px;
        min-width: 44px;
        min-height: 32px;
        padding: 0 9px;
        appearance: none;
        border-radius: 999px;
        border: 0;
        background: color-mix(in srgb, var(--primary-color, #03a9f4) 85%, black);
        color: white;
        font: inherit;
        font-size: 10px;
        font-weight: 700;
        line-height: 32px;
        text-align: center;
        z-index: 20;
        cursor: grab;
        touch-action: none;
        user-select: none;
      }
      .hide-surface {
        display: none;
        position: absolute;
        top: 8px;
        right: 62px;
        min-width: 44px;
        min-height: 32px;
        padding: 0 9px;
        appearance: none;
        border-radius: 999px;
        border: 0;
        background: color-mix(in srgb, var(--error-color, #db4437) 82%, black);
        color: white;
        font: inherit;
        font-size: 10px;
        font-weight: 700;
        line-height: 32px;
        text-align: center;
        z-index: 20;
        cursor: pointer;
        touch-action: manipulation;
        user-select: none;
      }
      .item.customizing .drag-surface {
        display: block;
      }
      .item.customizing .hide-surface {
        display: block;
      }
      .hide-surface:disabled {
        opacity: 0.45;
        cursor: not-allowed;
      }
      .item.hidden-preview {
        opacity: 0.58;
      }
      .item.hidden-preview .hide-surface {
        background: color-mix(in srgb, var(--primary-color, #03a9f4) 85%, black);
      }
      .item.dragging {
        opacity: 0.55;
        cursor: grabbing;
        touch-action: none;
        margin: 0;
        will-change: transform;
      }
      .item.dragging .drag-surface {
        cursor: grabbing;
      }
      .drag-placeholder {
        min-height: 84px;
        border: 2px dashed color-mix(in srgb, var(--primary-color, #03a9f4) 68%, transparent);
        border-radius: 10px;
        background: color-mix(in srgb, var(--primary-color, #03a9f4) 10%, transparent);
        box-sizing: border-box;
      }
      .empty {
        padding: 24px;
        color: var(--secondary-text-color, #888);
        text-align: center;
      }
      @media (max-width: 760px) {
        .grid {
          padding: 6px;
        }
      }
    `;
    root.appendChild(style);

    const toolbar = document.createElement('div');
    toolbar.className = 'toolbar';
    toolbar.innerHTML = `
      <button class="toggle" type="button">Customize layout</button>
      <button class="restore-hidden" type="button" hidden>Show hidden (0)</button>
      <button class="reset" type="button">Reset layout</button>
    `;
    toolbar.querySelector('.toggle').addEventListener('click', () => this._setCustomizing(!this._customizing));
    toolbar.querySelector('.restore-hidden').addEventListener('click', () => this._showHiddenItems());
    toolbar.querySelector('.reset').addEventListener('click', () => this._resetOrder());
    root.appendChild(toolbar);

    const grid = document.createElement('div');
    grid.className = 'grid';
    root.appendChild(grid);

    if ('ResizeObserver' in window) {
      this._resizeObserver = new ResizeObserver((entries) => this._scheduleLayoutForResize(entries?.[0]));
      this._resizeObserver.observe(this);
    }

    let helpers;
    try { helpers = await window.loadCardHelpers(); } catch (_) {}

    const keyOccurrences = new Map();
    const hiddenKeys = this._loadHiddenKeys();
    for (const [index, cardConfig] of this._flattenCards().entries()) {
      const baseKey = this._cardKeyParts(cardConfig).join(':') || 'card';
      const occurrence = keyOccurrences.get(baseKey) || 0;
      keyOccurrences.set(baseKey, occurrence + 1);
      const cardKey = this._cardKey(cardConfig, occurrence);
      const legacyKey = this._legacyCardKey(cardConfig, index);

      const item = document.createElement('div');
      item.className = 'item';
      item.dataset.defaultIndex = String(index);
      item.dataset.key = cardKey;
      item.dataset.legacyKey = legacyKey;
      item.dataset.hidden = hiddenKeys.has(cardKey) || hiddenKeys.has(legacyKey) ? 'true' : 'false';
      item.addEventListener('dragstart', (event) => event.preventDefault());

      const dragSurface = document.createElement('button');
      dragSurface.className = 'drag-surface';
      dragSurface.type = 'button';
      dragSurface.textContent = 'Drag';
      dragSurface.setAttribute('aria-label', 'Drag dashboard card');
      dragSurface.addEventListener('dragstart', (event) => {
        event.stopPropagation();
        event.preventDefault();
      });
      dragSurface.addEventListener('click', (event) => {
        event.stopPropagation();
        event.preventDefault();
      });
      dragSurface.addEventListener('pointerdown', (event) => {
        event.stopPropagation();
        this._startPointerDrag(item, event, dragSurface);
      });
      dragSurface.addEventListener('pointermove', (event) => {
        event.stopPropagation();
        this._updatePointerDrag(item, event);
      });
      dragSurface.addEventListener('pointerup', (event) => {
        event.stopPropagation();
        this._finishPointerDrag(item);
      });
      dragSurface.addEventListener('pointercancel', (event) => {
        event.stopPropagation();
        this._cancelPointerDrag(item);
      });

      const hideSurface = document.createElement('button');
      hideSurface.className = 'hide-surface';
      hideSurface.type = 'button';
      hideSurface.textContent = 'Hide';
      hideSurface.setAttribute('aria-label', 'Hide dashboard card');
      hideSurface.addEventListener('click', (event) => {
        event.stopPropagation();
        event.preventDefault();
        this._toggleItemHidden(item);
      });

      let card;
      try {
        card = helpers
          ? await helpers.createCardElement(cardConfig)
          : document.createElement(cardConfig.type);
        if (!helpers && card.setConfig) card.setConfig(cardConfig);
      } catch (err) {
        card = document.createElement('hui-error-card');
        try { card.setConfig({ type: 'error', error: err.message, origConfig: cardConfig }); } catch (_) {}
      }

      if (this._hass) card.hass = this._hass;
      this._cards.push(card);
      item.appendChild(card);
      item.appendChild(hideSurface);
      item.appendChild(dragSurface);
      this._items.push(item);
    }

    this._syncHiddenKeys();
    this._updateToolbarState();
    this._scheduleLayout();
    setTimeout(() => this._scheduleLayout(), 250);
    setTimeout(() => this._scheduleLayout(), 1000);
  }

  getCardSize() { return 12; }
}

if (!customElements.get('power-sync-layout')) {
  customElements.define('power-sync-layout', PowerSyncLayout);
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
      { element: 'power-flow-card-plus', name: 'power-flow-card-plus', hacs: 'power-flow-card-plus', optional: true },
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
      const resourceNames = [
        c.element,
        c.hacs,
        c.element.replace(/-/g, ''),
        c.hacs?.replace(/-/g, ''),
      ]
        .filter(Boolean)
        .map(name => name.toLowerCase());
      const inResources = lovelaceResources.some(r => {
        const url = String(r.url || '').toLowerCase();
        return resourceNames.some(name => url.includes(name));
      });
      loaded[c.element] = inResources;
    }

    const missing = requiredCards.filter(c => !loaded[c.element] && !c.optional);
    // Use detected state for optional cards; always generate required cards
    const hasButton = true;
    const hasFlowCard = true;
    // Built-in energy flow card is always available (power-sync-energy-flow.js)
    const hasTeslaFlow = true;

    const isAvailableState = (id) => {
      const s = (hass.states || {})[id];
      return s && s.state !== 'unavailable' && s.state !== 'unknown';
    };

    // Entity resolver - tries power_sync_ prefixed first, then HA-renamed
    // PowerSync sensors such as sensor.powersync_amber_battery_level.
    // Handles mixed installs where some entities have the prefix and others don't.
    const e = (name) => {
      const prefixed = `sensor.power_sync_${name}`;
      const bare = `sensor.${name}`;
      const tail = `_${name}`;
      const candidates = [];
      for (const id of [prefixed, bare]) {
        if (hass.states[id]) candidates.push(id);
      }
      for (const id of Object.keys(hass.states || {})) {
        if (!id.startsWith('sensor.')) continue;
        const objectId = id.slice('sensor.'.length);
        if (!objectId.endsWith(tail)) continue;
        if (!objectId.startsWith('power_sync_') && !objectId.startsWith('powersync_')) continue;
        candidates.push(id);
      }
      const unique = Array.from(new Set(candidates));
      if (unique.length === 0) return prefixed;
      return unique.sort((a, b) => {
        const score = (id) => {
          if (isAvailableState(id)) return 0;
          return 1;
        };
        const nameScore = (id) => {
          if (id === prefixed) return 0;
          if (id.startsWith('sensor.powersync_')) return 1;
          if (id.startsWith('sensor.power_sync_')) return 2;
          if (id === bare) return 3;
          return 4;
        };
        return (
          score(a) - score(b) ||
          nameScore(a) - nameScore(b) ||
          a.length - b.length ||
          a.localeCompare(b)
        );
      })[0];
    };

    // Entity existence + availability helper
    const has = (id) => isAvailableState(id);

    // Shorthand: resolve then check
    const hasE = (name) => has(e(name));

    const findSensor = (nameOrNames) => {
      const names = (Array.isArray(nameOrNames) ? nameOrNames : [nameOrNames])
        .map((name) => String(name || '').trim())
        .filter(Boolean);
      if (names.length === 0) return null;

      const directMatches = [];
      for (const name of names) {
        for (const id of [`sensor.power_sync_${name}`, `sensor.${name}`]) {
          if (hass.states[id]) directMatches.push(id);
        }
      }

      const tails = names.map((name) => `_${name}`);
      const suffixMatches = Object.keys(hass.states || {}).filter((id) => {
        if (!id.startsWith('sensor.')) return false;
        const objectId = id.slice('sensor.'.length);
        return names.includes(objectId) || tails.some((tail) => objectId.endsWith(tail));
      });
      const candidates = Array.from(new Set([...directMatches, ...suffixMatches]));
      if (candidates.length === 0) return null;

      const available = candidates.filter(has);
      const pool = available.length > 0 ? available : candidates;
      return pool.sort((a, b) => {
        const score = (id) => {
          if (id.startsWith('sensor.power_sync_')) return 0;
          if (id.includes('goodwe') || id.includes('foxess')) return 1;
          return 2;
        };
        return score(a) - score(b) || a.length - b.length || a.localeCompare(b);
      })[0];
    };

    const findProviderSensor = (provider, suffixOrSuffixes) => {
      const providerKey = String(provider || '').trim().toLowerCase();
      const suffixes = (Array.isArray(suffixOrSuffixes) ? suffixOrSuffixes : [suffixOrSuffixes])
        .map((suffix) => String(suffix || '').trim())
        .filter(Boolean);
      if (!providerKey || suffixes.length === 0) return null;

      const directMatches = [];
      for (const suffix of suffixes) {
        const ids = [
          `sensor.power_sync_${providerKey}_${suffix}`,
          `sensor.powersync_${providerKey}_${suffix}`,
        ];
        if (providerKey === 'globird') {
          ids.push(`sensor.power_sync_${suffix}`, `sensor.powersync_${suffix}`);
        }
        for (const id of ids) {
          if (hass.states[id]) directMatches.push(id);
        }
      }

      const suffixMatches = Object.keys(hass.states || {}).filter((id) => {
        if (!id.startsWith('sensor.')) return false;
        const objectId = id.slice('sensor.'.length);
        const providerPrefixes = [
          `power_sync_${providerKey}_`,
          `powersync_${providerKey}_`,
        ];
        if (providerKey === 'globird') {
          providerPrefixes.push('power_sync_service_', 'powersync_service_');
        }
        if (!providerPrefixes.some((prefix) => objectId.startsWith(prefix))) {
          return false;
        }
        return suffixes.some((suffix) => objectId.endsWith(`_${suffix}`));
      });

      const candidates = Array.from(new Set([...directMatches, ...suffixMatches]));
      if (candidates.length === 0) return null;
      const available = candidates.filter(has);
      const pool = available.length > 0 ? available : candidates;
      return pool.sort((a, b) => {
        const directScore = (id) => directMatches.includes(id) ? 0 : 1;
        return directScore(a) - directScore(b) || a.length - b.length || a.localeCompare(b);
      })[0];
    };

    const hasProviderSensor = (provider, suffixOrSuffixes) => {
      const id = findProviderSensor(provider, suffixOrSuffixes);
      return !!(id && has(id));
    };

    // Domain-aware entity finder used by the Tesla Energy Site controls section.
    // The new Tesla entities use _attr_has_entity_name=True, so HA composes
    // their entity_ids from the device name (e.g. "Home" → home_backup_reserve)
    // rather than the suggested object_id. This helper scans hass.states for
    // an AVAILABLE match first, to avoid surfacing orphaned entities that
    // HA left in the registry from a prior capability-probe result but which
    // are now unavailable because the feature is no longer supported.
    const isAvailable = (id) => {
      const s = (hass.states || {})[id];
      return s && s.state !== 'unavailable' && s.state !== 'unknown';
    };
    const findEntity = (domain, suffix) => {
      const direct = `${domain}.power_sync_${suffix}`;
      if (isAvailable(direct)) return direct;
      const prefix = `${domain}.`;
      const tail = `_${suffix}`;
      // Fallback only matches entities that look Tesla/Powerwall/energy-site related
      // to avoid grabbing unrelated entities from GoodWe, Sigenergy, etc.
      const isTeslaLike = (key) =>
        key.startsWith(`${domain}.power_sync_`) ||
        key.includes('powerwall') ||
        key.includes('tesla') ||
        key.includes('energy_site') ||
        key.includes('teslemetry');
      // First pass: prefer available Tesla-like states
      for (const key of Object.keys(hass.states || {})) {
        if (!key.startsWith(prefix)) continue;
        if (!key.endsWith(tail)) continue;
        if (!isTeslaLike(key)) continue;
        if (isAvailable(key)) return key;
      }
      // Second pass: fall back to temporarily unavailable Tesla-like states
      // (coordinator startup), but never match unrelated integrations.
      for (const key of Object.keys(hass.states || {})) {
        if (!key.startsWith(prefix)) continue;
        if (key.endsWith(tail) && isTeslaLike(key)) return key;
      }
      return null;
    };

    // Find every VPP program switch (one switch per Tesla program enrollment),
    // filtering out orphaned unavailable registry entries.
    const findVppSwitches = () => {
      const matches = new Set();
      for (const key of Object.keys(hass.states || {})) {
        if (!key.startsWith('switch.') || !key.includes('_vpp_')) continue;
        if (isAvailable(key)) matches.add(key);
      }
      return Array.from(matches);
    };

    // ── 3-column layout: left (controls/status), center (flow/charts), right (prices/energy) ──
    const left = [];
    const center = [];
    const right = [];

    // --- Left Column: Price Gauges ---
    if (hasE('current_import_price')) {
      left.push(_priceGauges(e, hass));
    }

    // --- Left Column: Battery Controls (requires button-card) ---
    if (hasButton && (hasE('battery_level') || hasE('battery_power'))) {
      left.push(_batteryControls(hass));
    }

    // --- Left Column: Tesla Energy Site Controls (v2.10.0+) ---
    // Groups backup reserve, operation mode, grid export rule, grid charging,
    // storm watch, off-grid EV reserve, and any VPP program switches into one
    // card. Each row is only added if the corresponding entity exists, so the
    // section gracefully scales from a basic Powerwall (4 rows) to a US site
    // with VPP enrollment (8+ rows).
    //
    // Gated through the Tesla-aware entity resolver. HA may compose these IDs
    // from the device name, or from the newer power_sync_tesla_* object IDs, so
    // direct power_sync_backup_reserve / power_sync_operation_mode checks miss
    // valid Tesla controls.
    {
      const _hasTesla = !!(
        findEntity('number', 'backup_reserve') ||
        findEntity('select', 'operation_mode')
      );
      if (_hasTesla) {
        const teslaSection = _teslaEnergySiteControls(findEntity, findVppSwitches);
        if (teslaSection) left.push(teslaSection);
        const powerwallStatus = _powerwallStatus(e, hasE);
        if (powerwallStatus) left.push(powerwallStatus);
      }
    }

    // --- Left Column: Optimizer Status (requires button-card) ---
    if (hasButton && hasE('optimization_status')) {
      left.push(_optimizerStatus(e));
      center.push(_optimizationPlan(e));
    }

    // --- Center Column: Power Flow ---
    if (hasTeslaFlow && hasE('solar_power')) {
      center.push(_teslaStyleFlow(e, hass, findSensor));
    } else if (hasFlowCard && hasE('solar_power')) {
      center.push(_powerFlow(e));
    }

    // --- Center Column: EV Dashboard Panel ---
    center.push(_evPanel());

    // --- Right Column: Price Chart ---
    if (hasE('current_import_price')) {
      right.push(_priceChart(e, hass));
    }

    // --- Right Column: TOU Schedule (uses PowerSyncChart) ---
    if (hasE('tariff_schedule')) {
      right.push(_touSchedule(e, hass));
    }

    // --- Center Column: LP Forecast Summary ---
    if (hasE('lp_solar_forecast')) {
      center.push(_lpForecastSummary(e, has));
    }

    // --- Center Column: Load Forecast Today/Tomorrow ---
    if (hasE('load_forecast_today_remaining') || hasE('load_forecast_tomorrow')) {
      const loadForecastEntities = [];
      if (hasE('load_forecast_today_remaining')) {
        loadForecastEntities.push({
          entity: e('load_forecast_today_remaining'),
          name: 'Usage Today (Remaining)',
          icon: 'mdi:home-lightning-bolt-outline',
        });
      }
      if (hasE('load_forecast_tomorrow')) {
        loadForecastEntities.push({
          entity: e('load_forecast_tomorrow'),
          name: 'Usage Tomorrow',
          icon: 'mdi:home-clock-outline',
        });
      }
      if (hasE('away_mode')) {
        loadForecastEntities.push({
          entity: e('away_mode'),
          name: 'Away Mode',
          icon: 'mdi:home-export-outline',
        });
      }
      center.push({
        type: 'entities',
        title: 'Load Forecast',
        show_header_toggle: false,
        entities: loadForecastEntities,
      });
    }

    // --- Center Column: LP Price Chart (48h) ---
    if (hasE('lp_import_price_forecast')) {
      center.push(_lpPriceChart(e, hass));
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

    // --- Left Column: PV String Sensors ---
    {
      const pvStringCard = _pvStringSensors(e, hass, findSensor);
      if (pvStringCard) left.push(pvStringCard);
    }

    // --- Left Column: Battery Health (requires button-card) ---
    if (hasButton && hasE('battery_health')) {
      left.push(_batteryHealth(e, hass));
    }

    // --- Controls Column: LP Battery Power Chart (48h) ---
    if (hasE('lp_battery_power_forecast')) {
      left.push(_lpBatteryPowerChart(e));
    }

    // --- Controls Column: Combined Energy Chart ---
    if (hasE('solar_power')) {
      left.push(_combinedEnergyChart(e, hasE('home_load')));
    }

    // --- Center Column: Daily Energy Summary ---
    if (hasE('daily_solar_energy')) {
      const dailyEntities = [
        { entity: e('daily_solar_energy'), name: 'Solar', icon: 'mdi:solar-power' },
        { entity: e('daily_grid_import'), name: 'Grid Import', icon: 'mdi:transmission-tower-import' },
        { entity: e('daily_grid_export'), name: 'Grid Export', icon: 'mdi:transmission-tower-export' },
        { entity: e('daily_battery_charge'), name: 'Battery Charge', icon: 'mdi:battery-charging' },
        { entity: e('daily_battery_discharge'), name: 'Battery Discharge', icon: 'mdi:battery-arrow-down' },
      ];
      if (hasE('daily_load')) {
        dailyEntities.push({ entity: e('daily_load'), name: 'Home Consumption', icon: 'mdi:home-lightning-bolt' });
      }
      center.push({
        type: 'entities',
        title: 'Daily Energy (kWh)',
        show_header_toggle: false,
        entities: dailyEntities,
      });
    }

    // --- Center Column: Daily Cost Tracking ---
    if (hasE('daily_import_cost')) {
      const costEntities = [
        { entity: e('daily_import_cost'), name: 'Import Cost Today', icon: 'mdi:cash-minus' },
      ];
      if (hasE('daily_export_earnings')) {
        costEntities.push({ entity: e('daily_export_earnings'), name: 'Export Earnings Today', icon: 'mdi:cash-plus' });
      }
      if (hasE('daily_avg_cost_per_kwh')) {
        costEntities.push({ entity: e('daily_avg_cost_per_kwh'), name: 'Avg Cost per kWh (Today)', icon: 'mdi:cash-clock' });
      }
      if (hasE('mtd_avg_cost_per_kwh')) {
        costEntities.push({ entity: e('mtd_avg_cost_per_kwh'), name: 'Avg Cost per kWh (Month)', icon: 'mdi:calendar-month' });
      }
      center.push({
        type: 'entities',
        title: 'Daily Cost Tracking',
        show_header_toggle: false,
        entities: costEntities,
      });
    }

    // --- Left Column: Demand Charge ---
    if (hasE('in_demand_charge_period')) {
      left.push(_demandCharge(e));
    }

    // --- Left Column: AEMO Spike ---
    if (hasE('aemo_price')) {
      left.push(_aemoSpike(e));
    }

    // --- Left Column: Powerwall Local Control (only when paired) ---
    // Gated on the binary_sensor.powerwall_local_paired entity so the card
    // stays hidden until the user completes the pairing flow in the app.
    if (hasE('powerwall_local_paired')) {
      left.push(_powerwallLocalControl(e, hasE));
      const health = _powerwallHealth(hass);
      if (health) left.push(health);
    }

    // --- Left Column: Provider Pricing ---
    if (hasE('flow_power_price') || hasE('fp_account_pea')) {
      const flowPowerCard = _flowPower(e, hasE);
      if (flowPowerCard) left.push(flowPowerCard);
    }
    if (hasProviderSensor('globird', ['latest_data_status', 'latest_day_cost', 'balance'])) {
      const globirdCard = _globirdProvider(findProviderSensor);
      if (globirdCard) left.push(globirdCard);
    }

    // --- Left Column: Missing dependency warnings ---
    if (missing.length > 0) {
      left.push({
        type: 'markdown',
        content:
          '**Note:** Some dashboard cards are hidden because these HACS frontend dependencies were not detected:\n\n' +
          missing.map(c => `- **${c.name}** — search "${c.hacs}" in HACS Frontend`).join('\n') + '\n\n' +
          'Install them via [HACS](https://hacs.xyz/) and refresh your browser.',
      });
    }

    const optionalMissing = requiredCards.filter(c => !loaded[c.element] && c.optional);
    if (optionalMissing.length > 0) {
      left.push({
        type: 'markdown',
        content:
          '**Recommended:** Install these optional HACS cards for additional dashboard cards:\n\n' +
          optionalMissing.map(c => `- **${c.name}** — search "${c.hacs}" in HACS Frontend`).join('\n'),
      });
    }

    // ── Build responsive layout ──
    const columns = [left, center, right].filter(col => col.length > 0);
    let cards;

    if (columns.length <= 1) {
      // Single column — flat list for narrow/simple installs
      cards = left.concat(center, right);
    } else {
      // Viewport-fitting grid via custom layout element
      cards = [{
        type: 'custom:power-sync-layout',
        columns,
      }];
    }

    return {
      views: [{
        title: 'Energy Dashboard',
        path: 'energy',
        icon: 'mdi:lightning-bolt',
        type: 'panel',
        cards,
      }],
    };
  }
}

// ─── Helpers ─────────────────────────────────────────────────

function _hassCurrency(hass) {
  return (hass?.config?.currency || 'AUD').toUpperCase();
}

function _minorCurrencyUnit(currency) {
  const code = (currency || 'AUD').toUpperCase();
  if (code === 'GBP') return 'p';
  if (code === 'EUR') return 'ct';
  return 'c';
}

function _currencyFromUnit(unit) {
  const match = String(unit || '').match(/^([A-Z]{3})(?:\/|$)/);
  return match ? match[1] : null;
}

function _priceMeta(hass, entityId) {
  const attrs = hass?.states?.[entityId]?.attributes || {};
  const currency = (attrs.currency || _currencyFromUnit(attrs.unit_of_measurement) || _hassCurrency(hass)).toUpperCase();
  return {
    currency,
    priceUnit: attrs.price_unit || `${currency}/kWh`,
    minorPriceUnit: attrs.minor_price_unit || `${_minorCurrencyUnit(currency)}/kWh`,
    minorUnit: _minorCurrencyUnit(currency),
  };
}

// ─── Section Builders ────────────────────────────────────────

function _optimizationPlan(e) {
  return {
    type: 'custom:power-sync-optimization-plan',
    optimizationPath: 'power_sync/optimization',
    statusEntity: e('optimization_status'),
    forceChargeEntity: e('optimization_force_charge_windows'),
    forceDischargeEntity: e('optimization_force_discharge_windows'),
    importPriceEntity: e('lp_import_price_forecast'),
    exportPriceEntity: e('lp_export_price_forecast'),
  };
}

function _evPanel() {
  return {
    type: 'custom:power-sync-ev-panel',
  };
}

function _svgArcGaugeCard({ entityId, label, unit, min, max, thresholds, multiplier = 1, decimals = 1 }) {
  // thresholds: { green, yellow, red } in display units (after multiplier).
  // Color picked is the one whose threshold is the highest <= displayed value.
  return {
    type: 'custom:button-card',
    entity: entityId,
    show_icon: false,
    show_state: false,
    show_name: false,
    show_label: false,
    custom_fields: {
      gauge: `[[[
        const raw = parseFloat(entity?.state);
        if (isNaN(raw)) {
          return '<div style="text-align:center;padding-top:30px;color:#888;">—</div>';
        }
        const value = raw * ${multiplier};
        const min = ${min}, max = ${max};
        const pct = Math.max(0, Math.min(1, (value - min) / (max - min)));
        const t = ${JSON.stringify(thresholds)};
        const stops = [['#f44336', t.red], ['#ff9800', t.yellow], ['#4caf50', t.green]]
          .filter(([_, th]) => th !== undefined && value >= th)
          .sort((a, b) => b[1] - a[1]);
        const color = stops.length > 0 ? stops[0][0] : '#9e9e9e';
        const r = 50;
        const circ = Math.PI * r;
        const fill = pct * circ;
        const decimals = ${decimals};
        const display = Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(decimals);
        return \`
          <div style="display:flex;flex-direction:column;align-items:center;">
            <div style="font-size:0.85em;color:var(--secondary-text-color);margin-bottom:2px;">${label}</div>
            <svg viewBox="0 0 120 75" style="width:100%;max-width:140px;">
              <path d="M 10,60 A 50,50 0 0,1 110,60" fill="none" stroke="var(--divider-color, #444)" stroke-width="10" stroke-linecap="round"/>
              <path d="M 10,60 A 50,50 0 0,1 110,60" fill="none" stroke="\${color}" stroke-width="10" stroke-linecap="round" stroke-dasharray="\${fill} \${circ}"/>
              <text x="60" y="54" text-anchor="middle" font-size="20" font-weight="600" fill="var(--primary-text-color)">\${display}</text>
              <text x="60" y="68" text-anchor="middle" font-size="9" fill="var(--secondary-text-color)">${unit}</text>
            </svg>
          </div>
        \`;
      ]]]`,
    },
    styles: {
      card: [
        { 'border-radius': '12px' },
        { padding: '8px' },
        { height: '110px' },
      ],
      grid: [
        { 'grid-template-areas': '"gauge"' },
      ],
      custom_fields: {
        gauge: [
          { 'grid-area': 'gauge' },
          { 'align-self': 'center' },
        ],
      },
    },
  };
}

function _priceGauges(e, hass) {
  const importMeta = _priceMeta(hass, e('current_import_price'));
  const exportMeta = _priceMeta(hass, e('current_export_price'));
  return {
    type: 'horizontal-stack',
    cards: [
      _svgArcGaugeCard({
        entityId: e('current_import_price'),
        label: 'Import Price',
        unit: importMeta.minorPriceUnit,
        min: 0,
        max: 60,
        thresholds: { green: 0, yellow: 25, red: 40 },
        multiplier: 100,
      }),
      _svgArcGaugeCard({
        entityId: e('current_export_price'),
        label: 'Export Price',
        unit: exportMeta.minorPriceUnit,
        min: -10,
        max: 30,
        thresholds: { green: 5, yellow: 0, red: -10 },
        multiplier: 100,
      }),
      _svgArcGaugeCard({
        entityId: e('battery_level'),
        label: 'Battery',
        unit: '%',
        min: 0,
        max: 100,
        thresholds: { green: 60, yellow: 30, red: 0 },
        decimals: 0,
      }),
    ],
  };
}

function _batteryControls(hass) {
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

  const hasForcePower = !!(hass && hass.states['number.power_sync_force_power_kw']);

  return {
    type: 'vertical-stack',
    cards: [
      // Power slider — only shown when the ForcePowerNumber entity exists.
      // Tile card with numeric-input feature gives a clean inline slider.
      // 0 kW = auto (uses inverter rated/BMS max at dispatch).
      ...(hasForcePower ? [{
        type: 'tile',
        entity: 'number.power_sync_force_power_kw',
        name: 'Force Power (0 = Max)',
        icon: 'mdi:lightning-bolt',
        features: [{ type: 'numeric-input', mode: 'slider' }],
        card_mod: {
          style: `
            ha-card {
              background: rgba(0, 180, 220, 0.07) !important;
              border: 1px solid rgba(0, 180, 220, 0.18) !important;
              border-radius: 12px !important;
              box-shadow: none !important;
              --tile-color: rgb(0, 180, 220);
            }
          `,
        },
      }] : []),
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
                power_w: "[[[ const kw = parseFloat(states['number.power_sync_force_power_kw']?.state) || 0; return kw > 0 ? Math.round(kw * 1000) : undefined; ]]]",
              },
              confirmation: {
                text: "[[[ const kw = parseFloat(states['number.power_sync_force_power_kw']?.state) || 0; const dur = states['select.power_sync_force_charge_duration']?.state ?? '30'; return 'Force charge for ' + dur + ' min' + (kw > 0 ? ' at ' + kw.toFixed(1) + ' kW' : ' at max power') + '?'; ]]]",
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
                power_w: "[[[ const kw = parseFloat(states['number.power_sync_force_power_kw']?.state) || 0; return kw > 0 ? Math.round(kw * 1000) : undefined; ]]]",
              },
              confirmation: {
                text: "[[[ const kw = parseFloat(states['number.power_sync_force_power_kw']?.state) || 0; const dur = states['select.power_sync_force_discharge_duration']?.state ?? '30'; return 'Force discharge for ' + dur + ' min' + (kw > 0 ? ' at ' + kw.toFixed(1) + ' kW' : ' at max power') + '?'; ]]]",
              },
            },
          },
        ],
      },
      {
        type: 'custom:button-card',
        name: 'Self Consumption',
        icon: 'mdi:home-battery',
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
            { 'font-size': '14px' },
          ],
        },
        tap_action: {
          action: 'call-service',
          service: 'power_sync.set_self_consumption',
          confirmation: { text: 'Set battery to self-consumption mode?' },
        },
      },
      {
        square: false,
        type: 'grid',
        columns: 2,
        cards: [
          {
            type: 'custom:button-card',
            name: 'Hold SoC',
            icon: 'mdi:battery-lock',
            styles: {
              card: [
                { height: '40px' },
                { 'border-radius': '18px' },
                { padding: '4px 12px' },
                { background: 'rgba(var(--rgb-blue-color, 33, 150, 243), 0.1)' },
              ],
              grid: [
                { 'grid-template-areas': '"i n"' },
                { 'grid-template-columns': '24px 1fr' },
              ],
              icon: [
                { 'grid-area': 'i' },
                { width: '24px' },
                { color: 'var(--blue-color, #2196F3)' },
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
              service: 'power_sync.hold_battery_soc',
              data: {
                duration: "[[[ return (states['select.power_sync_force_discharge_duration'] ? states['select.power_sync_force_discharge_duration'].state : '60'); ]]]",
              },
              confirmation: {
                text: "[[[ const dur = states['select.power_sync_force_discharge_duration']?.state ?? '60'; return 'Hold battery at current SoC for ' + dur + ' min?'; ]]]",
              },
            },
          },
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

function _teslaEnergySiteControls(findEntity, findVppSwitches) {
  // Resolve all the entity_ids the strategy supports for Tesla Energy Sites.
  const backupReserve = findEntity('number', 'backup_reserve');
  const offGridReserve = findEntity('number', 'off_grid_ev_reserve');
  const operationMode = findEntity('select', 'operation_mode');
  const exportRule = findEntity('select', 'grid_export_rule');
  const gridCharging = findEntity('switch', 'grid_charging');
  const stormWatch = findEntity('switch', 'storm_watch');
  const stormActive = findEntity('binary_sensor', 'storm_watch_active');
  const manualOverride = findEntity('binary_sensor', 'manual_export_override');
  const vppSwitches = findVppSwitches();

  // Diagnostic log so missing entities are visible in the browser console.
  // Shown once per dashboard render; helps debug the "only grid charging
  // shows" class of report when a user's entity_id naming doesn't match.
  try {
    const report = {
      backupReserve, offGridReserve, operationMode, exportRule,
      gridCharging, stormWatch, stormActive, manualOverride,
      vppCount: vppSwitches.length,
    };
    console.debug('[PowerSync Strategy] Tesla Energy Site controls:', report);
  } catch (_) { /* ignore */ }

  const cards = [];

  // ── Slider row: backup reserve + off-grid EV reserve (when supported) ──
  const sliders = [];
  if (backupReserve) {
    sliders.push({
      type: 'tile',
      entity: backupReserve,
      name: 'Backup Reserve',
      icon: 'mdi:battery-lock',
      vertical: false,
    });
  }
  if (offGridReserve) {
    sliders.push({
      type: 'tile',
      entity: offGridReserve,
      name: 'Off-Grid EV Reserve',
      icon: 'mdi:car-electric',
      vertical: false,
    });
  }
  if (sliders.length > 0) {
    cards.push({
      type: 'grid',
      columns: sliders.length === 2 ? 2 : 1,
      square: false,
      cards: sliders,
    });
  }

  // ── Select row: operation mode + grid export rule (side-by-side) ──
  const selects = [];
  if (operationMode) {
    selects.push({
      type: 'tile',
      entity: operationMode,
      name: 'Operation Mode',
      icon: 'mdi:cog-transfer',
      vertical: false,
    });
  }
  if (exportRule) {
    selects.push({
      type: 'tile',
      entity: exportRule,
      name: 'Grid Export',
      icon: 'mdi:transmission-tower-export',
      vertical: false,
    });
  }
  if (selects.length > 0) {
    cards.push({
      type: 'grid',
      columns: selects.length === 2 ? 2 : 1,
      square: false,
      cards: selects,
    });
  }

  // ── Toggle row: grid charging + storm watch + each VPP program ──
  const toggles = [];
  if (gridCharging) {
    toggles.push({
      type: 'tile',
      entity: gridCharging,
      name: 'Grid Charging',
      icon: 'mdi:transmission-tower-import',
      vertical: false,
    });
  }
  if (stormWatch) {
    toggles.push({
      type: 'tile',
      entity: stormWatch,
      name: 'Storm Watch',
      icon: 'mdi:weather-lightning',
      vertical: false,
    });
  }
  for (const sw of vppSwitches) {
    const tail = sw.split('_vpp_').pop() || '';
    const label = tail
      .split('_').filter(Boolean)
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(' ');
    toggles.push({
      type: 'tile',
      entity: sw,
      name: label ? `VPP: ${label}` : 'VPP Program',
      icon: 'mdi:transmission-tower',
      vertical: false,
    });
  }
  if (toggles.length > 0) {
    // Split into rows of 2 for better mobile layout
    cards.push({
      type: 'grid',
      columns: Math.min(2, toggles.length),
      square: false,
      cards: toggles,
    });
  }

  // ── Status row: storm active + manual export override + grid services + calibration + PTO ──
  const gridServicesActive = findEntity('binary_sensor', 'grid_services_active');
  const calibrationActive = findEntity('binary_sensor', 'calibration_active');
  const permissionToOperate = findEntity('binary_sensor', 'permission_to_operate');
  const statuses = [];
  if (stormActive) {
    statuses.push({ entity: stormActive, name: 'Storm Watch Active', icon: 'mdi:weather-lightning-rainy' });
  }
  if (gridServicesActive) {
    statuses.push({ entity: gridServicesActive, name: 'Grid Services Active', icon: 'mdi:transmission-tower-export' });
  }
  if (calibrationActive) {
    statuses.push({ entity: calibrationActive, name: 'Calibration Active', icon: 'mdi:battery-sync' });
  }
  if (permissionToOperate) {
    statuses.push({ entity: permissionToOperate, name: 'Permission to Operate', icon: 'mdi:check-decagram' });
  }
  if (manualOverride) {
    statuses.push({ entity: manualOverride, name: 'Manual Export Override', icon: 'mdi:hand-back-right' });
  }
  if (statuses.length > 0) {
    cards.push({
      type: 'entities',
      title: null,
      show_header_toggle: false,
      state_color: true,
      entities: statuses,
    });
  }

  if (cards.length === 0) return null;

  return {
    type: 'vertical-stack',
    cards: [
      {
        type: 'markdown',
        content: '## ⚡ Tesla Energy Site\n_Powerwall and site-level controls_',
        card_mod: { style: 'ha-card { padding: 8px 16px 0; background: none; border: none; box-shadow: none; }' },
      },
      ...cards,
    ],
  };
}

function _powerwallStatus(e, hasE) {
  // Live Powerwall vitals: backup runtime, capacity, lifetime totals.
  // Each row is added only when the underlying sensor has a value, so the
  // section gracefully scales from a fresh install (no lifetime data yet) to
  // a long-running site (full energy history).
  const live = [];
  if (hasE('backup_time_remaining')) {
    live.push({ entity: e('backup_time_remaining'), name: 'Backup Time Remaining', icon: 'mdi:timer-sand' });
  }
  if (hasE('energy_left')) {
    live.push({ entity: e('energy_left'), name: 'Energy Available', icon: 'mdi:battery-50' });
  }
  if (hasE('total_pack_energy')) {
    live.push({ entity: e('total_pack_energy'), name: 'Pack Capacity', icon: 'mdi:battery-high' });
  }
  if (hasE('grid_services_power')) {
    live.push({ entity: e('grid_services_power'), name: 'Grid Services Power', icon: 'mdi:transmission-tower' });
  }

  const lifetime = [];
  if (hasE('lifetime_solar_energy')) {
    lifetime.push({ entity: e('lifetime_solar_energy'), name: 'Solar', icon: 'mdi:solar-power-variant' });
  }
  if (hasE('lifetime_grid_import')) {
    lifetime.push({ entity: e('lifetime_grid_import'), name: 'Grid Import', icon: 'mdi:transmission-tower-import' });
  }
  if (hasE('lifetime_grid_export')) {
    lifetime.push({ entity: e('lifetime_grid_export'), name: 'Grid Export', icon: 'mdi:transmission-tower-export' });
  }
  if (hasE('lifetime_battery_charged')) {
    lifetime.push({ entity: e('lifetime_battery_charged'), name: 'Battery Charged', icon: 'mdi:battery-charging-100' });
  }
  if (hasE('lifetime_battery_discharged')) {
    lifetime.push({ entity: e('lifetime_battery_discharged'), name: 'Battery Discharged', icon: 'mdi:battery-arrow-down' });
  }
  if (hasE('lifetime_home_consumption')) {
    lifetime.push({ entity: e('lifetime_home_consumption'), name: 'Home Consumption', icon: 'mdi:home-lightning-bolt' });
  }

  if (live.length === 0 && lifetime.length === 0) return null;

  const cards = [];
  if (live.length > 0) {
    cards.push({
      type: 'entities',
      title: 'Powerwall Status',
      show_header_toggle: false,
      entities: live,
    });
  }
  if (lifetime.length > 0) {
    cards.push({
      type: 'entities',
      title: 'Lifetime Energy',
      show_header_toggle: false,
      entities: lifetime,
    });
  }
  return { type: 'vertical-stack', cards };
}

function _optimizerStatus(e, showForceChargeWindows = false, showForceDischargeWindows = false) {
  const statusEntity = e('optimization_status');
  const nextEntity = e('optimization_next_action');
  const forceChargeWindowsEntity = e('optimization_force_charge_windows');
  const forceDischargeWindowsEntity = e('optimization_force_discharge_windows');
  const cards = [{
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
      if (current.attributes?.idle_hold_active) {
        const holdPct = Number(current.attributes?.idle_hold_reserve_percent);
        const holdText = Number.isFinite(holdPct)
          ? 'holding SOC at ' + Math.round(holdPct) + '%'
          : 'temporary hold';
        line1 += ' - ' + holdText;
      }
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
  }];

  if (showForceChargeWindows || showForceDischargeWindows) {
    cards.push({
      type: 'custom:button-card',
      name: 'Planned Battery Windows',
      icon: 'mdi:calendar-clock',
      show_icon: true,
      show_name: true,
      show_label: true,
      triggers_update: [
        ...(showForceChargeWindows ? [forceChargeWindowsEntity] : []),
        ...(showForceDischargeWindows ? [forceDischargeWindowsEntity] : []),
      ],
      label: `[[[
        const sensors = [
          ${showForceChargeWindows ? `'${forceChargeWindowsEntity}'` : 'null'},
          ${showForceDischargeWindows ? `'${forceDischargeWindowsEntity}'` : 'null'},
        ].filter(Boolean);
        const count = sensors.reduce((sum, entityId) => {
          const n = Number(states[entityId]?.attributes?.count ?? 0);
          return sum + (Number.isFinite(n) ? n : 0);
        }, 0);
        if (!count) return 'No forced charge or discharge windows in the next 24h';
        return count + ' upcoming window' + (count === 1 ? '' : 's');
      ]]]`,
      custom_fields: {
        windows: `[[[
          const inputs = [
            ${showForceChargeWindows ? `{entityId: '${forceChargeWindowsEntity}', kind: 'charge'}` : 'null'},
            ${showForceDischargeWindows ? `{entityId: '${forceDischargeWindowsEntity}', kind: 'discharge'}` : 'null'},
          ].filter(Boolean);
          const fmtTime = (value) => {
            if (!value) return '--:--';
            const d = new Date(value);
            if (Number.isNaN(d.getTime())) return '--:--';
            return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
          };
          const fmtDuration = (minutes) => {
            const mins = Number(minutes || 0);
            if (!Number.isFinite(mins) || mins <= 0) return '';
            if (mins < 60) return Math.round(mins) + ' min';
            const hours = Math.floor(mins / 60);
            const rem = Math.round(mins % 60);
            return hours + 'h' + (rem ? ' ' + rem + 'm' : '');
          };
          const fmtPower = (watts) => {
            const value = Number(watts || 0);
            if (!Number.isFinite(value) || Math.abs(value) < 1) return '';
            return Math.abs(value) >= 1000
              ? (Math.abs(value) / 1000).toFixed(1) + ' kW'
              : Math.round(Math.abs(value)) + ' W';
          };
          const labelFor = (entry) => {
            if (entry.kind === 'charge') return 'Charge';
            return entry.action === 'export' ? 'Export' : 'Discharge';
          };
          const entries = inputs.flatMap(({entityId, kind}) => {
            const windows = states[entityId]?.attributes?.windows;
            if (!Array.isArray(windows)) return [];
            return windows.map((window) => ({...window, entityId, kind}));
          }).filter((entry) => entry.start_time && entry.end_time)
            .sort((a, b) => new Date(a.start_time) - new Date(b.start_time));

          const style = '<style>' +
            '.ps-window-list{display:grid;gap:8px;margin-top:10px;}' +
            '.ps-window-row{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;padding:9px 10px;border-radius:10px;background:var(--ha-card-background,var(--card-background-color,#fff));border:1px solid var(--divider-color);}' +
            '.ps-window-row.charge{border-left:4px solid var(--blue-color,#2196f3);}' +
            '.ps-window-row.discharge{border-left:4px solid var(--orange-color,#ff9800);}' +
            '.ps-pill{font-size:11px;font-weight:800;letter-spacing:0;text-transform:uppercase;line-height:1;padding:6px 7px;border-radius:999px;}' +
            '.ps-window-row.charge .ps-pill{color:var(--blue-color,#2196f3);background:rgba(33,150,243,.12);}' +
            '.ps-window-row.discharge .ps-pill{color:var(--orange-color,#ff9800);background:rgba(255,152,0,.14);}' +
            '.ps-times{font-size:14px;font-weight:700;color:var(--primary-text-color);line-height:1.25;}' +
            '.ps-meta{font-size:12px;color:var(--secondary-text-color);line-height:1.25;margin-top:2px;}' +
            '.ps-power{font-size:13px;font-weight:800;color:var(--primary-text-color);white-space:nowrap;}' +
            '.ps-empty{margin-top:10px;padding:10px;border-radius:10px;background:rgba(127,127,127,.08);color:var(--secondary-text-color);font-size:13px;text-align:left;}' +
            '</style>';

          if (!entries.length) {
            return style + '<div class="ps-empty">Optimizer has no forced charge, discharge, or export windows scheduled.</div>';
          }

          return style + '<div class="ps-window-list">' + entries.slice(0, 6).map((entry) => {
            const cssKind = entry.kind === 'charge' ? 'charge' : 'discharge';
            const duration = fmtDuration(entry.duration_minutes);
            const power = fmtPower(entry.power_w);
            const soc = Number.isFinite(Number(entry.soc)) ? Math.round(Number(entry.soc) * 100) + '% SoC' : '';
            const meta = [duration, soc].filter(Boolean).join(' - ');
            return '<div class="ps-window-row ' + cssKind + '">' +
              '<div class="ps-pill">' + labelFor(entry) + '</div>' +
              '<div><div class="ps-times">' + fmtTime(entry.start_time) + ' - ' + fmtTime(entry.end_time) + '</div>' +
              '<div class="ps-meta">' + (meta || 'Scheduled window') + '</div></div>' +
              '<div class="ps-power">' + power + '</div>' +
              '</div>';
          }).join('') + (entries.length > 6
            ? '<div class="ps-empty">+' + (entries.length - 6) + ' more window' + (entries.length - 6 === 1 ? '' : 's') + '</div>'
            : '') + '</div>';
        ]]]`,
      },
      styles: {
        card: [
          { 'border-radius': '16px' },
          { padding: '12px' },
          { background: 'linear-gradient(135deg, rgba(33, 150, 243, 0.08) 0%, rgba(255, 152, 0, 0.08) 100%)' },
        ],
        grid: [
          { 'grid-template-areas': '"i n" "i l" "windows windows"' },
          { 'grid-template-columns': 'min-content 1fr' },
          { 'column-gap': '10px' },
          { 'row-gap': '2px' },
          { 'align-items': 'center' },
        ],
        img_cell: [
          { width: '32px' },
          { height: '32px' },
          { 'align-self': 'start' },
        ],
        icon: [
          { width: '28px' },
          { color: 'var(--primary-color)' },
        ],
        name: [
          { 'justify-self': 'start' },
          { 'font-weight': '700' },
          { 'font-size': '16px' },
        ],
        label: [
          { 'justify-self': 'start' },
          { opacity: '0.85' },
          { 'font-size': '13px' },
          { 'text-align': 'left' },
        ],
        custom_fields: {
          windows: [
            { 'grid-area': 'windows' },
            { width: '100%' },
          ],
        },
      },
      tap_action: { action: 'none' },
    });
  }

  return {
    type: 'vertical-stack',
    cards,
  };
}

function _teslaStyleFlow(e, hass, findSensor) {
  // Auto-detect weather entity — try common patterns
  let weatherEntity = null;
  for (const candidate of [
    'weather.home', 'weather.forecast_home',
    'weather.openweathermap', 'weather.bom',
  ]) {
    if (hass.states[candidate]) {
      weatherEntity = candidate;
      break;
    }
  }
  // Fallback: find any weather.* entity
  if (!weatherEntity) {
    const weatherKey = Object.keys(hass.states).find(k => k.startsWith('weather.'));
    if (weatherKey) weatherEntity = weatherKey;
  }

  const config = {
    type: 'custom:power-sync-energy-flow',
    show_header: false,
    dynamic_background: true,
    language: 'en',
    grid_invert: false,
    battery_invert: true,
    ev_hide_when_idle: false,
    ev_min_w: 50,
    thresholds: { solar_min_w: 50, grid_min_w: 50, battery_min_w: 50 },
    entities: {
      solar_power: e('solar_power'),
      grid_power: e('grid_power'),
      grid_status: e('grid_status'),
      battery_power: e('battery_power'),
      load_power: e('home_load'),
      battery_level: e('battery_level'),
      sun: 'sun.sun',
    },
  };

  // Add weather if found
  if (weatherEntity) {
    config.entities.weather = weatherEntity;
  }

  // Add EV if sensors exist
  const evPower = e('ev_power');
  if (hass.states[evPower]) {
    config.entities.ev_power = evPower;
    const matchedVehicleName = String(hass.states[evPower]?.attributes?.vehicle_name || '').trim();
    if (matchedVehicleName && !/^wall connector$/i.test(matchedVehicleName)) {
      config.ev_label = matchedVehicleName;
    }
    const evBattery = e('ev_battery_level');
    if (hass.states[evBattery]) {
      config.entities.ev_battery = evBattery;
    }
    const evPowerAttrs = hass.states[evPower]?.attributes || {};
    if (
      !config.entities.ev_presence &&
      (Object.prototype.hasOwnProperty.call(evPowerAttrs, 'is_connected') ||
        Object.prototype.hasOwnProperty.call(evPowerAttrs, 'is_charging'))
    ) {
      config.entities.ev_presence = evPower;
    }
    // Auto-detect EV presence sensor (shows car even when idle/not charging)
    // Searches: Tesla BLE charge flap, Teslemetry BT charging state,
    // Tesla Fleet charge cable/charging state, Wallbox/Easee/OCPP status
    if (!config.entities.ev_presence) {
      const evPresenceCandidates = Object.keys(hass.states).filter(eid => {
        // Tesla BLE charge flap (binary_sensor.*_charge_flap)
        if (eid.startsWith('binary_sensor.') && eid.endsWith('_charge_flap')) return true;
        // Tesla Fleet / Teslemetry charge cable (binary_sensor.*_charge_cable)
        if (eid.startsWith('binary_sensor.') && eid.endsWith('_charge_cable')) return true;
        // Wallbox connected sensor
        if (eid.startsWith('binary_sensor.') && eid.includes('wallbox') && eid.includes('plugged')) return true;
        // Easee cable locked (indicates plugged in)
        if (eid.startsWith('binary_sensor.') && eid.includes('easee') && eid.includes('cable_locked')) return true;
        return false;
      });
      if (evPresenceCandidates.length > 0) {
        config.entities.ev_presence = evPresenceCandidates[0];
      } else {
        // Fallback: use any *_charging_state sensor as pseudo-presence
        // States like "Charging", "Complete", "Connected", "Stopped" = present
        // "Disconnected", "unknown", "unavailable" = absent
        const chargingStateSensor = Object.keys(hass.states).find(eid =>
          eid.startsWith('sensor.') && eid.endsWith('_charging_state') &&
          !eid.includes('power_sync')
        );
        if (chargingStateSensor) {
          config.entities.ev_presence = chargingStateSensor;
        }
      }
    }
    // Derive EV name from Tesla/Teslemetry vehicle entity prefix
    // e.g. binary_sensor.tessy_charge_cable → "Tessy"
    // e.g. sensor.tessy_charging → "Tessy"
    if (!config.ev_label) {
      // Prefer _charge_cable (Teslemetry/Fleet — uses vehicle name like "tessy")
      // over _charge_flap (BLE — uses device name like "teslable")
      const evNameEntity =
        Object.keys(hass.states).find(eid =>
          eid.startsWith('binary_sensor.') && eid.endsWith('_charge_cable')
        ) ||
        config.entities.ev_presence ||
        Object.keys(hass.states).find(eid =>
          eid.startsWith('sensor.') && eid.endsWith('_charging') &&
          !eid.includes('power_sync')
        );
      if (evNameEntity) {
        // Try friendly_name from the device first (e.g. "Tessy")
        const friendlyName = hass.states[evNameEntity]?.attributes?.friendly_name || '';
        // Extract prefix from entity_id: binary_sensor.tessy_charge_cable → tessy
        const entitySuffix = evNameEntity.split('.')[1] || '';
        const suffixes = ['_charge_flap', '_charge_cable', '_charging_state', '_charging',
          '_charger', '_plugged_in', '_cable_locked'];
        let prefix = '';
        for (const s of suffixes) {
          if (entitySuffix.endsWith(s)) {
            prefix = entitySuffix.slice(0, -s.length);
            break;
          }
        }
        if (prefix) {
          // Capitalize: tessy → Tessy, my_car → My Car
          config.ev_label = prefix.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
        } else if (friendlyName && !friendlyName.toLowerCase().includes('ev power')) {
          // Use friendly_name if it's not the generic PowerSync sensor name
          config.ev_label = friendlyName.replace(/\s*(charge|charging|cable|flap|state).*$/i, '').trim();
        }
      }
    }
  }

  // Keep string-level PV telemetry in the details table; avoid adding it to the
  // house scene by default, where it quickly becomes visual clutter.

  // Sigenergy DC/AC PV split
  const pvDc = e('pv_dc_power');
  const pvAc = e('pv_ac_power');
  if (hass.states[pvDc]) {
    config.entities.roof_a_power = pvDc;
    config.roof_a_label = 'DC Solar';
  }
  if (hass.states[pvAc]) {
    config.entities.roof_b_power = pvAc;
    config.roof_b_label = 'AC Solar';
  }

  return config;
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

function _priceChart(e, hass) {
  const importMeta = _priceMeta(hass, e('current_import_price'));
  return {
    type: 'custom:power-sync-chart',
    title: 'Electricity Prices - 24 Hours',
    mode: 'history',
    historyHours: 24,
    historyRange: 'today',
    height: 255,
    yUnit: importMeta.minorPriceUnit,
    yUnitCompact: true,
    yMultiplier: 100,
    zeroBaseline: true,
    stepLine: true,
    series: [
      {
        entity: e('current_import_price'),
        name: 'Import Price',
        color: '#FF9800',
      },
      {
        entity: e('current_export_price'),
        name: 'Export Price',
        color: '#4CAF50',
      },
    ],
  };
}

function _touSchedule(e, hass) {
  const meta = _priceMeta(hass, e('tariff_schedule'));
  return {
    type: 'custom:power-sync-chart',
    title: 'TOU Schedule',
    entity: e('tariff_schedule'),
    mode: 'tou',
    height: 235,
    stepLine: true,
    yUnit: meta.minorPriceUnit,
    yUnitCompact: true,
    yMultiplier: 100,
    hideZeroTickLabel: true,
    series: [
      { key: 'buy', name: 'Buy Price', color: '#FF9800' },
      { key: 'sell', name: 'Sell Price', color: '#4CAF50' },
    ],
  };
}

function _lpForecastSummary(e, has) {
  const items = [
    { entity: e('lp_solar_forecast'), name: 'Solar Forecast', icon: 'mdi:solar-power-variant' },
    { entity: e('lp_load_forecast'), name: 'Load Forecast', icon: 'mdi:home-lightning-bolt' },
  ];
  if (has(e('lp_import_price_forecast'))) {
    items.push({ entity: e('lp_import_price_forecast'), name: 'Import Price Avg', icon: 'mdi:cash-clock', price: true });
  }
  if (has(e('lp_export_price_forecast'))) {
    items.push({ entity: e('lp_export_price_forecast'), name: 'Export Price Avg', icon: 'mdi:cash-clock', price: true });
  }
  return { type: 'custom:power-sync-forecast-summary', items };
}

function _lpSolarLoadChart(e) {
  return {
    type: 'custom:power-sync-chart',
    title: 'LP Forecast - Solar & Load (48h)',
    mode: 'forecast',
    intervalMinutes: 5,
    height: 260,
    yUnit: 'kW',
    yMin: 0,
    series: [
      { entity: e('lp_solar_forecast'), attribute: 'forecast_values_kw', name: 'Solar', color: '#FFD700', fill: true },
      { entity: e('lp_load_forecast'), attribute: 'forecast_values_kw', name: 'Load', color: '#9C27B0' },
    ],
  };
}

function _lpPriceChart(e, hass) {
  const meta = _priceMeta(hass, e('lp_import_price_forecast'));
  return {
    type: 'custom:power-sync-chart',
    title: 'LP Forecast - Import & Export Prices (48h)',
    mode: 'forecast',
    intervalMinutes: 5,
    height: 255,
    stepLine: true,
    yUnit: meta.minorPriceUnit,
    yUnitCompact: true,
    yMultiplier: 100,
    series: [
      { entity: e('lp_import_price_forecast'), attribute: 'price_values', name: 'Import', color: '#FF9800' },
      { entity: e('lp_export_price_forecast'), attribute: 'price_values', name: 'Export', color: '#4CAF50' },
    ],
  };
}

function _lpBatteryPowerChart(e) {
  return {
    type: 'custom:power-sync-chart',
    title: 'LP Forecast - Battery Power (48h)',
    mode: 'forecast',
    intervalMinutes: 5,
    height: 255,
    stepLine: true,
    yUnit: 'kW',
    series: [
      { entity: e('lp_battery_power_forecast'), attribute: 'charge_values_kw', name: 'Charge', color: '#2196F3', fill: true },
      { entity: e('lp_battery_power_forecast'), attribute: 'discharge_values_kw', name: 'Discharge', color: '#4CAF50', fill: true },
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
      name: 'DC Solar',
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

function _pvStringSensors(e, hass, findSensor) {
  const pvStringEntities = [];
  const extraEntities = [];
  const resolveSensor = (names, fallback) =>
    (typeof findSensor === 'function' ? findSensor(names) : null) || e(fallback);
  const isAvailable = (entity) => {
    const state = hass?.states?.[entity]?.state;
    return state != null && !['unknown', 'unavailable', 'none'].includes(String(state).toLowerCase());
  };
  const numericState = (entity) => {
    if (!isAvailable(entity)) return null;
    const value = Number(hass.states[entity].state);
    return Number.isFinite(value) ? value : null;
  };
  const powerWatts = (entity) => {
    const value = numericState(entity);
    if (value == null) return null;
    const unit = String(hass.states[entity]?.attributes?.unit_of_measurement || '').toLowerCase();
    return unit === 'kw' ? value * 1000 : value;
  };
  const hasMeaningfulPower = (entity) => {
    const watts = powerWatts(entity);
    return watts != null && Math.abs(watts) > 25;
  };
  const hasMeaningfulCurrent = (entity) => {
    const amps = numericState(entity);
    return amps != null && Math.abs(amps) > 0.1;
  };
  const rowFor = (entity, name, icon) => {
    if (!entity || !hass?.states?.[entity]) return;
    const row = { entity, name };
    if (icon) row.icon = icon;
    return row;
  };
  const addStringRow = (entity, name, icon) => {
    if (!isAvailable(entity)) return;
    const row = rowFor(entity, name, icon);
    if (row) pvStringEntities.push(row);
  };
  const addString = (powerEntity, voltageEntity, currentEntity, label) => {
    if (!hasMeaningfulPower(powerEntity) && !hasMeaningfulCurrent(currentEntity)) return;
    addStringRow(powerEntity, `${label} Power`, 'mdi:solar-panel');
    addStringRow(voltageEntity, `${label} Voltage`, 'mdi:sine-wave');
    addStringRow(currentEntity, `${label} Current`, 'mdi:current-dc');
  };
  const addExtra = (entity, name, icon) => {
    if (!isAvailable(entity)) return;
    const row = rowFor(entity, name, icon);
    if (row) extraEntities.push(row);
  };

  for (let idx = 1; idx <= 6; idx += 1) {
    addString(
      resolveSensor([`pv${idx}_power`, `pv_${idx}_power`, `pv_power_${idx}`, `ppv${idx}`], `pv${idx}_power`),
      resolveSensor([`pv${idx}_voltage`, `pv_${idx}_voltage`, `pv_voltage_${idx}`, `vpv${idx}`], `pv${idx}_voltage`),
      resolveSensor([`pv${idx}_current`, `pv_${idx}_current`, `pv_current_${idx}`, `ipv${idx}`], `pv${idx}_current`),
      `PV${idx}`,
    );
  }

  if (pvStringEntities.length === 0) return null;

  addExtra(e('ct2_power'), 'CT2 Power', 'mdi:current-ac');
  addExtra(e('work_mode'), 'Work Mode', 'mdi:cog');
  addExtra(e('min_soc'), 'Min SOC', 'mdi:battery-low');
  addExtra(e('daily_battery_charge_foxess'), 'Daily Charge', 'mdi:battery-charging');
  addExtra(e('daily_battery_discharge_foxess'), 'Daily Discharge', 'mdi:battery-arrow-down');

  return {
    type: 'entities',
    title: 'PV String Details',
    show_header_toggle: false,
    entities: [...pvStringEntities, ...extraEntities],
  };
}

function _batteryHealth(e, hass) {
  return {
    type: 'custom:power-sync-battery-health',
    entity: e('battery_health'),
  };
}

function _combinedEnergyChart(e, hasHome) {
  const series = [
    { entity: e('solar_power'), name: 'Solar', color: '#FFD700', fill: true },
    { entity: e('grid_power'), name: 'Grid', color: '#F44336' },
    { entity: e('battery_power'), name: 'Battery', color: '#2196F3' },
  ];
  if (hasHome) {
    series.push({ entity: e('home_load'), name: 'Home', color: '#9C27B0', minValue: 0 });
  }
  return {
    type: 'custom:power-sync-chart',
    title: 'Energy - 24 Hours',
    mode: 'history',
    historyHours: 24,
    historyRange: 'today',
    height: 275,
    yUnit: 'kW',
    zeroBaseline: true,
    series,
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

function _powerwallLocalControl(e, hasE) {
  const statusEntities = [
    {
      entity: e('powerwall_local_paired'),
      name: 'Paired',
      icon: 'mdi:key-variant',
    },
    {
      entity: e('powerwall_local_islanded'),
      name: 'Off-Grid',
      icon: 'mdi:transmission-tower-off',
    },
  ];
  if (hasE && hasE('pw_system_island_state')) {
    statusEntities.push({
      entity: e('pw_system_island_state'),
      name: 'Island State',
      icon: 'mdi:transmission-tower',
    });
  }
  if (hasE && hasE('pw_count')) {
    statusEntities.push({
      entity: e('pw_count'),
      name: 'Powerwalls',
      icon: 'mdi:battery-multiple',
    });
  }
  if (hasE && hasE('pw_active_alerts')) {
    statusEntities.push({
      entity: e('pw_active_alerts'),
      name: 'Active Alerts',
      icon: 'mdi:alert-circle',
    });
  }
  if (hasE && hasE('pw_critical_alert')) {
    statusEntities.push({
      entity: e('pw_critical_alert'),
      name: 'Alert Active',
      icon: 'mdi:alert-octagon',
    });
  }
  return {
    type: 'vertical-stack',
    cards: [
      {
        type: 'entities',
        title: 'Powerwall Local Control',
        show_header_toggle: false,
        state_color: true,
        entities: statusEntities,
      },
      {
        type: 'conditional',
        conditions: [
          { entity: e('powerwall_local_paired'), state: 'on' },
        ],
        card: {
          type: 'entities',
          entities: [
            {
              entity: findEntity('switch', 'on_grid') || 'switch.power_sync_on_grid',
              name: 'On-Grid Mode',
              icon: 'mdi:transmission-tower',
            },
            {
              entity: findEntity('switch', 'off_grid') || e('off_grid'),
              name: 'Off-Grid Mode',
              icon: 'mdi:transmission-tower-off',
            },
          ],
        },
      },
    ],
  };
}

function _powerwallHealth(hass) {
  // Scan hass.states for per-PW sensors created by the lazy-add task.
  // The deferred-add pattern means these only exist on PW2 / supported sites,
  // so we render the section only when at least one block is discovered.
  const states = hass && hass.states ? hass.states : {};
  const blockIndices = new Set();
  const blockRe = /^sensor\.power_sync_pw(\d+)_(soc|soh|capacity|voltage|temperature)$/;
  for (const key of Object.keys(states)) {
    const m = blockRe.exec(key);
    if (m) blockIndices.add(parseInt(m[1], 10));
  }
  if (blockIndices.size === 0) return null;

  const cards = [];
  const sortedIndices = Array.from(blockIndices).sort((a, b) => a - b);
  for (const i of sortedIndices) {
    const entities = [];
    let title = `Powerwall ${i}`;
    for (const [suffix, label, icon] of [
      ['soc', 'SOC', 'mdi:battery'],
      ['soh', 'State of Health', 'mdi:battery-heart'],
      ['capacity', 'Capacity', 'mdi:battery-high'],
      ['voltage', 'Voltage', 'mdi:flash'],
      ['temperature', 'Temperature', 'mdi:thermometer'],
    ]) {
      const id = `sensor.power_sync_pw${i}_${suffix}`;
      if (states[id] && states[id].state !== 'unavailable') {
        title = states[id].attributes?.pack_label || title;
        entities.push({ entity: id, name: label, icon });
      }
    }
    if (entities.length > 0) {
      cards.push({
        type: 'entities',
        title,
        show_header_toggle: false,
        entities,
      });
    }
  }
  if (cards.length === 0) return null;
  return { type: 'vertical-stack', cards };
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

function _flowPower(e, hasE) {
  const candidates = [
    ['flow_power_price', 'Import Price'],
    ['flow_power_export_price', 'Export Price'],
    ['flow_power_twap', 'TWAP 30-Day Average'],
    ['flow_power_network_tariff', 'Network Tariff'],
    ['fp_account_pea', 'Portal PEA'],
    ['fp_account_pea_30d', 'Portal PEA 30-Day'],
    ['fp_account_lwap', 'Portal LWAP'],
    ['fp_account_twap', 'Portal TWAP'],
    ['fp_account_dlf', 'DLF'],
    ['fp_account_avg_usage', 'Average Demand'],
    ['fp_account_max_usage', 'Max Demand'],
  ];
  const entities = candidates
    .filter(([key]) => !hasE || hasE(key))
    .map(([key, name]) => ({ entity: e(key), name }));
  if (entities.length === 0) return null;

  return {
    type: 'entities',
    title: 'Flow Power Pricing',
    show_header_toggle: false,
    entities,
  };
}

function _globirdProvider(findProviderSensor) {
  const candidates = [
    ['latest_data_status', 'Latest Data Status'],
    ['latest_data_date', 'Latest Data Date'],
    ['latest_day_usage', 'Latest Day Usage'],
    ['latest_day_solar_export', 'Latest Day Export'],
    ['latest_day_cost', 'Latest Day Cost'],
    ['balance', 'Balance'],
    ['latest_invoice', 'Latest Invoice'],
    ['zerohero_status', 'ZeroHero Status'],
    ['billing_period_cost', 'Billing Period Cost'],
    ['billing_period_days', 'Billing Period Days'],
    ['expected_month_cost', 'Expected Monthly Cost'],
  ];
  const entities = candidates
    .map(([suffix, name]) => {
      const entity = findProviderSensor('globird', suffix);
      return entity ? { entity, name } : null;
    })
    .filter(Boolean);
  if (entities.length === 0) return null;

  return {
    type: 'entities',
    title: 'GloBird Pricing',
    show_header_toggle: false,
    entities,
  };
}

// ─── Registration ────────────────────────────────────────────

if (!customElements.get('ll-strategy-dashboard-power-sync-strategy')) {
  customElements.define('ll-strategy-dashboard-power-sync-strategy', PowerSyncStrategy);
}
