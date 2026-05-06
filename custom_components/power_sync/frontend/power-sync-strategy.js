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
  }

  setConfig(config) {
    this._config = config;
    this._historyCache.clear();
    this._historyRequestKey = null;
    this._scheduleRender();
  }

  set hass(hass) {
    this._hass = hass;
    this._scheduleRender();
  }

  getCardSize() {
    return 4;
  }

  connectedCallback() {
    if (!this._resizeObserver && 'ResizeObserver' in window) {
      this._resizeObserver = new ResizeObserver(() => this._scheduleRender());
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

  _render() {
    if (!this._config || !this._hass) return;

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

    const configuredYMultiplier = config.yMultiplier || 1;
    if (configuredYMultiplier !== 1) {
      allSeries = allSeries.map(series => ({
        ...series,
        data: series.data.map(([t, v]) => [t, v * configuredYMultiplier]),
      }));
    }

    const box = this.getBoundingClientRect();
    const W = Math.max(320, Math.round(box.width || config.width || 640));
    const compact = W < 520;
    const H = Math.max(190, Math.round(config.height || (compact ? 220 : 250)));
    const pad = {
      top: 16,
      right: compact ? 12 : 20,
      bottom: compact ? 34 : 42,
      left: compact ? 42 : 56,
    };
    const chartW = W - pad.left - pad.right;
    const chartH = H - pad.top - pad.bottom;

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

    const yMultiplier = 1;
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
      const label = tick.toFixed(tick === Math.round(tick) ? 0 : 1)
        + (unit ? `${compactUnit ? '' : ' '}${unit}` : '');
      svg += `<text x="${pad.left - 8}" y="${y + 4}" text-anchor="end" font-size="${compact ? 10 : 11}" fill="var(--secondary-text-color, #888)">${this._escSvg(label)}</text>`;
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

      if (series.fill) {
        const baseline = yScale(Math.max(0, yMin));
        const first = series.data[0];
        const last = series.data[series.data.length - 1];
        const fillD = pathD + `L${xScale(last[0])},${baseline}L${xScale(first[0])},${baseline}Z`;
        svg += `<path d="${fillD}" fill="${series.color}" opacity="${series.fillOpacity ?? 0.16}"/>`;
      }

      svg += `<path d="${pathD}" fill="none" stroke="${series.color}" stroke-width="${series.strokeWidth || 2.25}" stroke-linejoin="round" stroke-linecap="round"/>`;

      const last = series.data[series.data.length - 1];
      if (last) {
        svg += `<circle cx="${xScale(last[0])}" cy="${yScale(last[1] * yMultiplier)}" r="${compact ? 3 : 4}" fill="${series.color}" stroke="var(--ha-card-background, var(--card-background-color, white))" stroke-width="2"/>`;
      }
    }

    if (mode === 'forecast' || mode === 'history') {
      const nowX = xScale(Date.now());
      if (nowX >= pad.left && nowX <= W - pad.right) {
        svg += `<line x1="${nowX}" y1="${pad.top}" x2="${nowX}" y2="${pad.top + chartH}" stroke="var(--primary-color, #03a9f4)" stroke-width="1" stroke-dasharray="4,2" opacity="0.6"/>`;
      }
    }

    const title = config.title || '';
    const legend = allSeries.map((s) => this._legendItem(s, yMultiplier, config)).join('');
    const empty = allSeries.every(s => s.data.length === 0);
    const accent = allSeries.find(s => s.color)?.color || 'var(--primary-color, #03a9f4)';

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
          font-size: 11.5px;
          line-height: 1.2;
          white-space: nowrap;
          padding: 3px 6px;
          border-radius: 999px;
          background: color-mix(in srgb, var(--secondary-background-color, transparent) 65%, transparent);
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
        }
        .tooltip {
          position: absolute;
          min-width: 140px;
          max-width: min(240px, calc(100% - 16px));
          padding: 8px 10px;
          border-radius: 8px;
          background: color-mix(in srgb, var(--ha-card-background, var(--card-background-color, white)) 88%, black);
          color: var(--primary-text-color, #333);
          box-shadow: 0 8px 22px rgba(0, 0, 0, 0.28);
          border: 1px solid var(--divider-color, rgba(255,255,255,0.18));
          font-size: 12px;
          line-height: 1.35;
          opacity: 0;
          pointer-events: none;
          transform: translate(-50%, -100%);
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

    if (!empty) {
      this._attachTooltip({
        allSeries,
        chartW,
        config,
        pad,
        spanHours,
        W,
        xMax,
        xMin,
      });
    }
  }

  _attachTooltip({ allSeries, chartW, config, pad, spanHours, W, xMax, xMin }) {
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
          <span class="tooltip-value">${this._escHtml(this._formatValue(point[1], config.yUnit, config.yUnitCompact))}</span>
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

  _formatTooltipTime(timestamp, spanHours) {
    const d = new Date(timestamp);
    const options = spanHours > 24
      ? { weekday: 'short', hour: '2-digit', minute: '2-digit' }
      : { hour: '2-digit', minute: '2-digit' };
    return d.toLocaleString([], options);
  }

  _legendItem(series, yMultiplier, config) {
    const last = this._lastValue(series.data);
    const value = last === null ? '' : this._formatValue(last * yMultiplier, config.yUnit, config.yUnitCompact);
    return `
      <span class="legend-item">
        <span class="swatch" style="background:${series.color}"></span>
        <span>${this._escHtml(series.name || '')}</span>
        ${value ? `<span class="value">${this._escHtml(value)}</span>` : ''}
      </span>
    `;
  }

  _lastValue(data) {
    for (let i = data.length - 1; i >= 0; i--) {
      const value = Number(data[i][1]);
      if (Number.isFinite(value)) return value;
    }
    return null;
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
    const spanHours = config.historyHours || 24;
    const now = Date.now();
    const start = now - spanHours * 3600000;
    return (config.series || []).map(s => {
      const stateObj = s.entity ? hass.states[s.entity] : null;
      const cached = this._historyCache.get(s.entity) || [];
      const data = cached.length
        ? cached
        : this._statePoint(stateObj, now);
      return { name: s.name, color: s.color, fill: !!s.fill, strokeWidth: s.strokeWidth, data: data.filter(([t]) => t >= start && t <= now) };
    });
  }

  _statePoint(stateObj, now) {
    const value = Number(stateObj?.state);
    if (!Number.isFinite(value)) return [];
    const changed = Date.parse(stateObj?.last_changed || stateObj?.last_updated || '');
    const t = Number.isFinite(changed) ? changed : now;
    return [[Math.max(t, now - 3600000), value], [now, value]];
  }

  async _loadHistoryData(config, hass) {
    if (!hass || typeof hass.callApi !== 'function') return;
    const entities = (config.series || []).map(s => s.entity).filter(Boolean);
    if (!entities.length) return;
    const spanHours = config.historyHours || 24;
    const end = new Date();
    const start = new Date(end.getTime() - spanHours * 3600000);
    const key = `${entities.join(',')}|${spanHours}|${Math.floor(end.getTime() / 300000)}`;
    if (this._historyRequestKey === key) return;
    this._historyRequestKey = key;

    try {
      const query = new URLSearchParams({
        filter_entity_id: entities.join(','),
        end_time: end.toISOString(),
        no_attributes: '1',
      });
      const response = await hass.callApi('GET', `history/period/${start.toISOString()}?${query.toString()}`);
      const next = new Map();
      if (Array.isArray(response)) {
        for (const series of response) {
          if (!Array.isArray(series) || series.length === 0) continue;
          const entityId = series[0]?.entity_id;
          if (!entityId) continue;
          const points = series
            .map((p) => [Date.parse(p.last_changed || p.last_updated), Number(p.state)])
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
    this._storageKey = 'power-sync-dashboard-layout-v1';
  }

  setConfig(config) {
    this._config = config;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._buildLayout();
    for (const c of this._cards) c.hass = hass;
    this._scheduleLayout();
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
    const portrait = window.matchMedia?.('(orientation: portrait)')?.matches;
    if (width < 760 || (portrait && width < 1040)) return 1;
    if (width < 1280) return 2;
    return 3;
  }

  _flattenCards() {
    const columns = this._config?.columns || [];
    const ordered = columns.length === 3 ? [columns[1], columns[0], columns[2]] : columns;
    return ordered.flatMap(column => column || []);
  }

  _cardKey(cardConfig, index) {
    const parts = [
      cardConfig.type,
      cardConfig.title,
      cardConfig.entity,
      cardConfig.name,
      cardConfig.card?.type,
      cardConfig.cards?.map(card => card.title || card.entity || card.type).join('|'),
    ].filter(Boolean);
    return `${index}:${parts.join(':')}`;
  }

  _applySavedOrder() {
    let saved;
    try {
      saved = JSON.parse(localStorage.getItem(this._storageKey) || '[]');
    } catch (_) {
      saved = [];
    }
    if (!Array.isArray(saved) || saved.length === 0) return;
    const rank = new Map(saved.map((key, index) => [key, index]));
    this._items.sort((a, b) => {
      const aRank = rank.has(a.dataset.key) ? rank.get(a.dataset.key) : Number.MAX_SAFE_INTEGER;
      const bRank = rank.has(b.dataset.key) ? rank.get(b.dataset.key) : Number.MAX_SAFE_INTEGER;
      return aRank - bRank || Number(a.dataset.defaultIndex) - Number(b.dataset.defaultIndex);
    });
  }

  _saveOrder() {
    try {
      localStorage.setItem(this._storageKey, JSON.stringify(this._items.map(item => item.dataset.key)));
    } catch (_) {}
  }

  _setCustomizing(enabled) {
    this._customizing = enabled;
    this.shadowRoot.querySelector('.toolbar')?.classList.toggle('active', enabled);
    for (const item of this._items) {
      item.draggable = enabled;
      item.classList.toggle('customizing', enabled);
    }
  }

  _resetOrder() {
    try { localStorage.removeItem(this._storageKey); } catch (_) {}
    this._items.sort((a, b) => Number(a.dataset.defaultIndex) - Number(b.dataset.defaultIndex));
    this._saveOrder();
    this._scheduleLayout();
  }

  _itemAtPoint(x, y) {
    return this._items.find(item => {
      const rect = item.getBoundingClientRect();
      return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
    });
  }

  _moveDragItem(targetItem, clientY) {
    if (!this._dragItem || !targetItem || this._dragItem === targetItem) return;
    const targetIndex = this._items.indexOf(targetItem);
    const dragIndex = this._items.indexOf(this._dragItem);
    if (targetIndex < 0 || dragIndex < 0) return;
    const rect = targetItem.getBoundingClientRect();
    const insertAfter = clientY > rect.top + rect.height / 2;
    this._items.splice(dragIndex, 1);
    const nextTargetIndex = this._items.indexOf(targetItem);
    this._items.splice(nextTargetIndex + (insertAfter ? 1 : 0), 0, this._dragItem);
    this._balanceLayout();
  }

  _balanceLayout() {
    if (!this._items.length) return;
    const count = Math.min(this._columnCount(), this._items.length);
    if (this._lanes.length !== count) {
      this._rebuildLanes(count);
    }

    const heights = new Array(count).fill(0);
    for (const item of this._items) {
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
        cursor: grab;
        outline: 1px dashed color-mix(in srgb, var(--primary-color, #03a9f4) 60%, transparent);
        outline-offset: 3px;
        border-radius: 10px;
      }
      .item.customizing::before {
        content: 'Drag';
        position: absolute;
        top: 8px;
        right: 8px;
        z-index: 5;
        padding: 4px 7px;
        border-radius: 999px;
        background: color-mix(in srgb, var(--primary-color, #03a9f4) 85%, black);
        color: white;
        font-size: 10px;
        font-weight: 700;
        pointer-events: none;
      }
      .item.dragging {
        opacity: 0.55;
        cursor: grabbing;
        touch-action: none;
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
      <button class="reset" type="button">Reset layout</button>
    `;
    toolbar.querySelector('.toggle').addEventListener('click', () => this._setCustomizing(!this._customizing));
    toolbar.querySelector('.reset').addEventListener('click', () => this._resetOrder());
    root.appendChild(toolbar);

    const grid = document.createElement('div');
    grid.className = 'grid';
    root.appendChild(grid);

    if ('ResizeObserver' in window) {
      this._resizeObserver = new ResizeObserver(() => this._scheduleLayout());
      this._resizeObserver.observe(this);
    }

    let helpers;
    try { helpers = await window.loadCardHelpers(); } catch (_) {}

    for (const [index, cardConfig] of this._flattenCards().entries()) {
      const item = document.createElement('div');
      item.className = 'item';
      item.dataset.defaultIndex = String(index);
      item.dataset.key = this._cardKey(cardConfig, index);
      item.addEventListener('dragstart', (event) => {
        if (!this._customizing) return;
        this._dragItem = item;
        item.classList.add('dragging');
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', item.dataset.key);
      });
      item.addEventListener('dragend', () => {
        item.classList.remove('dragging');
        this._dragItem = null;
        this._saveOrder();
      });
      item.addEventListener('dragover', (event) => {
        if (!this._customizing || !this._dragItem || this._dragItem === item) return;
        event.preventDefault();
        this._moveDragItem(item, event.clientY);
      });
      item.addEventListener('pointerdown', (event) => {
        if (!this._customizing || event.button > 0) return;
        this._pointerDrag = {
          active: false,
          item,
          startX: event.clientX,
          startY: event.clientY,
        };
        item.setPointerCapture?.(event.pointerId);
      });
      item.addEventListener('pointermove', (event) => {
        if (!this._pointerDrag || this._pointerDrag.item !== item) return;
        const distance = Math.hypot(event.clientX - this._pointerDrag.startX, event.clientY - this._pointerDrag.startY);
        if (!this._pointerDrag.active && distance < 8) return;
        if (!this._pointerDrag.active) {
          this._pointerDrag.active = true;
          this._dragItem = item;
          item.classList.add('dragging');
        }
        event.preventDefault();
        this._moveDragItem(this._itemAtPoint(event.clientX, event.clientY), event.clientY);
      });
      item.addEventListener('pointerup', () => {
        if (!this._pointerDrag || this._pointerDrag.item !== item) return;
        item.classList.remove('dragging');
        this._pointerDrag = null;
        this._dragItem = null;
        this._saveOrder();
      });
      item.addEventListener('pointercancel', () => {
        if (!this._pointerDrag || this._pointerDrag.item !== item) return;
        item.classList.remove('dragging');
        this._pointerDrag = null;
        this._dragItem = null;
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
      this._items.push(item);
    }

    this._applySavedOrder();
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
    // Gated on power_sync_backup_reserve or power_sync_operation_mode — these
    // are only created for Tesla setups. Without this guard, findEntity's broad
    // suffix-match fallback picks up unrelated entities from GoodWe, Sigenergy,
    // etc. and incorrectly renders the Tesla section for non-Tesla users.
    {
      const _s = hass.states || {};
      const _hasTesla = !!(
        _s['number.power_sync_backup_reserve'] ||
        _s['select.power_sync_operation_mode']
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
      left.push(_optimizerStatus(e, hasE('optimization_force_charge_windows')));
    }

    // --- Center Column: Power Flow ---
    if (hasTeslaFlow && hasE('solar_power')) {
      center.push(_teslaStyleFlow(e, hass, findSensor));
    } else if (hasFlowCard && hasE('solar_power')) {
      center.push(_powerFlow(e));
    }

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

    // --- Left Column: Flow Power ---
    if (hasE('flow_power_price')) {
      left.push(_flowPower(e));
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

function _optimizerStatus(e, showForceChargeWindows = false) {
  const statusEntity = e('optimization_status');
  const nextEntity = e('optimization_next_action');
  const forceChargeWindowsEntity = e('optimization_force_charge_windows');
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

  if (showForceChargeWindows) {
    cards.push({
      type: 'entities',
      show_header_toggle: false,
      entities: [{
        entity: forceChargeWindowsEntity,
        name: 'Future Force Charge',
        icon: 'mdi:battery-clock',
      }],
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
    const evBattery = e('ev_battery_level');
    if (hass.states[evBattery]) {
      config.entities.ev_battery = evBattery;
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
    series: [
      { key: 'buy', name: 'Buy Price', color: '#FF9800' },
      { key: 'sell', name: 'Sell Price', color: '#4CAF50' },
    ],
  };
}

function _lpForecastSummary(e, has) {
  const cards = [
    { type: 'entity', entity: e('lp_solar_forecast'), name: 'Solar Forecast', icon: 'mdi:solar-power-variant' },
    { type: 'entity', entity: e('lp_load_forecast'), name: 'Load Forecast', icon: 'mdi:home-lightning-bolt' },
  ];
  if (has(e('lp_import_price_forecast'))) {
    cards.push({ type: 'entity', entity: e('lp_import_price_forecast'), name: 'Import Price Avg', icon: 'mdi:cash-clock' });
  }
  if (has(e('lp_export_price_forecast'))) {
    cards.push({ type: 'entity', entity: e('lp_export_price_forecast'), name: 'Export Price Avg', icon: 'mdi:cash-clock' });
  }
  return { type: 'horizontal-stack', cards };
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
  const entities = [];
  const resolveSensor = (names, fallback) =>
    (typeof findSensor === 'function' ? findSensor(names) : null) || e(fallback);
  const add = (entity, name, icon) => {
    if (!entity || !hass?.states?.[entity]) return;
    const row = { entity, name };
    if (icon) row.icon = icon;
    entities.push(row);
  };

  add(resolveSensor(['pv1_power', 'pv_1_power', 'pv_power_1', 'ppv1'], 'pv1_power'), 'PV1 Power', 'mdi:solar-panel');
  add(resolveSensor(['pv1_voltage', 'pv_1_voltage', 'pv_voltage_1', 'vpv1'], 'pv1_voltage'), 'PV1 Voltage', 'mdi:sine-wave');
  add(resolveSensor(['pv1_current', 'pv_1_current', 'pv_current_1', 'ipv1'], 'pv1_current'), 'PV1 Current', 'mdi:current-dc');
  add(resolveSensor(['pv2_power', 'pv_2_power', 'pv_power_2', 'ppv2'], 'pv2_power'), 'PV2 Power', 'mdi:solar-panel');
  add(resolveSensor(['pv2_voltage', 'pv_2_voltage', 'pv_voltage_2', 'vpv2'], 'pv2_voltage'), 'PV2 Voltage', 'mdi:sine-wave');
  add(resolveSensor(['pv2_current', 'pv_2_current', 'pv_current_2', 'ipv2'], 'pv2_current'), 'PV2 Current', 'mdi:current-dc');
  add(resolveSensor(['pv3_power', 'pv_3_power', 'pv_power_3', 'ppv3'], 'pv3_power'), 'PV3 Power', 'mdi:solar-panel');
  add(resolveSensor(['pv3_voltage', 'pv_3_voltage', 'pv_voltage_3', 'vpv3'], 'pv3_voltage'), 'PV3 Voltage', 'mdi:sine-wave');
  add(resolveSensor(['pv3_current', 'pv_3_current', 'pv_current_3', 'ipv3'], 'pv3_current'), 'PV3 Current', 'mdi:current-dc');

  add(e('ct2_power'), 'CT2 Power', 'mdi:current-ac');
  add(e('work_mode'), 'Work Mode', 'mdi:cog');
  add(e('min_soc'), 'Min SOC', 'mdi:battery-low');
  add(e('daily_battery_charge_foxess'), 'Daily Charge', 'mdi:battery-charging');
  add(e('daily_battery_discharge_foxess'), 'Daily Discharge', 'mdi:battery-arrow-down');

  if (entities.length === 0) return null;

  return {
    type: 'entities',
    title: 'PV String Details',
    show_header_toggle: false,
    entities,
  };
}

function _batteryHealth(e, hass) {
  const healthEntity = e('battery_health');

  // Determine how many individual battery gauges to show. Read battery_count from
  // current state at render time so the grid expands for stacked PW3 systems.
  const stateObj = hass?.states?.[healthEntity];
  const batteryCount = Number(stateObj?.attributes?.battery_count || 0);
  // Show at least 1 individual gauge slot, cap at 8. If we have no count data yet,
  // default to 3 so the card isn't empty on first load.
  const numSlots = batteryCount > 0 ? Math.min(batteryCount, 8) : 3;

  const healthGauge = (name, attrPath) => ({
    type: 'custom:button-card',
    entity: healthEntity,
    name,
    show_icon: false,
    show_name: true,
    show_state: true,
    state_display: `[[[
      const v = ${attrPath};
      if (v == null || ['unknown','unavailable','none'].includes(String(v).toLowerCase())) return '';
      const n = Number(v);
      if (!Number.isFinite(n)) return '';
      return n.toFixed(1) + ' %';
    ]]]`,
    styles: {
      card: [
        { height: '70px' },
        { 'border-radius': '12px' },
        { padding: '6px' },
        {
          display: `[[[
            const v = ${attrPath};
            if (v == null || ['unknown','unavailable','none'].includes(String(v).toLowerCase())) return 'none';
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
        { 'font-size': '18px' },
        { 'font-weight': '800' },
        { 'margin-top': '2px' },
      ],
    },
  });

  // Build individual battery gauge cards using the same reconciled pack labels
  // as the backend sensors. Legacy follower/expansion attributes are fallback
  // only for already-restored data that has not been rescanned yet.
  const individualGauges = [];
  for (let n = 1; n <= numSlots; n++) {
    const attrPath = `states['${healthEntity}']?.attributes?.battery_${n}_health_percent`;
    const labelPath = `states['${healthEntity}']?.attributes?.battery_${n}_label`;
    const rolePath = `states['${healthEntity}']?.attributes?.battery_${n}_role`;
    const followerPath = `states['${healthEntity}']?.attributes?.battery_${n}_is_follower`;
    const expansionPath = `states['${healthEntity}']?.attributes?.battery_${n}_is_expansion`;
    individualGauges.push({
      type: 'custom:button-card',
      entity: healthEntity,
      show_icon: false,
      show_name: true,
      show_state: true,
      name: `[[[
        const label = ${labelPath};
        if (label) return label;
        const role = String(${rolePath} || '').toLowerCase();
        const isFollower = ${followerPath};
        const isExpansion = ${expansionPath};
        if (role === 'powerwall') return 'Powerwall ${n}';
        if (role === 'leader') return 'Leader PW3';
        if (role === 'follower' || isFollower) return 'Follower PW3';
        if (role === 'expansion' || isExpansion) return 'Expansion Pack ${n}';
        return 'Powerwall ${n}';
      ]]]`,
      state_display: `[[[
        const v = ${attrPath};
        if (v == null || ['unknown','unavailable','none'].includes(String(v).toLowerCase())) return '';
        const n = Number(v);
        if (!Number.isFinite(n)) return '';
        return n.toFixed(1) + ' %';
      ]]]`,
      styles: {
        card: [
          { height: '70px' },
          { 'border-radius': '12px' },
          { padding: '6px' },
          {
            display: `[[[
              const v = ${attrPath};
              if (v == null || ['unknown','unavailable','none'].includes(String(v).toLowerCase())) return 'none';
              const num = Number(v);
              return Number.isFinite(num) ? 'block' : 'none';
            ]]]`,
          },
        ],
        name: [
          { 'font-weight': '700' },
          { 'font-size': '13px' },
        ],
        state: [
          { 'font-size': '18px' },
          { 'font-weight': '800' },
          { 'margin-top': '2px' },
        ],
      },
    });
  }

  // Grid columns: Overall + individual gauges. Use 4 columns for ≤3 slots, else match count.
  const gridColumns = numSlots <= 3 ? 4 : Math.min(numSlots + 1, 5);

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
        columns: gridColumns,
        square: false,
        cards: [
          healthGauge('Overall', `states['${healthEntity}']?.state`),
          ...individualGauges,
        ],
      },
      {
        type: 'markdown',
        content: `{% set source = state_attr('${healthEntity}', 'source') %}
{% set original = state_attr('${healthEntity}', 'original_capacity_kwh') %}
{% set current = state_attr('${healthEntity}', 'current_capacity_kwh') %}
{% set scan = state_attr('${healthEntity}', 'last_scan') %}
{% set soh = state_attr('${healthEntity}', 'state_of_health_percent') %}
{% set ns = namespace(has_follower=false) %}
{%- for n in range(1, 9) %}
{%- set role = state_attr('${healthEntity}', 'battery_' ~ n ~ '_role') %}
{%- if role == 'follower' or state_attr('${healthEntity}', 'battery_' ~ n ~ '_is_follower') %}
{%- set ns.has_follower = true %}
{%- endif %}
{%- endfor %}
{% set source_label = 'local gateway' if source == 'ha_local_tedapi' else 'Fleet API relay' if source == 'ha_fleet_api_relay' else 'mobile local scan' if source == 'mobile_app_tedapi' else 'mobile cloud RSA' if source == 'mobile_app_cloud_rsa' else source %}
{%- if source in ('mobile_app_tedapi', 'mobile_app', 'fleet_api', 'ha_local_tedapi', 'ha_fleet_api_relay', 'mobile_app_cloud_rsa') %}
**Capacity:** {{ current }} / {{ original }} kWh | **Last scan:** {{ scan[:10] if scan else 'N/A' }} | **Source:** {{ source_label }}
{%- if ns.has_follower %} *(follower capacity inferred from aggregate)*{%- endif %}
{%- elif source == 'inverter_modbus' %}
**State of Health:** {{ soh }}% (from inverter)
{%- elif states('${healthEntity}') not in ['unavailable', 'unknown'] %}
**Health:** {{ states('${healthEntity}') }}%
{%- else %}
No battery health data available yet.
{%- endif %}`,
      },
    ],
  };
}

function _combinedEnergyChart(e, hasHome) {
  const series = [
    { entity: e('solar_power'), name: 'Solar', color: '#FFD700', fill: true },
    { entity: e('grid_power'), name: 'Grid', color: '#F44336' },
    { entity: e('battery_power'), name: 'Battery', color: '#2196F3' },
  ];
  if (hasHome) {
    series.push({ entity: e('home_load'), name: 'Home', color: '#9C27B0' });
  }
  return {
    type: 'custom:power-sync-chart',
    title: 'Energy - 24 Hours',
    mode: 'history',
    historyHours: 24,
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

if (!customElements.get('ll-strategy-dashboard-power-sync-strategy')) {
  customElements.define('ll-strategy-dashboard-power-sync-strategy', PowerSyncStrategy);
}
