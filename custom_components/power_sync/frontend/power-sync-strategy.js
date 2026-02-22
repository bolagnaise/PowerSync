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

class PowerSyncStrategy {
  static async generate(config, hass) {
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
      return s && s.state !== 'unavailable';
    };

    // Shorthand: resolve then check
    const hasE = (name) => has(e(name));

    const cards = [];

    // --- Price Gauges ---
    if (hasE('current_import_price')) {
      cards.push(_priceGauges(e));
    }

    // --- Battery Controls (always show if integration has battery sensors) ---
    if (hasE('battery_level') || hasE('battery_power')) {
      cards.push(_batteryControls());
    }

    // --- Optimizer Status ---
    if (hasE('optimization_status')) {
      cards.push(_optimizerStatus(e));
    }

    // --- Power Flow ---
    if (hasE('solar_power')) {
      cards.push(_powerFlow(e));
    }

    // --- Price Chart (Amber/Octopus 24h) ---
    if (hasE('current_import_price')) {
      cards.push(_priceChart(e));
    }

    // --- TOU Schedule ---
    if (hasE('tariff_schedule')) {
      cards.push(_touSchedule(e));
    }

    // --- LP Forecast Summary ---
    if (hasE('lp_solar_forecast')) {
      cards.push(_lpForecastSummary(e));
    }

    // --- LP Forecast Charts ---
    if (hasE('lp_solar_forecast')) {
      cards.push(_lpSolarLoadChart(e));
      cards.push(_lpPriceChart(e));
    }

    // --- Curtailment Status ---
    const hasDC = hasE('solar_curtailment');
    const hasAC = hasE('inverter_status');
    if (hasDC || hasAC) {
      cards.push(_curtailmentStatus(e, hasDC, hasAC));
    }

    // --- AC Inverter Controls ---
    if (hasAC) {
      cards.push(_acInverterControls(e));
    }

    // --- FoxESS Sensors ---
    if (hasE('pv1_power')) {
      cards.push(_foxessSensors(e));
    }

    // --- Battery Health ---
    if (hasE('battery_health')) {
      cards.push(_batteryHealth(e));
    }

    // --- Energy Charts ---
    if (hasE('solar_power')) {
      cards.push(_energyChart('Solar', e('solar_power'), '#FFD700', { yaxis: { min: 0 } }));
      cards.push(_energyChart('Battery', e('battery_power'), '#2196F3', {}));
      cards.push(_energyChart('Grid', e('grid_power'), '#F44336', {}));
    }
    if (hasE('home_load')) {
      cards.push(_energyChart('Home', e('home_load'), '#9C27B0', { yaxis: { min: 0 } }));
    }

    // --- Demand Charge ---
    if (hasE('in_demand_charge_period')) {
      cards.push(_demandCharge(e));
    }

    // --- AEMO Spike ---
    if (hasE('aemo_price')) {
      cards.push(_aemoSpike(e));
    }

    // --- Flow Power ---
    if (hasE('flow_power_price')) {
      cards.push(_flowPower(e));
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
      min: 0,
      apex_config: {
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
  const touEntity = e('tariff_schedule');
  return {
    type: 'custom:apexcharts-card',
    header: { show: true, title: 'TOU Schedule', show_states: false },
    graph_span: '24h',
    span: { start: 'day' },
    yaxis: [{
      id: 'price',
      min: 0,
      apex_config: {
        tickAmount: 5,
        labels: {
          formatter: "EVAL:function(val) { return val.toFixed(0) + '¢'; }",
        },
      },
    }],
    series: [
      {
        entity: touEntity,
        name: 'Buy Price',
        type: 'line',
        color: '#FF9800',
        yaxis_id: 'price',
        stroke_width: 2,
        curve: 'stepline',
        extend_to: 'end',
        data_generator: _touDataGenerator('buy'),
      },
      {
        entity: touEntity,
        name: 'Sell Price',
        type: 'line',
        color: '#4CAF50',
        yaxis_id: 'price',
        stroke_width: 2,
        curve: 'stepline',
        extend_to: 'end',
        data_generator: _touDataGenerator('sell'),
      },
    ],
    apex_config: {
      chart: { height: 200 },
      stroke: { curve: 'stepline' },
      legend: { show: true, position: 'bottom' },
      tooltip: {
        x: { format: 'HH:mm' },
        y: {
          formatter: "EVAL:function(value) { if (value === null || value === undefined) return ''; if (Math.abs(value) >= 100) { return '$' + (value / 100).toFixed(2); } return value.toFixed(1) + '¢'; }",
        },
      },
    },
  };
}

function _touDataGenerator(priceKey) {
  return `
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const currentDow = now.getDay();
    let data = [];
    const schedule = entity?.attributes?.schedule || [];
    if (schedule.length > 0) {
      data = schedule.map((entry) => {
        const [hours, mins] = String(entry.time).split(':').map(Number);
        const timestamp = new Date(today.getTime() + hours * 3600000 + mins * 60000);
        return [timestamp.getTime(), entry.${priceKey}];
      });
      if (data.length > 0) {
        const endOfDay = new Date(today.getTime() + 24 * 3600000 - 1);
        data.push([endOfDay.getTime(), data[data.length - 1][1]]);
      }
      return data;
    }
    const touSchedule = entity?.attributes?.tou_schedule || [];
    if (touSchedule.length > 0) {
      const hourlyPrices = new Array(24).fill(null);
      touSchedule.forEach((period) => {
        const windows = period.windows || [];
        windows.forEach((w) => {
          if (currentDow >= w.from_day && currentDow <= w.to_day) {
            const fromHour = w.from_hour || 0;
            const toHour = w.to_hour || 24;
            if (fromHour <= toHour) {
              for (let h = fromHour; h < toHour && h < 24; h++) {
                hourlyPrices[h] = period.${priceKey};
              }
            } else {
              for (let h = fromHour; h < 24; h++) hourlyPrices[h] = period.${priceKey};
              for (let h = 0; h < toHour; h++) hourlyPrices[h] = period.${priceKey};
            }
          }
        });
      });
      const defaultPrice = entity?.attributes?.${priceKey}_price || touSchedule[0]?.${priceKey} || 0;
      for (let h = 0; h < 24; h++) {
        if (hourlyPrices[h] === null) hourlyPrices[h] = defaultPrice;
        const timestamp = new Date(today.getTime() + h * 3600000);
        data.push([timestamp.getTime(), hourlyPrices[h]]);
      }
      const endOfDay = new Date(today.getTime() + 24 * 3600000 - 1);
      data.push([endOfDay.getTime(), hourlyPrices[23]]);
      return data;
    }
    const price = entity?.attributes?.${priceKey}_price;
    if (price !== undefined) {
      return [
        [today.getTime(), price],
        [new Date(today.getTime() + 24 * 3600000 - 1).getTime(), price]
      ];
    }
    return [];
  `;
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
  const forecastDataGen = `
    const values = entity?.attributes?.forecast_values_kw;
    if (!values || !Array.isArray(values)) return [];
    const now = new Date();
    const interval = 5 * 60 * 1000;
    const start = new Date(Math.floor(now.getTime() / interval) * interval);
    return values.map((v, i) => [start.getTime() + i * interval, v]);
  `;
  return {
    type: 'custom:apexcharts-card',
    header: { show: true, title: 'LP Forecast - Solar & Load (48h)', show_states: false },
    graph_span: '48h',
    span: { start: 'minute' },
    yaxis: [{
      id: 'power',
      min: 0,
      apex_config: {
        tickAmount: 5,
        labels: {
          formatter: "EVAL:function(val) { return val.toFixed(1) + ' kW'; }",
        },
      },
    }],
    series: [
      {
        entity: e('lp_solar_forecast'),
        name: 'Solar Forecast',
        type: 'area',
        color: '#FFD700',
        yaxis_id: 'power',
        stroke_width: 2,
        opacity: 0.3,
        curve: 'smooth',
        extend_to: false,
        data_generator: forecastDataGen,
      },
      {
        entity: e('lp_load_forecast'),
        name: 'Load Forecast',
        type: 'line',
        color: '#9C27B0',
        yaxis_id: 'power',
        stroke_width: 2,
        curve: 'smooth',
        extend_to: false,
        data_generator: forecastDataGen,
      },
    ],
    apex_config: {
      chart: { height: 200 },
      stroke: { curve: 'smooth' },
      legend: { show: true, position: 'bottom' },
      tooltip: {
        x: { format: 'ddd HH:mm' },
        y: {
          formatter: "EVAL:function(value) { if (value === null || value === undefined) return ''; return value.toFixed(2) + ' kW'; }",
        },
      },
    },
  };
}

function _lpPriceChart(e) {
  const priceDataGen = `
    const values = entity?.attributes?.price_values;
    if (!values || !Array.isArray(values)) return [];
    const now = new Date();
    const interval = 5 * 60 * 1000;
    const start = new Date(Math.floor(now.getTime() / interval) * interval);
    return values.map((v, i) => [start.getTime() + i * interval, v]);
  `;
  return {
    type: 'custom:apexcharts-card',
    header: { show: true, title: 'LP Forecast - Import & Export Prices (48h)', show_states: false },
    graph_span: '48h',
    span: { start: 'minute' },
    yaxis: [{
      id: 'price',
      min: 0,
      apex_config: {
        tickAmount: 5,
        labels: {
          formatter: "EVAL:function(val) { return (val * 100).toFixed(0) + '¢'; }",
        },
      },
    }],
    series: [
      {
        entity: e('lp_import_price_forecast'),
        name: 'Import Price',
        type: 'line',
        color: '#FF9800',
        yaxis_id: 'price',
        stroke_width: 2,
        curve: 'stepline',
        extend_to: false,
        data_generator: priceDataGen,
      },
      {
        entity: e('lp_export_price_forecast'),
        name: 'Export Price',
        type: 'line',
        color: '#4CAF50',
        yaxis_id: 'price',
        stroke_width: 2,
        curve: 'stepline',
        extend_to: false,
        data_generator: priceDataGen,
      },
    ],
    apex_config: {
      chart: { height: 200 },
      stroke: { curve: 'stepline' },
      legend: { show: true, position: 'bottom' },
      tooltip: {
        x: { format: 'ddd HH:mm' },
        y: {
          formatter: "EVAL:function(value) { if (value === null || value === undefined) return ''; const cents = value * 100; if (Math.abs(cents) >= 1000) { return '$' + value.toFixed(2); } return cents.toFixed(1) + '¢'; }",
        },
      },
    },
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

function _energyChart(title, entity, color, extraApex) {
  return {
    type: 'custom:apexcharts-card',
    header: { show: true, title, show_states: true },
    graph_span: '24h',
    span: { start: 'day' },
    series: [{
      entity,
      name: title,
      type: 'area',
      color,
      stroke_width: 2,
      extend_to: 'now',
      group_by: { func: 'avg', duration: '5min' },
    }],
    apex_config: {
      chart: { height: 150 },
      ...extraApex,
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

customElements.define('ll-strategy-power-sync-strategy', PowerSyncStrategy);
