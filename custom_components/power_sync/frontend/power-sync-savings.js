/**
 * PowerSync Savings Card
 *
 * A Lovelace custom card that displays battery optimization savings,
 * cost breakdowns, energy stats, and the latest optimizer decision.
 *
 * Usage:
 *   type: custom:power-sync-savings
 *   entity_prefix: power_sync   # optional, default auto-detect
 */

class PowerSyncSavingsCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._selectedPeriod = 'today';
    this._renderScheduled = false;
  }

  static getConfigElement() {
    return undefined;
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = config;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._renderScheduled) {
      this._renderScheduled = true;
      requestAnimationFrame(() => {
        this._renderScheduled = false;
        this._render();
      });
    }
  }

  getCardSize() {
    return 5;
  }

  // ─── Helpers ───────────────────────────────────────────────

  _getPrefix() {
    return this._config.entity_prefix || 'power_sync';
  }

  _getEntity(entityId) {
    if (!this._hass) return null;
    const state = this._hass.states[entityId];
    if (!state || state.state === 'unavailable' || state.state === 'unknown') {
      return null;
    }
    return state;
  }

  _formatCurrency(value) {
    const num = parseFloat(value);
    if (isNaN(num)) return 'N/A';
    const sign = num >= 0 ? '' : '-';
    return `${sign}$${Math.abs(num).toFixed(2)}`;
  }

  _formatKwh(value) {
    const num = parseFloat(value);
    if (isNaN(num)) return 'N/A';
    return `${num.toFixed(1)} kWh`;
  }

  _formatPercent(value) {
    const num = parseFloat(value);
    if (isNaN(num)) return 'N/A';
    return `${num.toFixed(1)}%`;
  }

  _formatTimestamp(ts) {
    if (!ts) return 'N/A';
    try {
      const d = new Date(ts);
      if (isNaN(d.getTime())) return 'N/A';
      const hours = d.getHours();
      const mins = String(d.getMinutes()).padStart(2, '0');
      const ampm = hours >= 12 ? 'PM' : 'AM';
      const h12 = hours % 12 || 12;
      return `${h12}:${mins} ${ampm}`;
    } catch (_e) {
      return 'N/A';
    }
  }

  _getSavingsForPeriod(period) {
    const prefix = this._getPrefix();
    const periodMap = {
      today: `sensor.${prefix}_savings_today`,
      week: `sensor.${prefix}_savings_this_week`,
      month: `sensor.${prefix}_savings_this_month`,
      lifetime: `sensor.${prefix}_savings_lifetime`,
    };
    return this._getEntity(periodMap[period]);
  }

  _getSavingsValue(entity) {
    if (!entity) return NaN;
    const attrs = entity.attributes || {};
    // Prefer total_savings attribute, fall back to entity state
    if (attrs.total_savings !== undefined) return parseFloat(attrs.total_savings);
    return parseFloat(entity.state);
  }

  // ─── Action Badge ──────────────────────────────────────────

  _getActionColor(action) {
    if (!action) return '#9e9e9e';
    const lower = String(action).toLowerCase().replace(/[_\s-]/g, '');
    if (lower.includes('charg')) return '#2196f3';
    if (lower.includes('export')) return '#4caf50';
    if (lower.includes('selfconsumption') || lower.includes('self')) return '#ff9800';
    return '#9e9e9e'; // idle / unknown
  }

  _getActionLabel(action) {
    if (!action) return 'Unknown';
    const lower = String(action).toLowerCase().replace(/[_-]/g, ' ');
    // Capitalize first letter of each word
    return lower.replace(/\b\w/g, (c) => c.toUpperCase());
  }

  _escapeHtml(str) {
    if (str == null) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
  }

  // ─── Trend Arrow ───────────────────────────────────────────

  _getTrendArrow(savings) {
    if (isNaN(savings)) return '';
    if (savings > 0) {
      return `<span class="trend trend-up" title="Saving money">&#9650;</span>`;
    } else if (savings < 0) {
      return `<span class="trend trend-down" title="Costing more">&#9660;</span>`;
    }
    return `<span class="trend trend-neutral">&#9644;</span>`;
  }

  // ─── Render ────────────────────────────────────────────────

  _render() {
    if (!this._hass) return;

    const prefix = this._getPrefix();
    const period = this._selectedPeriod;

    // Fetch entities
    const periodEntity = this._getSavingsForPeriod(period);
    const todayEntity = this._getEntity(`sensor.${prefix}_savings_today`);
    const costEntity = this._getEntity(`sensor.${prefix}_daily_cost_total`);
    const baselineEntity = this._getEntity(`sensor.${prefix}_daily_baseline`);
    const roiEntity = this._getEntity(`sensor.${prefix}_roi_percentage`);
    const decisionEntity = this._getEntity(`sensor.${prefix}_last_decision`);

    // Savings value
    const savingsValue = this._getSavingsValue(periodEntity);
    const savingsColor = isNaN(savingsValue)
      ? 'var(--secondary-text-color)'
      : savingsValue >= 0
        ? '#4caf50'
        : '#f44336';

    // Today attributes for breakdown
    const todayAttrs = todayEntity ? todayEntity.attributes || {} : {};
    const importCost = parseFloat(todayAttrs.import_cost);
    const exportEarnings = parseFloat(todayAttrs.export_earnings);
    const netCost = parseFloat(todayAttrs.net_cost);
    const baselineCost = parseFloat(todayAttrs.baseline_cost);
    const importKwh = parseFloat(todayAttrs.import_kwh);
    const exportKwh = parseFloat(todayAttrs.export_kwh);
    const batteryChargeKwh = parseFloat(todayAttrs.battery_charge_kwh);
    const batteryDischargeKwh = parseFloat(todayAttrs.battery_discharge_kwh);
    const batteryCycled =
      !isNaN(batteryChargeKwh) && !isNaN(batteryDischargeKwh)
        ? batteryChargeKwh + batteryDischargeKwh
        : NaN;

    // Cost breakdown bar widths
    let importBarPct = 0;
    let exportBarPct = 0;
    if (!isNaN(importCost) && !isNaN(exportEarnings)) {
      const total = Math.abs(importCost) + Math.abs(exportEarnings);
      if (total > 0) {
        importBarPct = (Math.abs(importCost) / total) * 100;
        exportBarPct = (Math.abs(exportEarnings) / total) * 100;
      }
    }

    // Savings vs baseline
    const savedVsBaseline =
      !isNaN(baselineCost) && !isNaN(netCost) ? baselineCost - netCost : NaN;

    // Decision attributes (escaped to prevent XSS via innerHTML)
    const decisionAttrs = decisionEntity ? decisionEntity.attributes || {} : {};
    const decisionAction = decisionAttrs.action || null;
    const decisionTimestamp = decisionAttrs.timestamp || null;
    const decisionReason = decisionEntity ? this._escapeHtml(decisionEntity.state) : null;

    // ROI
    const roiValue = roiEntity ? parseFloat(roiEntity.state) : NaN;

    // Period labels
    const periodLabels = {
      today: 'Today',
      week: 'Week',
      month: 'Month',
      lifetime: 'All Time',
    };

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
        }
        ha-card {
          padding: 16px;
          border-radius: var(--ha-card-border-radius, 12px);
          background: var(--card-background-color, var(--ha-card-background, #fff));
          color: var(--primary-text-color, #333);
          font-family: var(--paper-font-body1_-_font-family, 'Roboto', sans-serif);
        }

        /* Header */
        .header {
          display: flex;
          align-items: center;
          gap: 8px;
          margin-bottom: 16px;
        }
        .header svg {
          width: 24px;
          height: 24px;
          fill: var(--primary-text-color);
          flex-shrink: 0;
        }
        .header h2 {
          margin: 0;
          font-size: 1.1em;
          font-weight: 500;
          color: var(--primary-text-color);
        }

        /* Hero */
        .hero {
          text-align: center;
          margin-bottom: 16px;
        }
        .hero-value {
          font-size: 2.5em;
          font-weight: 700;
          line-height: 1.1;
        }
        .hero-label {
          font-size: 0.85em;
          color: var(--secondary-text-color);
          margin-top: 4px;
        }
        .trend {
          font-size: 0.5em;
          vertical-align: middle;
          margin-left: 4px;
        }
        .trend-up { color: #4caf50; }
        .trend-down { color: #f44336; }
        .trend-neutral { color: var(--secondary-text-color); }

        /* Period Tabs */
        .period-tabs {
          display: flex;
          justify-content: center;
          gap: 4px;
          margin-bottom: 16px;
        }
        .period-tab {
          padding: 6px 14px;
          border-radius: 20px;
          border: none;
          cursor: pointer;
          font-size: 0.85em;
          font-weight: 500;
          background: var(--secondary-background-color, rgba(0,0,0,0.06));
          color: var(--secondary-text-color);
          transition: background 0.2s, color 0.2s;
          font-family: inherit;
        }
        .period-tab:hover {
          background: var(--primary-color, #03a9f4);
          color: var(--text-primary-color, #fff);
          opacity: 0.8;
        }
        .period-tab.active {
          background: var(--primary-color, #03a9f4);
          color: var(--text-primary-color, #fff);
        }

        /* Divider */
        .divider {
          border: none;
          border-top: 1px solid var(--divider-color, rgba(0,0,0,0.12));
          margin: 12px 0;
        }

        /* Cost Breakdown */
        .cost-breakdown {
          margin-bottom: 16px;
        }
        .cost-breakdown h3 {
          font-size: 0.85em;
          font-weight: 500;
          color: var(--secondary-text-color);
          margin: 0 0 8px 0;
          text-transform: uppercase;
          letter-spacing: 0.5px;
        }
        .bar-container {
          display: flex;
          height: 16px;
          border-radius: 8px;
          overflow: hidden;
          background: var(--secondary-background-color, rgba(0,0,0,0.06));
          margin-bottom: 8px;
        }
        .bar-import {
          background: #f44336;
          height: 100%;
          transition: width 0.3s ease;
        }
        .bar-export {
          background: #4caf50;
          height: 100%;
          transition: width 0.3s ease;
        }
        .bar-labels {
          display: flex;
          justify-content: space-between;
          font-size: 0.8em;
          color: var(--secondary-text-color);
          margin-bottom: 4px;
        }
        .bar-label-import { color: #f44336; }
        .bar-label-export { color: #4caf50; }
        .cost-summary {
          font-size: 0.85em;
          color: var(--secondary-text-color);
          line-height: 1.6;
        }
        .cost-summary .highlight {
          font-weight: 600;
          color: #4caf50;
        }

        /* Stats Row */
        .stats-row {
          display: flex;
          justify-content: space-around;
          margin-bottom: 16px;
        }
        .stat-item {
          text-align: center;
          flex: 1;
        }
        .stat-value {
          font-size: 1.1em;
          font-weight: 600;
          color: var(--primary-text-color);
        }
        .stat-label {
          font-size: 0.75em;
          color: var(--secondary-text-color);
          margin-top: 2px;
        }

        /* Decision Feed */
        .decision-feed {
          margin-bottom: 16px;
        }
        .decision-feed h3 {
          font-size: 0.85em;
          font-weight: 500;
          color: var(--secondary-text-color);
          margin: 0 0 8px 0;
          text-transform: uppercase;
          letter-spacing: 0.5px;
        }
        .decision-row {
          display: flex;
          align-items: flex-start;
          gap: 10px;
        }
        .decision-badge {
          display: inline-block;
          padding: 3px 10px;
          border-radius: 12px;
          font-size: 0.75em;
          font-weight: 600;
          color: #fff;
          white-space: nowrap;
          flex-shrink: 0;
        }
        .decision-detail {
          flex: 1;
          min-width: 0;
        }
        .decision-reason {
          font-size: 0.85em;
          color: var(--primary-text-color);
          line-height: 1.4;
          word-break: break-word;
        }
        .decision-time {
          font-size: 0.75em;
          color: var(--secondary-text-color);
          margin-top: 2px;
        }

        /* ROI Progress */
        .roi-section {
          margin-bottom: 4px;
        }
        .roi-section h3 {
          font-size: 0.85em;
          font-weight: 500;
          color: var(--secondary-text-color);
          margin: 0 0 8px 0;
          text-transform: uppercase;
          letter-spacing: 0.5px;
        }
        .roi-bar-bg {
          height: 12px;
          border-radius: 6px;
          background: var(--secondary-background-color, rgba(0,0,0,0.06));
          overflow: hidden;
          margin-bottom: 4px;
        }
        .roi-bar-fill {
          height: 100%;
          border-radius: 6px;
          background: linear-gradient(90deg, #4caf50, #2196f3);
          transition: width 0.3s ease;
        }
        .roi-label {
          font-size: 0.8em;
          color: var(--secondary-text-color);
        }

        /* Unavailable */
        .unavailable {
          text-align: center;
          padding: 24px;
          color: var(--secondary-text-color);
          font-size: 0.9em;
        }

        /* Section hidden */
        .hidden {
          display: none;
        }
      </style>

      <ha-card>
        <div class="header">
          <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
            <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-1.85
              0-3.55-.63-4.9-1.69L12 14.5l4.9 3.81C15.55 19.37 13.85 20 12 20zM5.69 7.1
              C7.04 5.96 8.73 5.33 10.57 5.08L10 10.5l-4.31.6zm12.62 0l.69 4 -4.31-.6
              -.57-5.42c1.84.25 3.53.88 4.88 2.02z"
              opacity="0" />
            <path d="M19.83 7.5l-2.27 2.27A7.948 7.948 0 0 0 12 4c-2.08 0-3.98.8-5.41
              2.09L4.32 3.82A11.95 11.95 0 0 1 12 1c3.04 0 5.82 1.14 7.94 3l-.11 3.5zM12
              6c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6-2.69-6-6-6zm-1 10.5v-3l-2.5
              2.5c.69.69 1.52 1.22 2.5 1.5v-1zm3.5-1.5L12 12.5v3c.98-.28 1.81-.81
              2.5-1.5zM12 8c2.21 0 4 1.79 4 4s-1.79 4-4 4-4-1.79-4-4 1.79-4 4-4z"
              opacity="0" />
            <!-- Piggy bank icon (simplified) -->
            <path d="M20 8h-1.44a.61.61 0 0 0-.4.14C17.1 5.19 14.13 3 10.5 3 6.35 3 3 5.57
              3 8.79v.1C1.87 9.44 1 10.63 1 12c0 1.93 1.57 3.5 3.5 3.5h.17c1.23 1.52
              3.16 2.5 5.33 2.5s4.1-.98 5.33-2.5h.17c.64 0 1.24-.17 1.76-.47l1.24
              1.24 1.41-1.41-1.24-1.24c.3-.52.47-1.12.47-1.76 0-.87-.33-1.66-.87-2.27
              L20 8zm-9.5 8c-3.04 0-5.5-2.24-5.5-5s2.46-5 5.5-5S16 7.74 16 10.5 13.54
              16 10.5 16zm-1-7.5c-.55 0-1 .45-1 1s.45 1 1 1 1-.45 1-1-.45-1-1-1z" />
          </svg>
          <h2>PowerSync Savings</h2>
        </div>

        ${periodEntity === null && todayEntity === null
          ? `<div class="unavailable">No savings data available.<br>Ensure PowerSync sensors are configured.</div>`
          : `
            <!-- Hero -->
            <div class="hero">
              <div class="hero-value" style="color: ${savingsColor}">
                ${isNaN(savingsValue) ? 'N/A' : this._formatCurrency(savingsValue)}
                ${this._getTrendArrow(savingsValue)}
              </div>
              <div class="hero-label">${periodLabels[period]} savings</div>
            </div>

            <!-- Period Tabs -->
            <div class="period-tabs">
              ${['today', 'week', 'month', 'lifetime']
                .map(
                  (p) =>
                    `<button class="period-tab ${p === period ? 'active' : ''}" data-period="${p}">${periodLabels[p]}</button>`
                )
                .join('')}
            </div>

            <!-- Cost Breakdown (Today only) -->
            <div class="${period === 'today' && (!isNaN(importCost) || !isNaN(exportEarnings)) ? 'cost-breakdown' : 'hidden'}">
              <hr class="divider" />
              <h3>Cost Breakdown</h3>
              <div class="bar-labels">
                <span class="bar-label-import">Import: ${this._formatCurrency(importCost)}</span>
                <span class="bar-label-export">Export: ${this._formatCurrency(exportEarnings)}</span>
              </div>
              <div class="bar-container">
                <div class="bar-import" style="width: ${importBarPct}%"></div>
                <div class="bar-export" style="width: ${exportBarPct}%"></div>
              </div>
              <div class="cost-summary">
                Net: ${this._formatCurrency(netCost)}<br>
                Baseline: ${this._formatCurrency(baselineCost)}<br>
                ${!isNaN(savedVsBaseline)
                  ? `<span class="highlight">You saved ${this._formatCurrency(savedVsBaseline)} vs no battery</span>`
                  : ''}
              </div>
            </div>

            <!-- Stats Row (Today only) -->
            <div class="${period === 'today' && (!isNaN(importKwh) || !isNaN(exportKwh) || !isNaN(batteryCycled)) ? 'stats-row' : 'hidden'}">
              <hr class="divider" style="display:none" />
              <div class="stat-item">
                <div class="stat-value">${this._formatKwh(importKwh)}</div>
                <div class="stat-label">Import</div>
              </div>
              <div class="stat-item">
                <div class="stat-value">${this._formatKwh(exportKwh)}</div>
                <div class="stat-label">Export</div>
              </div>
              <div class="stat-item">
                <div class="stat-value">${this._formatKwh(batteryCycled)}</div>
                <div class="stat-label">Battery Cycled</div>
              </div>
            </div>

            <!-- Decision Feed -->
            <div class="${decisionEntity ? 'decision-feed' : 'hidden'}">
              <hr class="divider" />
              <h3>Last Decision</h3>
              <div class="decision-row">
                <span class="decision-badge" style="background: ${this._getActionColor(decisionAction)}">
                  ${this._getActionLabel(decisionAction)}
                </span>
                <div class="decision-detail">
                  <div class="decision-reason">${decisionReason || 'N/A'}</div>
                  <div class="decision-time">${this._formatTimestamp(decisionTimestamp)}</div>
                </div>
              </div>
            </div>

            <!-- ROI Progress -->
            <div class="${!isNaN(roiValue) ? 'roi-section' : 'hidden'}">
              <hr class="divider" />
              <h3>Return on Investment</h3>
              <div class="roi-bar-bg">
                <div class="roi-bar-fill" style="width: ${isNaN(roiValue) ? 0 : Math.min(roiValue, 100)}%"></div>
              </div>
              <div class="roi-label">${this._formatPercent(roiValue)} of system cost recovered</div>
            </div>
          `}
      </ha-card>
    `;

    // Attach tab click handlers
    const tabs = this.shadowRoot.querySelectorAll('.period-tab');
    tabs.forEach((tab) => {
      tab.addEventListener('click', (e) => {
        this._selectedPeriod = e.target.dataset.period;
        this._render();
      });
    });
  }
}

customElements.define('power-sync-savings', PowerSyncSavingsCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'power-sync-savings',
  name: 'PowerSync Savings',
  description:
    'Shows battery optimization savings, cost breakdown, and decision log',
  preview: true,
});
