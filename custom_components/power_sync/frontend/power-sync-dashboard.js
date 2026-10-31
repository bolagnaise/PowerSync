/**
 * PowerSync dashboard board and native cards.
 *
 * Loaded by power-sync-strategy.js. The strategy decides which cards exist and
 * hands them to <power-sync-board>, which draws every card in the cloud app's
 * skin: a full-width zone (Right now, Optimizer plan) above one to three lanes
 * of movable, hideable cards. Cards without a native renderer fall back to
 * their existing Lovelace card, restyled to match.
 *
 * Native cards render into the board's shadow root (no shadow of their own) so
 * one stylesheet covers every card.
 */

const PS_LAYOUT_KEY = 'power-sync-board-layout-v1';
const PS_HIDDEN_KEY = 'power-sync-board-hidden-v1';
const PS_API_TTL_MS = 60_000;
const PS_HISTORY_TTL_MS = 5 * 60_000;
const PS_CONFIRM_MS = 4000;
const PS_NS = 'http://www.w3.org/2000/svg';

// Default order per lane count, from the approved design. Cards that are not
// listed join the shortest lane; listed cards that do not exist are skipped.
const PS_DEFAULT_LAYOUT = {
  wide: ['now', 'plan'],
  3: [
    ['controls', 'pwset', 'optset', 'packs', 'local', 'amber', 'curtail', 'acinv', 'hacs'],
    ['scene', 'ai', 'ev', 'forecast', 'bp48', 'health', 'demand', 'aemo', 'pv'],
    ['tariff', 'today', 'prices', 'energy', 'sl48', 'status', 'lifetime', 'flow', 'globird', 'covau', 'envelope', 'details'],
  ],
  2: [
    ['controls', 'scene', 'pwset', 'ev', 'optset', 'forecast', 'packs', 'health', 'local', 'amber', 'acinv', 'demand', 'flow', 'covau', 'hacs'],
    ['tariff', 'today', 'ai', 'prices', 'energy', 'bp48', 'sl48', 'status', 'lifetime', 'curtail', 'pv', 'aemo', 'globird', 'envelope', 'details'],
  ],
  1: [
    ['controls', 'tariff', 'today', 'scene', 'ai', 'ev', 'pwset', 'packs', 'optset', 'prices', 'energy', 'forecast', 'bp48', 'sl48', 'health', 'status', 'lifetime', 'local', 'amber', 'curtail', 'acinv', 'pv', 'demand', 'aemo', 'flow', 'globird', 'covau', 'envelope', 'details', 'hacs'],
  ],
};

const PS_ICON = {
  grip: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 4v5h-5"/></svg>',
};

// ─── Styles ──────────────────────────────────────────────────────
// Tokens mirror powersync.cc/app. The board follows Home Assistant's light or
// dark theme through the host's data-theme attribute.
const PS_DARK_TOKENS = `
  --bg: #060a14; --bg-2: #0b1120; --card: #0f1629; --card-hi: #141c35; --card-up: #182040;
  --fg: #e8edf5; --fg-2: #8892a8; --muted: #63708c;
  --blue: #3b82f6; --cyan: #00d4ff; --green: #00e68a; --yellow: #fbbf24; --red: #ff4d6a; --amber: #f59e0b; --purple: #a78bfa;
  --import-fill: #00d4ff1f; --soc-fill: #00e68a14; --past: #ffffff05; --grid-line: #3b82f60f; --hold: #44506e;
  --border: #3b82f624; --border-hi: #3b82f647; --border-subtle: #ffffff0f; --on-accent: #0a0e1a;
  --tint-green: #00e68a12; --tint-amber: #f59e0b14; --tint-red: #ff4d6a14; --tint-cyan: #00d4ff12; --tint-blue: #3b82f614;
  --shadow: 0 1px 3px #0000004d, 0 0 0 1px #3b82f60f;
  --bg-image: radial-gradient(120% 60% at 50% 0%, #3b82f608 0%, transparent 70%), linear-gradient(#060a14, #080d1a);
  color-scheme: dark;`;
const PS_LIGHT_TOKENS = `
  --bg: #f4f7fb; --bg-2: #edf2f8; --card: #ffffff; --card-hi: #e8eef7; --card-up: #dde7f4;
  --fg: #17243a; --fg-2: #354963; --muted: #485c76;
  --blue: #2878f0; --cyan: #009dcc; --green: #00b86b; --yellow: #d99a00; --red: #ff3158; --amber: #e08600; --purple: #9455ed;
  --import-fill: #009dcc2e; --soc-fill: #00b86b14; --past: #334c700a; --grid-line: #334c701f; --hold: #b8c4d6;
  --border: #334c7038; --border-hi: #334c7066; --border-subtle: #334c701f; --on-accent: #ffffff;
  --tint-green: #00b86b14; --tint-amber: #ef930014; --tint-red: #ff315812; --tint-cyan: #009dcc12; --tint-blue: #2878f012;
  --shadow: 0 1px 4px #1e325014, 0 0 0 1px #334c700a;
  --bg-image: linear-gradient(#f4f7fb, #eaf0f8);
  color-scheme: light;`;

const PS_CSS = `
:host { display: block; ${PS_DARK_TOKENS}
  --r: 10px; --r-lg: 16px;
  --sans: "DM Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
  background: var(--bg); background-image: var(--bg-image); color: var(--fg);
  font-family: var(--sans); font-size: 15px; line-height: 1.5; -webkit-font-smoothing: antialiased;
  min-height: 100%;
}
:host([data-theme="light"]) { ${PS_LIGHT_TOKENS} }
* { box-sizing: border-box; }
[hidden] { display: none !important; }
.wrap { max-width: 1440px; margin: 0 auto; padding: 18px 16px 48px; display: flex; flex-direction: column; gap: 16px; }
.num { font-family: var(--mono); font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
button { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; border-radius: 6px; }
h1, h2, h3, p, dl { margin: 0; }

.top { display: flex; align-items: center; gap: 10px 14px; flex-wrap: wrap; padding-bottom: 4px; }
.brand b { font-size: 19px; font-weight: 600; color: var(--cyan); letter-spacing: -0.01em; }
.live { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--fg-2); }
.live i { width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 3px #00e68a22; }
.live.stale i { background: var(--amber); box-shadow: 0 0 0 3px #f59e0b22; }
.tools { display: flex; flex-wrap: wrap; gap: 8px; margin-left: auto; align-items: center; }
.tbtn { border: 1px solid var(--border); background: var(--card); border-radius: 8px; padding: 6px 11px; font-size: 13px; font-weight: 500; cursor: pointer; color: var(--fg); }
.tbtn:hover { border-color: var(--border-hi); }
.tbtn[aria-pressed="true"] { border-color: var(--cyan); color: var(--cyan); background: var(--tint-cyan); }
.custom-hint { font-size: 13px; color: var(--fg-2); padding: 10px 14px; border-radius: var(--r); background: var(--tint-cyan); border: 1px solid #00d4ff33; }

.board { display: flex; flex-direction: column; gap: 16px; }
.zone-wide { display: flex; flex-direction: column; gap: 16px; }
.zone-wide:empty { display: none; }
.lanes { display: grid; grid-template-columns: repeat(var(--lanes, 3), minmax(0, 1fr)); gap: 16px; align-items: start; }
.lane { display: flex; flex-direction: column; gap: 16px; min-width: 0; min-height: 40px; }

.card { background: var(--card); border: 1px solid var(--border); border-radius: var(--r-lg); box-shadow: var(--shadow); padding: 18px; display: flex; flex-direction: column; gap: 14px; min-width: 0; position: relative; }
.card-b { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.card-b > ps-card-body, .card-b > [data-native] { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.card-h { display: flex; align-items: baseline; gap: 6px 12px; flex-wrap: wrap; }
.card-h h2 { font-size: 16px; font-weight: 600; letter-spacing: -0.01em; }
.card-h .meta { font-size: 12.5px; color: var(--muted); }
.card-h .act { margin-left: auto; display: flex; gap: 6px; align-items: center; }
.icon-btn { border: 1px solid var(--border); background: var(--bg-2); border-radius: 8px; width: 30px; height: 30px; display: grid; place-items: center; cursor: pointer; color: var(--fg-2); }
.icon-btn:hover { color: var(--fg); border-color: var(--border-hi); }
.icon-btn svg { width: 16px; height: 16px; }
.hint { color: var(--muted); font-size: 13px; }
.empty { color: var(--muted); font-size: 13.5px; }
.kv { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px 16px; font-size: 14px; align-items: baseline; }
.kv dt { color: var(--fg-2); margin: 0; }
.kv dd { margin: 0; text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }
.kv dd.ok { color: var(--green); }
.kv dd.warn { color: var(--amber); }
.kv dd.txt { font-family: var(--sans); }
.stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 18px; }
.stat { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.stat .k { font-size: 13px; color: var(--fg-2); }
.stat .v { font-size: 22px; font-weight: 500; font-family: var(--mono); font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.stat .v small { font-size: 13px; color: var(--muted); margin-left: 3px; font-weight: 400; }
.stat .s { font-size: 12.5px; color: var(--muted); }
.callout { padding: 10px 12px; border-radius: var(--r); font-size: 13px; display: flex; flex-direction: column; gap: 2px; border: 1px solid; }
.callout b { font-weight: 600; }
.callout.amber { background: var(--tint-amber); border-color: #f59e0b44; } .callout.amber b { color: var(--amber); }
.callout.cyan { background: var(--tint-cyan); border-color: #00d4ff33; } .callout.cyan b { color: var(--cyan); }
.callout.green { background: var(--tint-green); border-color: #00e68a33; } .callout.green b { color: var(--green); }
.callout.red { background: var(--tint-red); border-color: #ff4d6a44; } .callout.red b { color: var(--red); }
.callout span { color: var(--fg-2); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 12.5px; color: var(--fg-2); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 14px; height: 8px; border-radius: 2px; background: var(--c); display: inline-block; }
.legend i.line { height: 2px; border-radius: 1px; }
.legend i.dash { height: 0; border-top: 2px dashed var(--c); background: none; }
svg.chart { display: block; width: 100%; height: 170px; }
svg.chart text { font-family: var(--mono); font-size: 10.5px; fill: var(--muted); }

.cust-bar { display: none; align-items: center; gap: 6px; flex-wrap: wrap; margin: -6px -6px 0; padding: 6px; border-radius: 10px; background: var(--bg-2); border: 1px dashed var(--border-hi); }
.board.customizing .cust-bar { display: flex; }
.board.customizing .card-b { pointer-events: none; user-select: none; }
.cust-bar .grip { cursor: grab; touch-action: none; display: inline-flex; align-items: center; gap: 6px; border: 0; background: none; color: var(--fg-2); font-size: 13px; padding: 4px 6px; border-radius: 6px; }
.cust-bar .grip:active { cursor: grabbing; }
.cust-bar .grip svg { width: 16px; height: 16px; }
.cust-bar .ttl { font-size: 13px; font-weight: 600; }
.cust-bar .sp { flex: 1; }
.cust-bar .mv { width: 28px; height: 28px; border: 1px solid var(--border); background: var(--card); border-radius: 7px; cursor: pointer; color: var(--fg-2); font-size: 13px; }
.cust-bar .mv:disabled { opacity: .35; cursor: default; }
.cust-bar .hide { border: 1px solid var(--border); background: var(--card); border-radius: 7px; cursor: pointer; padding: 4px 10px; font-size: 12.5px; font-weight: 600; }
.cust-bar .hide:disabled { opacity: .4; cursor: not-allowed; }
.card.is-hidden { opacity: .55; border-style: dashed; }
.card.is-hidden .cust-bar .hide { color: var(--cyan); border-color: var(--cyan); }
.card.dragging { position: fixed; z-index: 50; pointer-events: none; box-shadow: 0 20px 50px #0008; opacity: .95; margin: 0; }
.placeholder { border: 2px dashed var(--cyan); border-radius: var(--r-lg); background: var(--tint-cyan); }

/* Legacy Lovelace cards inside the new chrome */
.card-b.legacy {
  --ha-card-background: transparent; --card-background-color: var(--card); --ha-card-box-shadow: none;
  --ha-card-border-width: 0; --ha-card-border-radius: 0; --ha-card-border-color: transparent;
  --primary-text-color: var(--fg); --secondary-text-color: var(--fg-2); --disabled-text-color: var(--muted);
  --primary-color: var(--cyan); --accent-color: var(--cyan); --divider-color: var(--border-subtle);
  --state-icon-color: var(--fg-2); --paper-item-icon-color: var(--fg-2);
  --mdc-theme-primary: var(--cyan); --primary-background-color: var(--bg-2); --secondary-background-color: var(--bg-2);
  --ha-font-family-body: var(--sans); --mdc-typography-font-family: var(--sans); --paper-font-body1_-_font-family: var(--sans);
  margin: -4px -16px -16px;
}
.card.own-header .card-b.legacy { margin-top: -18px; }

/* Right now */
.now-grid { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 28px; align-items: center; }
.mode { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; color: var(--fg-2); margin-bottom: 10px; }
.pill { border: 1px solid; padding: 2px 10px; border-radius: 999px; font-weight: 500; font-size: 13px; }
.pill.mon { color: var(--cyan); border-color: #00d4ff44; background: var(--tint-cyan); }
.pill.on { color: var(--green); border-color: #00e68a44; background: var(--tint-green); }
.pill.off { color: var(--fg-2); border-color: var(--border); background: var(--bg-2); }
.pill.override { color: var(--amber); border-color: #f59e0b55; background: var(--tint-amber); }
.now-grid h1 { font-size: clamp(26px, 3vw, 40px); line-height: 1.12; font-weight: 600; letter-spacing: -0.025em; max-width: 24ch; text-wrap: balance; }
.now-grid p.lead { margin-top: 12px; color: var(--fg-2); font-size: 16.5px; max-width: 62ch; text-wrap: pretty; }
.now-grid p.lead .num { color: var(--fg); }
.gauge { display: grid; grid-template-columns: auto auto; gap: 18px; align-items: end; }
.cell { position: relative; width: 68px; height: 170px; border-radius: 17px; border: 1.5px solid var(--border-hi); background: var(--bg-2); overflow: hidden; }
.cell .low { position: absolute; left: 0; right: 0; bottom: 0; background: repeating-linear-gradient(-45deg, #ff4d6a33 0 3px, transparent 3px 7px); }
.cell .fill { position: absolute; left: 0; right: 0; bottom: 0; background: linear-gradient(to top, #00e68acc, #00e68a88); transition: height .6s; }
.cell .fill::after { content: ""; position: absolute; inset: 0 0 auto; height: 2px; background: var(--green); }
.cell .res { position: absolute; left: 0; right: 0; border-top: 1.5px dashed var(--fg); opacity: .55; }
.gauge-read { display: flex; flex-direction: column; gap: 3px; }
.gauge-read .soc { font-size: 48px; line-height: 1; font-weight: 600; color: var(--green); }
.gauge-read .soc small { font-size: 20px; color: var(--fg-2); font-weight: 400; margin-left: 2px; }
.gauge-read span { font-size: 13px; color: var(--fg-2); }
.gauge-read span b { font-weight: 500; color: var(--fg); }
.power { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px 24px; padding-top: 4px; }
.read { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.read .k { font-size: 13px; color: var(--fg-2); display: flex; align-items: center; gap: 7px; }
.read .k::before { content: ""; width: 8px; height: 8px; border-radius: 2px; background: var(--c); }
.read .v { font-size: 28px; font-weight: 500; color: var(--c); line-height: 1.15; }
.read .v small { font-size: 14px; color: var(--muted); margin-left: 3px; font-weight: 400; }
.read.zero .v { color: var(--muted); }
.read .s { font-size: 13px; color: var(--muted); }
.balance { grid-column: 1 / -1; display: grid; grid-template-columns: 40px minmax(0, 1fr); gap: 6px 12px; align-items: center; font-size: 12px; color: var(--muted); }
.bar { display: flex; height: 8px; border-radius: 4px; overflow: hidden; gap: 2px; background: var(--border-subtle); }
.bar span { background: var(--c); }

/* Optimizer plan */
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { display: inline-flex; gap: 6px; align-items: baseline; font-size: 13px; padding: 4px 10px; border-radius: 8px; background: var(--bg-2); border: 1px solid var(--border-subtle); }
.chip span { color: var(--muted); }
.chip b { font-weight: 500; }
.chip.cy b { color: var(--cyan); } .chip.gr b { color: var(--green); } .chip.pu b { color: var(--purple); } .chip.rd b { color: var(--red); } .chip.am b { color: var(--amber); }
.scrub { display: grid; grid-template-columns: repeat(6, auto); justify-content: start; gap: 4px 26px; padding: 10px 14px; border-radius: var(--r); background: var(--bg-2); border: 1px solid var(--border-subtle); font-size: 13px; color: var(--fg-2); }
.scrub div { display: flex; flex-direction: column; min-width: 0; }
.scrub b { font-family: var(--mono); font-variant-numeric: tabular-nums; font-weight: 500; font-size: 15px; color: var(--fg); white-space: nowrap; }
.scrub .actv { font-family: var(--sans); font-weight: 600; }
svg.dayline { display: block; width: 100%; height: 300px; touch-action: pan-y; cursor: crosshair; }
svg.dayline text { font-family: var(--mono); font-size: 11px; fill: var(--muted); }
svg.dayline text.day-lbl, svg.dayline text.row-lbl { font-family: var(--sans); font-size: 11.5px; fill: var(--fg-2); }
svg.dayline text.lbl-now { fill: var(--fg); font-weight: 600; font-family: var(--sans); font-size: 12px; }
.plan-foot { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.windows { display: flex; flex-direction: column; gap: 8px; }
.win { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 4px 14px; align-items: center; padding: 10px 12px; border-radius: var(--r); background: var(--bg-2); border: 1px solid var(--border-subtle); }
.win .tag { font-size: 12px; font-weight: 600; color: var(--green); background: var(--tint-green); border: 1px solid #00e68a33; padding: 3px 9px; border-radius: 7px; }
.win .tag.export { color: var(--red); background: var(--tint-red); border-color: #ff4d6a44; }
.win .tag.solar { color: var(--yellow); background: var(--tint-amber); border-color: #fbbf2444; }
.win b { font-weight: 600; font-family: var(--mono); font-variant-numeric: tabular-nums; }
.win .d { font-size: 12.5px; color: var(--fg-2); }
.win .c { text-align: right; font-family: var(--mono); font-size: 13px; }
.win .c small { display: block; color: var(--muted); font-size: 11.5px; font-family: var(--sans); }
details.res { border-radius: var(--r); background: var(--bg-2); border: 1px solid var(--border-subtle); padding: 10px 14px; }
details.res summary { cursor: pointer; font-size: 14px; font-weight: 500; }
details.res .kv { margin-top: 10px; font-size: 13px; }

/* Controls and settings */
.chipsel { display: flex; flex-wrap: wrap; gap: 6px; }
.chipsel button { border: 1px solid var(--border); background: var(--bg-2); border-radius: 8px; padding: 6px 11px; cursor: pointer; font-family: var(--mono); font-size: 13px; color: var(--fg-2); }
.chipsel button[aria-pressed="true"] { border-color: var(--cyan); color: var(--cyan); background: var(--tint-cyan); }
.field { display: flex; flex-direction: column; gap: 8px; }
.field > .lab { font-size: 13px; color: var(--fg-2); }
.actions { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.btn { border-radius: var(--r); padding: 10px 12px; font-weight: 600; cursor: pointer; border: 1px solid transparent; font-size: 14px; }
.btn.primary { background: linear-gradient(180deg, #4f8ff8, #3b74e6); color: #fff; box-shadow: 0 6px 20px #3b82f633; }
.btn.amber { background: transparent; border-color: #f59e0b80; color: var(--amber); }
.btn.quiet { background: var(--bg-2); border-color: var(--border); color: var(--fg); }
.btn.danger { background: var(--tint-red); border-color: #ff4d6a55; color: var(--red); }
.btn.full { grid-column: 1 / -1; }
.btn.confirming { outline: 2px solid var(--amber); outline-offset: 1px; }
.btn:hover { filter: brightness(1.08); }
.btn:disabled { opacity: .45; cursor: not-allowed; filter: none; }
.seg { display: grid; grid-template-columns: repeat(var(--n, 3), 1fr); background: var(--card); border: 1px solid var(--border); border-radius: var(--r); padding: 3px; gap: 2px; }
.seg button { border: 0; background: none; padding: 6px 8px; border-radius: 7px; cursor: pointer; font-size: 13px; color: var(--fg-2); min-width: 0; }
.seg button[aria-pressed="true"] { background: var(--cyan); color: var(--on-accent); font-weight: 600; }
.seg button:hover:not([aria-pressed="true"]) { color: var(--fg); }
.pick { border: 1px solid var(--border); background: var(--bg-2); color: var(--fg); border-radius: 8px; padding: 6px 10px; font: inherit; font-size: 13.5px; max-width: 60%; }
.row2 { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.row2 .lab { font-size: 14px; min-width: 0; }
.row2 .lab small { display: block; color: var(--muted); font-size: 12.5px; }
.stepper { display: inline-flex; align-items: center; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; flex: none; }
.stepper button { width: 30px; height: 30px; border: 0; background: var(--bg-2); cursor: pointer; font-size: 16px; }
.stepper button:disabled { opacity: .35; cursor: default; }
.timing-input { width: 4.5em; min-width: 0; padding: 7px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-2); color: var(--fg); font: inherit; text-align: center; }
.timing-input.clock { width: 7em; }
.timing-editor { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.timing-label { font-size: 12px; color: var(--muted); }
.timing-summary { color: var(--fg-2); font-size: 13px; }
.stepper output { min-width: 64px; text-align: center; font-family: var(--mono); font-size: 14px; }
.switch { position: relative; width: 44px; height: 26px; border-radius: 13px; border: 0; background: var(--hold); cursor: pointer; flex: none; transition: background .2s; }
.switch::after { content: ""; position: absolute; top: 3px; left: 3px; width: 20px; height: 20px; border-radius: 50%; background: #fff; transition: transform .2s; }
.switch[aria-checked="true"] { background: var(--green); }
.switch[aria-checked="true"]::after { transform: translateX(18px); }
.switch:disabled { opacity: .45; cursor: not-allowed; }
.switch-list { display: flex; flex-direction: column; gap: 12px; }


/* The native dialog stays inside the board shadow root and shares its tokens. */
.settings-open { display: block; text-align: left; border: 0; padding: 0; flex: 1; background: transparent; color: var(--fg); font-family: inherit; cursor: pointer; }
.settings-open:hover { color: var(--cyan); }
.settings-open:focus-visible { outline: 2px solid var(--cyan); outline-offset: 5px; border-radius: 4px; }
.settings-chevron { color: var(--muted); margin-left: 8px; }
.settings-dialog { box-sizing: border-box; width: min(560px, calc(100vw - 32px)); max-height: calc(100dvh - 32px); overflow-y: auto; padding: 24px; border: 1px solid var(--border-hi); border-radius: var(--r-lg); background: var(--card); color: var(--fg); font: 14px/1.5 var(--sans); box-shadow: var(--shadow); color-scheme: inherit; }
.settings-dialog[open] { display: flex; flex-direction: column; }
.settings-dialog [data-body] { overflow-y: auto; min-height: 0; padding: 4px; margin: -4px; }
.settings-dialog header, .settings-dialog footer, .settings-dialog > .hint { flex-shrink: 0; }
.settings-dialog::backdrop { background: #060a14b8; backdrop-filter: blur(5px); }
.settings-dialog header { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.settings-dialog h2 { margin: 0; color: var(--fg); font-size: 20px; font-weight: 600; }
.settings-dialog header .btn { font-size: 22px; line-height: 1; padding: 8px 12px; }
.settings-dialog .hint, .settings-dialog small { color: var(--muted); }
.settings-dialog small { display: block; font-size: 12.5px; }
.settings-fields { display: flex; flex-direction: column; gap: 20px; margin-top: 20px; }
.settings-dialog .pick { box-sizing: border-box; width: 100%; max-width: 100%; padding: 10px 12px; font-family: var(--mono); }
.settings-dialog footer { display: flex; justify-content: flex-end; gap: 10px; margin-top: 24px; }
.settings-dialog .settings-error { color: var(--red); background: var(--tint-red); padding: 10px 12px; border-radius: var(--r); }
.settings-checkbox { appearance: none; position: relative; width: 44px; height: 26px; margin: 0; border: 0; border-radius: 13px; background: var(--hold); cursor: pointer; flex: none; }
.settings-checkbox::after { content: ""; position: absolute; top: 3px; left: 3px; width: 20px; height: 20px; border-radius: 50%; background: #fff; }
.settings-checkbox:checked { background: var(--green); }
.settings-checkbox:checked::after { transform: translateX(18px); }
.settings-dialog :focus-visible { outline: 2px solid var(--cyan); outline-offset: 3px; }
.settings-dialog :disabled { opacity: .45; cursor: wait; }
@media (max-width: 480px) { .settings-dialog { padding: 18px; } .settings-dialog footer .btn { flex: 1; } }

/* Tariff */
.tou-ribbon { display: flex; height: 22px; border-radius: 6px; overflow: hidden; position: relative; }
.tou-ribbon span { background: var(--c); }
.tou-ribbon .now-tick { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--fg); }
.tou-axis { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 11px; color: var(--muted); margin-top: -8px; }
.prow { display: grid; grid-template-columns: 10px minmax(0,1fr) 54px 54px; align-items: center; gap: 10px; padding: 7px 10px; border-radius: 8px; font-size: 13.5px; }
.prow.now { background: var(--bg-2); box-shadow: inset 0 0 0 1px var(--border); }
.prow i { width: 10px; height: 10px; border-radius: 3px; background: var(--c); }
.prow .n b { font-weight: 500; }
.prow .n span { display: block; color: var(--muted); font-size: 12px; font-family: var(--mono); }
.prow .i, .prow .e { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }
.prow .e { color: var(--red); }
.phead { font-size: 12px; color: var(--muted); padding-block: 0; }
.phead span:nth-child(n+3) { text-align: right; }

.chart-interaction { position: relative; min-width: 0; }
.graph-tooltip { position: absolute; top: 8px; z-index: 5; pointer-events: none; max-width: 100%; box-sizing: border-box; padding: 9px 12px; border: 1px solid var(--border-hi); border-radius: 8px; background: var(--card-up); color: var(--fg); box-shadow: 0 4px 16px #0005; font-size: 12px; }
.graph-tooltip[hidden] { display: none; }
.graph-tooltip > b { display: block; margin-bottom: 5px; }
.graph-tooltip > div { display: flex; justify-content: space-between; gap: 16px; }
.graph-tooltip strong { font-family: var(--mono); white-space: nowrap; }
.toast { position: fixed; left: 50%; bottom: 20px; transform: translate(-50%, 20px); background: var(--card-up); color: var(--fg); border: 1px solid var(--border-hi); padding: 10px 16px; border-radius: var(--r); font-size: 14px; opacity: 0; pointer-events: none; transition: opacity .2s, transform .2s; box-shadow: 0 10px 30px #0006; max-width: calc(100% - 32px); z-index: 60; }
.toast.show { opacity: 1; transform: translate(-50%, 0); }

@media (prefers-reduced-motion: reduce) { .cell .fill { transition: none; } }
@media (max-width: 1100px) { .plan-foot { grid-template-columns: 1fr; } }
@media (max-width: 760px) {
  .now-grid { grid-template-columns: 1fr; gap: 20px; }
  .gauge { justify-self: start; }
  .cell { width: 56px; height: 130px; }
  .gauge-read .soc { font-size: 40px; }
  .power { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .scrub { grid-template-columns: repeat(3, auto); gap: 8px 18px; }
  svg.dayline { height: 260px; }
  .tools { margin-left: 0; }
}
`;

// ─── Shared helpers ──────────────────────────────────────────────

const psEsc = (value) => String(value ?? '').replace(/[&<>"']/g, ch => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
));

function psStore(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key);
    return raw == null ? fallback : JSON.parse(raw);
  } catch (_) { return fallback; }
}
function psStoreSet(key, value) {
  try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* private mode */ }
}
function psStoreDel(key) {
  try { window.localStorage.removeItem(key); } catch (_) { /* private mode */ }
}

function psState(hass, id) {
  return id ? hass?.states?.[id] : undefined;
}
function psAvailable(hass, id) {
  const s = psState(hass, id);
  return !!s && s.state !== 'unavailable' && s.state !== 'unknown';
}
function psNum(hass, id) {
  const s = psState(hass, id);
  if (!s) return NaN;
  const n = Number(s.state);
  return Number.isFinite(n) ? n : NaN;
}
// Power in kW, whatever unit the sensor reports.
function psKw(hass, id) {
  const n = psNum(hass, id);
  if (!Number.isFinite(n)) return NaN;
  const unit = String(psState(hass, id)?.attributes?.unit_of_measurement || '').toLowerCase();
  if (unit === 'w') return n / 1000;
  if (unit === 'mw') return n * 1000;
  return n;
}
// Energy in kWh, whatever unit the sensor reports.
function psKwh(hass, id) {
  const n = psNum(hass, id);
  if (!Number.isFinite(n)) return NaN;
  const unit = String(psState(hass, id)?.attributes?.unit_of_measurement || '').toLowerCase();
  if (unit === 'wh') return n / 1000;
  if (unit === 'mwh') return n * 1000;
  return n;
}
function psPercent(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return NaN;
  return n <= 1 ? n * 100 : n;
}
// PowerSync price sensors report the major unit per kWh; charts use cents.
function psMinor(hass, id) {
  const n = psNum(hass, id);
  if (!Number.isFinite(n)) return NaN;
  const unit = String(psState(hass, id)?.attributes?.unit_of_measurement || '').toLowerCase();
  return /c\/|p\/|cent|pence/.test(unit) ? n : n * 100;
}
function psMinorUnit(hass) {
  const currency = String(hass?.config?.currency || 'AUD').toUpperCase();
  return currency === 'GBP' ? 'p' : currency === 'EUR' ? 'ct' : 'c';
}
function psMoney(hass, value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  try {
    return new Intl.NumberFormat(hass?.locale?.language || undefined, {
      style: 'currency', currency: hass?.config?.currency || 'AUD',
    }).format(n);
  } catch (_) {
    return `$${n.toFixed(2)}`;
  }
}
function psFix(value, digits = 1) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits) : '—';
}
function psKwText(kw) {
  if (!Number.isFinite(kw)) return '—';
  const a = Math.abs(kw);
  return a < 1 ? `${Math.round(a * 1000)} W` : `${a.toFixed(1)} kW`;
}
function psUse12h(hass) {
  const fmt = hass?.locale?.time_format;
  if (fmt === '12') return true;
  if (fmt === '24') return false;
  try {
    return new Intl.DateTimeFormat(hass?.locale?.language || undefined, { hour: 'numeric' })
      .resolvedOptions().hour12 === true;
  } catch (_) { return false; }
}
function psTime(hass, date) {
  const d = date instanceof Date ? date : new Date(date);
  if (Number.isNaN(d.getTime())) return '—';
  if (psUse12h(hass)) {
    return d.toLocaleTimeString(hass?.locale?.language || undefined, { hour: 'numeric', minute: '2-digit', hour12: true });
  }
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
function psDayPrefix(hass, date, now = new Date()) {
  const d = date instanceof Date ? date : new Date(date);
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const days = Math.floor((d - startToday) / 86_400_000);
  if (days === 0) return '';
  return d.toLocaleDateString(hass?.locale?.language || undefined, { weekday: 'short' }) + ' ';
}
function psWhen(hass, date, now = new Date()) {
  return psDayPrefix(hass, date, now) + psTime(hass, date);
}
function psDuration(minutes) {
  const m = Math.max(0, Math.round(Number(minutes) || 0));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `${h} h ${r} min` : `${h} h`;
}
// Hours since local midnight today, so charts can share one x-axis.
function psHoursFromMidnight(date, now = new Date()) {
  const d = date instanceof Date ? date : new Date(date);
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return (d - midnight) / 3_600_000;
}
function psFmtHour(hass, h, now = new Date()) {
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const d = new Date(midnight.getTime() + h * 3_600_000);
  return psWhen(hass, d, now);
}
function psFormatState(hass, id, attribute) {
  const s = psState(hass, id);
  if (!s) return '—';
  try {
    if (attribute) {
      if (typeof hass.formatEntityAttributeValue === 'function') {
        return hass.formatEntityAttributeValue(s, attribute);
      }
      return String(s.attributes?.[attribute] ?? '—');
    }
    if (typeof hass.formatEntityState === 'function') return hass.formatEntityState(s);
  } catch (_) { /* fall through */ }
  const unit = s.attributes?.unit_of_measurement;
  return unit ? `${s.state} ${unit}` : String(s.state);
}
function psFriendly(hass, id) {
  return psState(hass, id)?.attributes?.friendly_name || id;
}

// One in-flight request per path; results are reused for a short time so
// several cards can read the optimizer payload without hammering the API.
const psApiCache = new Map();
function psApi(hass, path, ttl = PS_API_TTL_MS, force = false) {
  const now = Date.now();
  const cached = psApiCache.get(path);
  if (!force && cached && (cached.promise || now - cached.at < ttl)) {
    return cached.promise || Promise.resolve(cached.data);
  }
  if (typeof hass?.callApi !== 'function') return Promise.resolve(cached?.data ?? null);
  const entry = { at: now, data: cached?.data ?? null, promise: null };
  entry.promise = hass.callApi('GET', path)
    .then(data => { entry.data = data; entry.at = Date.now(); return data; })
    .catch(() => entry.data)
    .finally(() => { entry.promise = null; });
  psApiCache.set(path, entry);
  return entry.promise;
}
function psApiPeek(path) {
  return psApiCache.get(path)?.data ?? null;
}

// Numeric history since `start` for a set of entities: { id: [[ms, value], ...] }.
function psHistory(hass, ids, start) {
  const list = ids.filter(Boolean);
  if (!list.length) return Promise.resolve({});
  const startIso = new Date(Math.floor(start.getTime() / PS_HISTORY_TTL_MS) * PS_HISTORY_TTL_MS).toISOString();
  const path = `history/period/${startIso}?filter_entity_id=${list.join(',')}&minimal_response&no_attributes`;
  return psApi(hass, path, PS_HISTORY_TTL_MS).then(raw => {
    const out = {};
    if (!Array.isArray(raw)) return out;
    for (const series of raw) {
      if (!Array.isArray(series) || !series.length) continue;
      const id = series[0].entity_id;
      out[id] = series
        .map(row => [Date.parse(row.last_changed || row.lu * 1000 || row.last_updated), Number(row.state ?? row.s)])
        .filter(([t, v]) => Number.isFinite(t) && Number.isFinite(v));
    }
    return out;
  });
}
function psAt(series, t) {
  if (!series?.length) return NaN;
  let value = NaN;
  for (const [ts, v] of series) {
    if (ts > t) break;
    value = v;
  }
  return value;
}

function psCss(el, name) {
  return getComputedStyle(el).getPropertyValue(name).trim() || '#888';
}
function psSvg(tag, attrs, parent) {
  const e = document.createElementNS(PS_NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

// Match the plotted step or line, without inventing values outside its data.
function psChartValue(series, at) {
  const pts = (series.pts || []).filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]));
  if (!pts.length || at < pts[0][0] || at > pts[pts.length - 1][0]) return NaN;
  for (let i = 1; i < pts.length; i++) {
    if (pts[i][0] > at) {
      const [x0, y0] = pts[i - 1], [x1, y1] = pts[i];
      return series.step ? y0 : y0 + (y1 - y0) * (at - x0) / (x1 - x0);
    }
  }
  return pts[pts.length - 1][1];
}

// One interaction surface for every small chart. Property handlers replace
// themselves on resize, so redraws cannot accumulate stale model listeners.
function psChartTooltip(svg, o, sample) {
  let wrap = svg.parentElement;
  if (!wrap.classList.contains('chart-interaction')) {
    wrap = document.createElement('div');
    wrap.className = 'chart-interaction';
    svg.parentElement.insertBefore(wrap, svg);
    wrap.appendChild(svg);
  }
  let tip = wrap.querySelector('.graph-tooltip');
  if (!tip) {
    tip = document.createElement('div');
    tip.className = 'graph-tooltip';
    tip.setAttribute('role', 'status');
    wrap.appendChild(tip);
  }
  tip.hidden = true;
  const hide = () => { tip.hidden = true; svg._chartAt = null; };
  const show = at => {
    at = Math.max(o.x0, Math.min(o.x1, at));
    const rows = sample(at).filter(row => Number.isFinite(row.value));
    if (!rows.length) { hide(); return; }
    svg._chartAt = at;
    const time = o.tooltipTime ? o.tooltipTime(at) : o.xFmt ? o.xFmt(at) : at;
    tip.innerHTML = `<b>${psEsc(time)}</b>${rows.map(row => `<div><span>${psEsc(row.name)}</span><strong>${psEsc(psFix(row.value, 2))} ${psEsc(o.unit || 'kW')}</strong></div>`).join('')}`;
    tip.hidden = false;
    const w = svg.clientWidth;
    const px = 34 + (at - o.x0) / (o.x1 - o.x0) * (w - 44);
    tip.style.left = `${Math.max(0, Math.min(w - tip.offsetWidth, px - tip.offsetWidth / 2))}px`;
  };
  svg.setAttribute('tabindex', '0');
  const move = e => {
    const r = svg.getBoundingClientRect();
    const px = (e.clientX - r.left) * svg.clientWidth / r.width;
    if (px < 34 || px > svg.clientWidth - 10) { hide(); return; }
    show(o.x0 + (px - 34) / (svg.clientWidth - 44) * (o.x1 - o.x0));
  };
  svg.onpointermove = move;
  svg.onpointerdown = move;
  svg.onpointerleave = hide;
  svg.onpointercancel = hide;
  svg.onblur = hide;
  svg.onkeydown = e => {
    if (e.key === 'Escape') { hide(); return; }
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    show((svg._chartAt ?? o.x0) + (e.key === 'ArrowRight' ? 1 : -1) * (o.tooltipStep || (o.x1 - o.x0) / 96));
  };
}

// Line/step chart in the design's style. Series points are [x, y].
function psChart(svg, o) {
  const W = svg.clientWidth, H = svg.clientHeight;
  if (!W) return;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.textContent = '';
  const css = name => psCss(svg, name);
  const L = 34, R = 10, T = 10, B = 20;
  const x = v => L + (v - o.x0) / (o.x1 - o.x0) * (W - L - R);
  const y = v => T + (1 - (v - o.y0) / (o.y1 - o.y0)) * (H - T - B);
  (o.yTicks || []).forEach(v => {
    psSvg('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: v === 0 ? css('--border-hi') : css('--grid-line') }, svg);
    psSvg('text', { x: L - 5, y: y(v) + 3.5, 'text-anchor': 'end' }, svg).textContent = o.yFmt ? o.yFmt(v) : v;
  });
  const span = o.x1 - o.x0, step = span > 30 ? 12 : span > 14 ? 6 : 3;
  for (let h = Math.ceil(o.x0 / step) * step; h <= o.x1; h += step) {
    const px = x(h);
    if (px < L + 14 || px > W - R - 14) continue;
    psSvg('text', { x: px, y: H - 4, 'text-anchor': 'middle' }, svg).textContent = o.xFmt ? o.xFmt(h) : h;
  }
  o.series.forEach(s => {
    const pts = (s.pts || []).filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]));
    if (!pts.length) return;
    let d = '';
    pts.forEach((p, i) => {
      if (s.step && i) d += `L${x(p[0])},${y(pts[i - 1][1])} `;
      d += `${i ? 'L' : 'M'}${x(p[0])},${y(p[1])} `;
    });
    if (s.fill) {
      const base = y(Math.max(o.y0, 0));
      psSvg('path', { d: d + `L${x(pts[pts.length - 1][0])},${base} L${x(pts[0][0])},${base} Z`, fill: css(s.fill), opacity: s.fillOp || 0.18 }, svg);
    }
    psSvg('path', {
      d, fill: 'none', stroke: css(s.c), 'stroke-width': s.w || 1.75,
      'stroke-dasharray': s.dash || 'none', 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    }, svg);
  });
  if (o.now != null) {
    psSvg('line', { x1: x(o.now), x2: x(o.now), y1: T, y2: H - B, stroke: css('--fg'), 'stroke-width': 1, opacity: 0.7 }, svg);
  }
  psChartTooltip(svg, o, at => o.series.map(s => ({ name: s.name, value: psChartValue(s, at) })));
}

// Bar chart with positive and negative colours.
function psBars(svg, o) {
  const W = svg.clientWidth, H = svg.clientHeight;
  if (!W) return;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.textContent = '';
  const css = name => psCss(svg, name);
  const L = 34, R = 10, T = 10, B = 20, n = o.vals.length || 1;
  const y = v => T + (1 - (v - o.y0) / (o.y1 - o.y0)) * (H - T - B);
  const bw = (W - L - R) / n;
  (o.yTicks || []).forEach(v => {
    psSvg('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: v === 0 ? css('--border-hi') : css('--grid-line') }, svg);
    psSvg('text', { x: L - 5, y: y(v) + 3.5, 'text-anchor': 'end' }, svg).textContent = v;
  });
  o.vals.forEach((v, i) => {
    if (!Number.isFinite(v) || v === 0) return;
    psSvg('rect', {
      x: L + i * bw, y: Math.min(y(v), y(0)), width: Math.max(0.5, bw - (bw > 3 ? 0.6 : 0)),
      height: Math.abs(y(v) - y(0)), fill: css(v >= 0 ? o.pos : o.neg),
    }, svg);
  });
  (o.labels || []).forEach(([i, txt, anchor]) => {
    psSvg('text', { x: L + i * bw, y: H - 4, 'text-anchor': anchor || 'middle' }, svg).textContent = txt;
  });
  psChartTooltip(svg, { x0: 0, x1: n, tooltipStep: 1, tooltipTime: at => o.times?.[Math.min(n - 1, Math.floor(at))] || '' }, at => {
    const value = o.vals[Math.min(n - 1, Math.floor(at))];
    return [{ name: value >= 0 ? 'Charging' : 'Discharging', value: Math.abs(value) }];
  });
}

function psNiceMax(values, floor = 1) {
  const max = Math.max(floor, ...values.filter(Number.isFinite));
  const steps = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
  const mag = 10 ** Math.floor(Math.log10(max));
  for (const s of steps) if (s * mag >= max) return s * mag;
  return 10 * mag;
}
function psTicks(min, max, count = 3) {
  const span = max - min;
  if (span <= 0) return [min];
  const raw = span / count, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(s => s * mag).find(s => s >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(Math.round(v * 100) / 100);
  return out;
}

// Normalise an optimizer schedule into one row per interval.
function psSchedule(data) {
  const s = data?.schedule || {};
  const ts = Array.isArray(s.timestamps) ? s.timestamps : [];
  const arr = key => (Array.isArray(s[key]) ? s[key] : []);
  const soc = arr('soc'), charge = arr('charge_w'), discharge = arr('discharge_w');
  const consume = arr('battery_consume_w'), exportW = arr('battery_export_w'), ev = arr('ev_charging_w');
  const imp = arr('import_price'), exp = arr('export_price'), gridImport = arr('grid_import_w');
  const exportValue = arr('export_value_price');
  const numeric = v => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v)) ? Number(v) : NaN;
  const detailed = consume.length === ts.length && exportW.length === ts.length;
  const rows = [];
  for (let i = 0; i < ts.length; i++) {
    const t = Date.parse(ts[i]);
    if (!Number.isFinite(t)) continue;
    const c = Math.max(0, Number(charge[i]) || 0);
    const d = Math.max(0, Number(discharge[i]) || 0);
    const ex = detailed ? Math.max(0, Number(exportW[i]) || 0) : 0;
    const co = detailed ? Math.max(0, Number(consume[i]) || 0) : d;
    // Site import bounds the grid-backed part of battery charging. Missing
    // source data must keep the generic label rather than imply free solar.
    const gi = numeric(gridImport[i]);
    const gridChargeKw = Number.isFinite(gi) ? Math.min(c, Math.max(0, gi)) / 1000 : NaN;
    let act = 'hold';
    if (c > 50) act = gridChargeKw <= 1e-9 ? 'solar' : 'charge';
    else if (ex > 50) act = 'export';
    else if (co > 50 || d > 50) act = 'self';
    rows.push({
      t, act,
      soc: psPercent(soc[i]),
      chargeKw: c / 1000, dischargeKw: d / 1000, exportKw: ex / 1000,
      evKw: Math.max(0, Number(ev[i]) || 0) / 1000,
      gridImportKw: Number.isFinite(gi) ? Math.max(0, gi) / 1000 : NaN,
      gridChargeKw,
      imp: Number.isFinite(Number(imp[i])) && imp[i] !== null ? Number(imp[i]) * 100 : NaN,
      exp: Number.isFinite(Number(exp[i])) && exp[i] !== null ? Number(exp[i]) * 100 : NaN,
      exportValuePrice: (Number.isFinite(numeric(exportValue[i])) ? numeric(exportValue[i]) : numeric(exp[i])) * 100,
    });
  }
  const interval = rows.length > 1 ? Math.max(1, Math.round((rows[1].t - rows[0].t) / 60_000)) : 30;
  return { rows, interval };
}
const PS_ACT = {
  charge: ['Charge', '--green'],
  solar: ['Solar charge', '--yellow'],
  self: ['Power the home', '--purple'],
  hold: ['Hold', '--hold'],
  export: ['Export', '--red'],
};
function psActionKey(action) {
  const a = String(action || '').toLowerCase();
  if (a.includes('charge') && !a.includes('discharge')) return 'charge';
  if (a.includes('export')) return 'export';
  if (a === 'idle' || a.includes('hold')) return 'hold';
  if (a.includes('self') || a.includes('consum') || a.includes('discharge') || a.includes('home')) return 'self';
  return a ? 'self' : '';
}
// When the plan stops doing `act`, following the run that contains `now`.
// The optimizer's current_action_end_time is only the end of its current
// step (often 5 minutes away), which read as "the battery runs out then".
function psPlannedRunEnd(rows, interval, act, now) {
  const step = interval * 60_000;
  const i = rows.findIndex(r => r.t <= now && now < r.t + step);
  if (i < 0 || !act || rows[i].act !== act) return null;
  let j = i;
  while (j + 1 < rows.length && rows[j + 1].act === act && rows[j + 1].t - rows[j].t <= step) j += 1;
  return rows[j].t + step;
}

// The next planned change of action. next_action is often the current action
// again (the optimizer re-checking), so prefer the first run in next_actions
// that actually differs.
function psNextChange(data, actKey, now) {
  const runs = Array.isArray(data?.next_actions) ? data.next_actions : [];
  for (const run of runs) {
    const key = psActionKey(run?.action);
    const t = Date.parse(run?.timestamp);
    if (key && key !== actKey && Number.isFinite(t) && t > now - 60_000) return { key, t };
  }
  const key = psActionKey(data?.next_action);
  const t = Date.parse(data?.next_action_time);
  return key && key !== actKey && Number.isFinite(t) ? { key, t } : null;
}

// Contiguous runs of one action; a different action always starts a new run.
function psWindows(rows, interval, act, fromT) {
  const out = [];
  let cur = null;
  for (const r of rows) {
    if (r.t + interval * 60_000 <= fromT) continue;
    if (r.act === act) {
      if (cur && r.t - cur.end <= interval * 60_000) {
        cur.end = r.t + interval * 60_000; cur.rows.push(r);
      } else {
        cur = { start: Math.max(r.t, fromT), end: r.t + interval * 60_000, rows: [r] };
        out.push(cur);
      }
    } else cur = null;
  }
  return out;
}

// ─── Native card base ────────────────────────────────────────────
// Light-DOM element: the board's shadow stylesheet styles it. Subclasses
// implement _signature() (cheap change detection) and _render().

class PsCardBase extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._hass = null;
    this._renderQueued = false;
    this._lastSig = null;
    this._resizeObserver = null;
  }

  set config(config) { this._config = config || {}; this._lastSig = null; this._queue(); }
  get config() { return this._config; }
  set hass(hass) { this._hass = hass; this._queue(); }
  get hass() { return this._hass; }

  get ents() { return this._config.entities || {}; }

  connectedCallback() {
    this.dataset.native = '';
    if (this._wantsResize && !this._resizeObserver && 'ResizeObserver' in window) {
      let lastWidth = 0;
      this._resizeObserver = new ResizeObserver(entries => {
        const w = Math.round(entries[0]?.contentRect?.width || 0);
        if (w && w !== lastWidth) { lastWidth = w; this._draw(); }
      });
      this._resizeObserver.observe(this);
    }
    this._queue();
  }

  disconnectedCallback() {
    if (this._resizeObserver) { this._resizeObserver.disconnect(); this._resizeObserver = null; }
  }

  _queue() {
    if (this._renderQueued || !this._hass) return;
    this._renderQueued = true;
    // A microtask, not requestAnimationFrame: rAF pauses in background tabs
    // and some kiosk browsers, which left cards empty until the next state.
    queueMicrotask(() => {
      this._renderQueued = false;
      if (!this.isConnected || !this._hass) return;
      const sig = this._signature();
      if (sig === this._lastSig) return;
      this._lastSig = sig;
      this._render();
      this._draw();
    });
  }

  // Re-render on the next frame even if entity states did not change (for
  // example when an API payload arrives).
  _invalidate() { this._lastSig = null; this._queue(); }

  _signature() {
    const ids = Object.values(this.ents).flat().filter(Boolean);
    return ids.map(id => {
      const s = this._hass?.states?.[id];
      return s ? `${s.state}|${s.last_updated}` : '-';
    }).join(';');
  }

  _render() {}
  _draw() {}

  _call(domain, service, data = {}) {
    return this._hass.callService(domain, service, data)
      .then(() => true)
      .catch(err => { this._toast(`Could not complete that: ${err?.message || err}`); return false; });
  }

  _toast(message) {
    this.dispatchEvent(new CustomEvent('ps-toast', { detail: message, bubbles: true, composed: true }));
  }

  _signal(name, detail) {
    this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true }));
  }

  // Cached data now; re-render once when a request in flight lands. A
  // resolved cache hit must not invalidate, or render -> _api -> render loops.
  _api(path, ttl, force) {
    const p = psApi(this._hass, path, ttl, force);
    if (psApiCache.get(path)?.promise === p) p.then(() => this._invalidate());
    return psApiPeek(path);
  }
}

// ─── Board ───────────────────────────────────────────────────────

class PowerSyncBoard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = { cards: [] };
    this._hass = null;
    this._built = false;
    this._els = {};
    this._bodies = {};
    this._customizing = false;
    this._showingHidden = false;
    this._hidden = new Set(psStore(PS_HIDDEN_KEY, []));
    this._lanes = 0;
    this._model = null;
    this._drag = null;
    this._toastTimer = null;
    this._resizeTimer = null;
    this._helpersPromise = null;
  }

  setConfig(config) {
    this._config = { cards: [], ...config };
    if (this._built) this._buildCards();
  }

  getCardSize() { return 20; }

  set hass(hass) {
    this._hass = hass;
    const dark = hass?.themes?.darkMode !== false;
    this.setAttribute('data-theme', dark ? 'dark' : 'light');
    if (!this._built) this._build();
    for (const body of Object.values(this._bodies)) {
      if (body) body.hass = hass;
    }
    this._updateLive();
  }

  connectedCallback() {
    PowerSyncBoard._loadFonts();
    if (this._built && !this._ro) this._observe();
  }

  disconnectedCallback() {
    if (this._ro) { this._ro.disconnect(); this._ro = null; }
  }

  static _loadFonts() {
    if (document.getElementById('power-sync-board-fonts')) return;
    const link = document.createElement('link');
    link.id = 'power-sync-board-fonts';
    link.rel = 'stylesheet';
    link.href = 'https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700&family=JetBrains+Mono:wght@400;500;600&display=swap';
    document.head.appendChild(link);
  }

  get _cards() {
    return (this._config.cards || []).filter(c => c && c.key);
  }

  _title(key) {
    return this._cards.find(c => c.key === key)?.title || key;
  }

  _build() {
    this._built = true;
    const root = this.shadowRoot;
    root.innerHTML = `
      <style>${PS_CSS}</style>
      <div class="wrap">
        <header class="top">
          <div class="brand"><b>PowerSync</b></div>
          <div class="live" id="live"><i aria-hidden="true"></i><span id="live-t">Live</span></div>
          <div class="tools">
            <button type="button" class="tbtn" id="t-hidden" hidden aria-pressed="false">Show hidden (0)</button>
            <button type="button" class="tbtn" id="t-reset">Reset layout</button>
            <button type="button" class="tbtn" id="t-cust" aria-pressed="false">Customize layout</button>
          </div>
        </header>
        <div class="custom-hint" id="cust-hint" hidden>Drag a card by its handle, or use the arrows, to move it. Hide removes a card from the dashboard. Show hidden brings hidden cards back so you can unhide them. Your layout is saved in this browser, separately for 1, 2 and 3 columns.</div>
        <main class="board" id="board">
          <div class="zone-wide" id="zone-wide"></div>
          <div class="lanes" id="lanes"></div>
        </main>
      </div>
      <div class="toast" id="toast" role="status" aria-live="polite"></div>`;
    this._$ = sel => root.querySelector(sel);
    this._$$ = (sel, r = root) => Array.from(r.querySelectorAll(sel));
    this._bindTools();
    this._buildCards();
    this._observe();
    root.addEventListener('ps-toast', e => this._toast(e.detail));
    root.addEventListener('ps-settings-open', e => {
      const { row, opener } = e.detail || {};
      if (PS_SETTING_EDITORS[row?.editor] && opener) psOpenSettings(root, this._hass, row, opener);
    });
    root.addEventListener('ps-settings-saved', e => {
      if (e.detail === 'power_sync/ev/solar_surplus_config') this._bodies.ev?.refresh?.();
    });
  }

  _observe() {
    if (!('ResizeObserver' in window)) return;
    this._ro = new ResizeObserver(() => {
      clearTimeout(this._resizeTimer);
      this._resizeTimer = setTimeout(() => {
        if (this._drag) return;
        if (this._laneCount() !== this._lanes) this._render();
      }, 80);
    });
    this._ro.observe(this._$('#board'));
  }

  _toast(message) {
    const el = this._$('#toast');
    if (!el) return;
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
  }

  _updateLive() {
    const el = this._$?.('#live-t');
    if (!el || !this._hass) return;
    const connected = this._hass.connected !== false;
    this._$('#live').classList.toggle('stale', !connected);
    el.textContent = connected ? `Live, updated ${psTime(this._hass, new Date())}` : 'Reconnecting to Home Assistant';
  }

  _helpers() {
    if (!this._helpersPromise) {
      this._helpersPromise = typeof window.loadCardHelpers === 'function'
        ? window.loadCardHelpers().catch(() => null)
        : Promise.resolve(null);
    }
    return this._helpersPromise;
  }

  _buildCards() {
    this._els = {};
    this._bodies = {};
    for (const c of this._cards) {
      const a = document.createElement('article');
      a.className = 'card';
      a.dataset.key = c.key;
      a.dataset.size = c.size === 'wide' ? 'wide' : 'std';
      a.setAttribute('aria-labelledby', `h-${c.key}`);
      if (c.ownHeader) a.classList.add('own-header');
      const header = c.ownHeader
        ? `<h2 id="h-${psEsc(c.key)}" hidden>${psEsc(c.title)}</h2>`
        : `<header class="card-h"><h2 id="h-${psEsc(c.key)}">${psEsc(c.title)}</h2>${c.meta ? `<span class="meta">${psEsc(c.meta)}</span>` : ''}${c.refresh ? `<span class="act"><button type="button" class="icon-btn" data-refresh aria-label="Refresh ${psEsc(c.title)}">${PS_ICON.refresh}</button></span>` : ''}</header>`;
      a.innerHTML = `
        <div class="cust-bar">
          <button type="button" class="grip" aria-label="Drag to move ${psEsc(c.title)}">${PS_ICON.grip}<span>Move</span></button>
          ${c.ownHeader ? `<span class="ttl">${psEsc(c.title)}</span>` : ''}
          <span class="sp"></span>
          <button type="button" class="mv" data-mv="left" aria-label="Move ${psEsc(c.title)} to the previous column">←</button>
          <button type="button" class="mv" data-mv="up" aria-label="Move ${psEsc(c.title)} up">↑</button>
          <button type="button" class="mv" data-mv="down" aria-label="Move ${psEsc(c.title)} down">↓</button>
          <button type="button" class="mv" data-mv="right" aria-label="Move ${psEsc(c.title)} to the next column">→</button>
          <button type="button" class="hide">Hide</button>
        </div>
        ${header}
        <div class="card-b"></div>`;
      const body = a.querySelector('.card-b');
      if (c.native) {
        const el = document.createElement(c.native);
        el.config = c.config || {};
        if (this._hass) el.hass = this._hass;
        body.appendChild(el);
        this._bodies[c.key] = el;
      } else if (c.card) {
        body.classList.add('legacy');
        this._bodies[c.key] = null;
        this._mountLegacy(c, body);
      }
      const refresh = a.querySelector('[data-refresh]');
      if (refresh) {
        refresh.addEventListener('click', () => {
          const el = this._bodies[c.key];
          if (el && typeof el.refresh === 'function') el.refresh();
        });
      }
      this._els[c.key] = a;
    }
    this._render();
  }

  async _mountLegacy(c, body) {
    const helpers = await this._helpers();
    let el;
    try {
      if (helpers) {
        el = helpers.createCardElement(c.card);
      } else {
        const tag = String(c.card.type || '').startsWith('custom:')
          ? c.card.type.slice(7)
          : `hui-${c.card.type}-card`;
        el = document.createElement(tag);
        el.setConfig?.(c.card);
      }
    } catch (err) {
      body.innerHTML = `<p class="empty">This card could not load: ${psEsc(err?.message || err)}</p>`;
      return;
    }
    if (this._hass) el.hass = this._hass;
    body.appendChild(el);
    this._bodies[c.key] = el;
  }

  _bindTools() {
    const $ = this._$;
    $('#t-cust').addEventListener('click', () => {
      this._customizing = !this._customizing;
      if (!this._customizing && this._showingHidden) { this._showingHidden = false; this._render(); }
      else this._sync();
    });
    $('#t-hidden').addEventListener('click', () => {
      this._showingHidden = !this._showingHidden;
      if (this._showingHidden) this._customizing = true;
      this._render();
    });
    $('#t-reset').addEventListener('click', () => {
      psStoreDel(PS_LAYOUT_KEY);
      psStoreDel(PS_HIDDEN_KEY);
      this._hidden = new Set();
      this._showingHidden = false;
      this._render();
      this._toast('Layout reset');
    });

    const board = $('#board');
    board.addEventListener('click', e => this._onBoardClick(e));
    board.addEventListener('pointerdown', e => this._onPointerDown(e));
    board.addEventListener('pointermove', e => this._onPointerMove(e));
    board.addEventListener('pointerup', () => this._endDrag());
    board.addEventListener('pointercancel', () => this._endDrag());
  }

  _laneCount() {
    const w = this._$('#board')?.clientWidth || this.clientWidth || window.innerWidth;
    return w < 760 ? 1 : w < 1180 ? 2 : 3;
  }

  _isShown(key) {
    return !!this._els[key] && (!this._hidden.has(key) || this._showingHidden);
  }

  _layoutFor(n) {
    const keys = this._cards.map(c => c.key);
    const present = new Set(keys);
    const wideKeys = this._cards.filter(c => c.size === 'wide').map(c => c.key);
    const saved = (psStore(PS_LAYOUT_KEY, {}) || {})[n];
    const defaults = PS_DEFAULT_LAYOUT[n] || PS_DEFAULT_LAYOUT[3];
    let wide = saved && Array.isArray(saved.wide) ? saved.wide.slice() : PS_DEFAULT_LAYOUT.wide.slice();
    let lanes = saved && Array.isArray(saved.lanes) && saved.lanes.length === n
      ? saved.lanes.map(l => (Array.isArray(l) ? l.slice() : []))
      : defaults.map(l => l.slice());
    wide = wide.filter(k => wideKeys.includes(k));
    wideKeys.forEach(k => { if (!wide.includes(k)) wide.push(k); });
    // Keep unknown keys in the saved model (a card may come back), but place
    // every present standard card exactly once.
    const seen = new Set();
    lanes = lanes.map(l => l.filter(k => {
      if (wideKeys.includes(k) || seen.has(k)) return false;
      seen.add(k);
      return true;
    }));
    for (const k of keys) {
      if (wideKeys.includes(k) || seen.has(k)) continue;
      const defaultLane = defaults.findIndex(l => l.includes(k));
      const target = defaultLane >= 0 && lanes[defaultLane]
        ? lanes[defaultLane]
        : lanes.reduce((a, b) => (a.filter(x => present.has(x)).length <= b.filter(x => present.has(x)).length ? a : b));
      target.push(k);
      seen.add(k);
    }
    return { wide, lanes };
  }

  _render() {
    if (!this._built) return;
    const n = this._laneCount();
    this._lanes = n;
    this._model = this._layoutFor(n);
    const zoneWide = this._$('#zone-wide'), lanesEl = this._$('#lanes');
    lanesEl.style.setProperty('--lanes', n);
    for (const el of Object.values(this._els)) el.remove();
    this._model.wide.forEach(k => { if (this._isShown(k)) zoneWide.appendChild(this._els[k]); });
    lanesEl.textContent = '';
    this._model.lanes.forEach((keys, i) => {
      const lane = document.createElement('div');
      lane.className = 'lane';
      lane.dataset.lane = i;
      keys.forEach(k => { if (this._isShown(k)) lane.appendChild(this._els[k]); });
      lanesEl.appendChild(lane);
    });
    this._sync();
  }

  _mergeOrder(base, dom) {
    const out = dom.slice(), domSet = new Set(dom);
    base.forEach((k, i) => {
      if (domSet.has(k)) return;
      const prev = base.slice(0, i).reverse().find(p => out.includes(p));
      out.splice(prev ? out.indexOf(prev) + 1 : 0, 0, k);
    });
    return out;
  }

  _saveLayout() {
    const $$ = this._$$;
    const domWide = $$('.card', this._$('#zone-wide')).map(e => e.dataset.key);
    const domLanes = $$('.lane', this._$('#lanes')).map(l => $$('.card', l).map(e => e.dataset.key));
    const onScreen = new Set(domLanes.flat());
    this._model.wide = this._mergeOrder(this._model.wide, domWide);
    this._model.lanes = this._model.lanes.map((base, i) => this._mergeOrder(
      base.filter(k => !onScreen.has(k) || domLanes[i].includes(k)),
      domLanes[i],
    ));
    const all = psStore(PS_LAYOUT_KEY, {}) || {};
    all[this._lanes] = this._model;
    psStoreSet(PS_LAYOUT_KEY, all);
  }

  _sync() {
    const $ = this._$, $$ = this._$$;
    $('#board').classList.toggle('customizing', this._customizing);
    $('#cust-hint').hidden = !this._customizing;
    const tc = $('#t-cust');
    tc.textContent = this._customizing ? 'Done' : 'Customize layout';
    tc.setAttribute('aria-pressed', String(this._customizing));
    const hiddenPresent = [...this._hidden].filter(k => this._els[k]);
    if (!hiddenPresent.length) this._showingHidden = false;
    const th = $('#t-hidden');
    th.hidden = hiddenPresent.length === 0;
    th.textContent = `${this._showingHidden ? 'Hide hidden' : 'Show hidden'} (${hiddenPresent.length})`;
    th.setAttribute('aria-pressed', String(this._showingHidden));
    const visibleCount = Object.keys(this._els).filter(k => !this._hidden.has(k)).length;
    for (const el of Object.values(this._els)) {
      const k = el.dataset.key, isH = this._hidden.has(k);
      el.classList.toggle('is-hidden', isH);
      const hb = el.querySelector('.hide');
      hb.textContent = isH ? 'Unhide' : 'Hide';
      hb.disabled = !isH && visibleCount <= 1;
      hb.setAttribute('aria-label', `${isH ? 'Unhide' : 'Hide'} ${this._title(k)}`);
      const parent = el.parentElement;
      if (!parent) continue;
      const sibs = $$('.card', parent), i = sibs.indexOf(el);
      el.querySelector('[data-mv="up"]').disabled = i <= 0;
      el.querySelector('[data-mv="down"]').disabled = i >= sibs.length - 1;
      const wide = el.dataset.size === 'wide', li = Number(parent.dataset.lane || 0);
      const left = el.querySelector('[data-mv="left"]'), right = el.querySelector('[data-mv="right"]');
      left.hidden = wide || this._lanes < 2;
      right.hidden = wide || this._lanes < 2;
      left.disabled = li <= 0;
      right.disabled = li >= this._lanes - 1;
    }
  }

  _onBoardClick(e) {
    if (!this._customizing) return;
    const path = e.composedPath();
    const card = path.find(n => n.classList?.contains('card'));
    if (!card) return;
    const k = card.dataset.key;
    const hideBtn = path.find(n => n.classList?.contains('hide'));
    if (hideBtn) {
      if (this._hidden.has(k)) { this._hidden.delete(k); this._toast(`${this._title(k)} is back on the dashboard`); }
      else { this._hidden.add(k); this._toast(`${this._title(k)} hidden`); }
      psStoreSet(PS_HIDDEN_KEY, [...this._hidden]);
      if (this._hidden.has(k) && !this._showingHidden) card.remove();
      this._sync();
      return;
    }
    const mv = path.find(n => n.dataset?.mv);
    if (!mv) return;
    const parent = card.parentElement, dir = mv.dataset.mv;
    if (dir === 'up' && card.previousElementSibling) parent.insertBefore(card, card.previousElementSibling);
    if (dir === 'down' && card.nextElementSibling) parent.insertBefore(card.nextElementSibling, card);
    if (dir === 'left' || dir === 'right') {
      const lanes = this._$$('.lane', this._$('#lanes'));
      const li = Number(parent.dataset.lane) + (dir === 'left' ? -1 : 1);
      if (lanes[li]) {
        const idx = this._$$('.card', parent).indexOf(card);
        lanes[li].insertBefore(card, this._$$('.card', lanes[li])[idx] || null);
      }
    }
    this._saveLayout();
    this._sync();
    mv.focus();
  }

  _onPointerDown(e) {
    if (!this._customizing) return;
    const path = e.composedPath();
    const grip = path.find(n => n.classList?.contains('grip'));
    if (!grip) return;
    const card = path.find(n => n.classList?.contains('card'));
    const r = card.getBoundingClientRect();
    e.preventDefault();
    try { grip.setPointerCapture(e.pointerId); } catch (_) { /* older browsers */ }
    const ph = document.createElement('div');
    ph.className = 'placeholder';
    ph.style.height = `${r.height}px`;
    card.parentElement.insertBefore(ph, card);
    card.classList.add('dragging');
    card.style.width = `${r.width}px`;
    card.style.left = `${r.left}px`;
    card.style.top = `${r.top}px`;
    this._drag = { card, ph, dx: e.clientX - r.left, dy: e.clientY - r.top, wide: card.dataset.size === 'wide' };
  }

  _onPointerMove(e) {
    const drag = this._drag;
    if (!drag) return;
    drag.card.style.left = `${e.clientX - drag.dx}px`;
    drag.card.style.top = `${e.clientY - drag.dy}px`;
    let container = this._$('#zone-wide');
    if (!drag.wide) {
      const lanes = this._$$('.lane', this._$('#lanes'));
      container = lanes.reduce((best, l) => {
        const r = l.getBoundingClientRect();
        const d = e.clientX < r.left ? r.left - e.clientX : e.clientX > r.right ? e.clientX - r.right : 0;
        return d < best.d ? { l, d } : best;
      }, { l: lanes[0], d: Infinity }).l;
    }
    const after = this._$$('.card', container)
      .filter(c => c !== drag.card)
      .find(c => { const r = c.getBoundingClientRect(); return e.clientY < r.top + r.height / 2; });
    container.insertBefore(drag.ph, after || null);
    if (e.clientY < 60) window.scrollBy(0, -14);
    else if (e.clientY > window.innerHeight - 60) window.scrollBy(0, 14);
  }

  _endDrag() {
    const drag = this._drag;
    if (!drag) return;
    drag.ph.parentElement.insertBefore(drag.card, drag.ph);
    drag.ph.remove();
    drag.card.classList.remove('dragging');
    drag.card.removeAttribute('style');
    this._drag = null;
    this._saveLayout();
    this._sync();
  }
}

// ─── Generic native cards ────────────────────────────────────────

// Rows of label and formatted state. Config: { rows: [{ entity, name,
// attribute?, ok?: [states], warn?: [states], text?: bool }], note? }.
class PsKvCard extends PsCardBase {
  _signature() {
    return (this._config.rows || []).map(r => {
      const s = this._hass?.states?.[r.entity];
      return s ? `${s.state}|${s.last_updated}` : '-';
    }).join(';');
  }

  _render() {
    const hass = this._hass;
    const rows = (this._config.rows || []).filter(r => psState(hass, r.entity));
    if (!rows.length) {
      this.innerHTML = '<p class="empty">Waiting for data.</p>';
      return;
    }
    const html = rows.map(r => {
      const s = psState(hass, r.entity);
      const raw = r.attribute ? s.attributes?.[r.attribute] : s.state;
      const text = psFormatState(hass, r.entity, r.attribute);
      const numeric = Number.isFinite(Number(raw));
      const cls = [
        !numeric || r.text ? 'txt' : '',
        (r.ok || []).includes(String(raw)) ? 'ok' : '',
        (r.warn || []).includes(String(raw)) ? 'warn' : '',
      ].filter(Boolean).join(' ');
      return `<dt>${psEsc(r.name || psFriendly(hass, r.entity))}</dt><dd class="${cls}">${psEsc(text)}</dd>`;
    }).join('');
    this.innerHTML = `<dl class="kv">${html}</dl>${this._config.note ? `<p class="hint">${psEsc(this._config.note)}</p>` : ''}`;
  }
}

// Editors use the existing partial-update APIs. Only changed fields are sent;
// toggles on the card remain independent of an open settings draft.
const PS_SETTING_FIELDS = {
  auto_update_time: { label: 'Scheduled update time', type: 'time', help: 'Home Assistant local time. PowerSync may restart Home Assistant after installing an update.' },
  backup_reserve: { label: 'Manual export floor', unit: '%', min: 0, max: 100, ratio: true, help: 'Used when the recommended reserve is not applied.' },
  daily_supply_charge: { label: 'Daily supply charge', min: 0, step: '0.01', help: 'Daily fixed charge in your tariff currency, rather than cents.' },
  charge_by_time_target_soc: { label: 'Target charge level', unit: '%', min: 0, max: 100, ratio: true },
  charge_by_time_target_time: { label: 'Target time', type: 'time', help: 'Home Assistant local time.' },
  battery_capacity_wh: { label: 'Usable battery capacity', unit: 'Wh', min: 1 },
  max_charge_w: { label: 'Maximum battery charge power', unit: 'W', min: 1 },
  max_discharge_w: { label: 'Maximum battery discharge power', unit: 'W', min: 1 },
  max_grid_import_w: { label: 'Maximum grid import', unit: 'W', min: 0, help: 'Zero uses the system default.' },
  max_grid_export_w: { label: 'Maximum grid export', unit: 'W', min: 0, nullable: true, help: 'Leave blank to use the system limit. Zero prevents planned export.' },
  allow_grid_charge: { label: 'Allow grid charging', type: 'checkbox' },
  grid_charge_soc_cap: { label: 'Grid charge ceiling', unit: '%', min: 0, max: 100, ratio: true },
  ev_integration: { label: 'Include PowerSync EV plans', type: 'checkbox', help: 'Include planned car demand in battery planning.' },
  battery_efficiency_learning_enabled: { label: 'Learn battery efficiency', type: 'checkbox' },
  household_buffer_kw: { label: 'Household buffer', unit: 'kW', min: 0, step: '0.1' },
  max_export_price_cents: { label: 'Maximum export price for car charging', unit: 'c/kWh', min: 0, max: 200, step: '0.1' },
  home_battery_minimum: { label: 'Minimum home battery before car charging', unit: '%', min: 0, max: 100 },
  sustained_surplus_minutes: { label: 'Start delay', unit: 'minutes', min: 1 },
  stop_delay_minutes: { label: 'Stop delay', unit: 'minutes', min: 1 },
  allow_parallel_charging: { label: 'Charge the car and home battery together', type: 'checkbox' },
  max_battery_charge_rate_kw: { label: 'Maximum home battery charge rate', unit: 'kW', min: 0.1, step: '0.1' },
  dual_vehicle_strategy: { label: 'Multiple car strategy', type: 'select', options: [['priority_first', 'Priority first'], ['even', 'Even split'], ['priority_only', 'Primary car only']] },
};
const PS_SETTING_EDITORS = {
  auto_update: { path: 'power_sync/auto_update/settings', entityScoped: true, fields: ['auto_update_time'] },
  optimizer: { path: 'power_sync/optimization/settings', fields: ['backup_reserve', 'battery_capacity_wh', 'max_charge_w', 'max_discharge_w', 'max_grid_import_w', 'max_grid_export_w', 'allow_grid_charge', 'grid_charge_soc_cap', 'ev_integration', 'battery_efficiency_learning_enabled'] },
  reserve: { path: 'power_sync/optimization/settings', fields: ['backup_reserve'] },
  cost_neutral: { path: 'power_sync/optimization/settings', fields: ['daily_supply_charge'] },
  charge_by_time: { path: 'power_sync/optimization/settings', fields: ['charge_by_time_target_soc', 'charge_by_time_target_time'] },
  solar: { path: 'power_sync/ev/solar_surplus_config', fields: ['home_battery_minimum', 'household_buffer_kw', 'max_export_price_cents', 'sustained_surplus_minutes', 'stop_delay_minutes', 'allow_parallel_charging', 'max_battery_charge_rate_kw', 'dual_vehicle_strategy'] },
};

class PsSettingsDialog extends HTMLElement {
  async open(hass, row, opener) {
    this._hass = hass;
    const editor = PS_SETTING_EDITORS[row.editor];
    this._editor = { ...editor, path: editor.path + (editor.entityScoped ? `?entity_id=${encodeURIComponent(row.entity)}` : '') };
    this._opener = opener;
    this._owner = opener.closest('ps-settings-card') || opener.getRootNode().host;
    this.innerHTML = `<dialog class="settings-dialog" aria-labelledby="settings-title"><header><h2 id="settings-title">${psEsc(row.label)}</h2><button type="button" class="btn quiet" data-close aria-label="Close settings">×</button></header><p class="hint">${psEsc(row.help || '')}</p><div data-body role="status">Loading settings…</div><p class="settings-error" role="alert" hidden></p><footer><button type="button" class="btn quiet" data-close>Cancel</button><button type="submit" form="ps-settings-form" class="btn primary" data-save disabled>Save changes</button></footer></dialog>`;
    const dialog = this.querySelector('dialog');
    this._dialog = dialog;
    dialog.addEventListener('cancel', e => { if (this._saving) e.preventDefault(); });
    dialog.addEventListener('keydown', e => {
      if (e.key !== 'Tab') return;
      const controls = [...dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled)')];
      const active = dialog.getRootNode().activeElement;
      if (!controls.length) { e.preventDefault(); return; }
      if (e.shiftKey && active === controls[0]) { e.preventDefault(); controls.at(-1).focus(); }
      else if (!e.shiftKey && active === controls.at(-1)) { e.preventDefault(); controls[0].focus(); }
    });
    dialog.addEventListener('close', () => {
      const focusTarget = this._opener?.isConnected ? this._opener : (opener.dataset.editor != null ? this._owner?.querySelector(`[data-editor="${opener.dataset.editor}"]`) : this._owner?.shadowRoot?.querySelector('[data-action="configure-solar"]'));
      this.remove();
      focusTarget?.focus();
    });
    this.querySelectorAll('[data-close]').forEach(btn => btn.addEventListener('click', () => dialog.close()));
    dialog.showModal();
    try {
      const data = await this._load();
      if (!this.isConnected) return;
      this._values = { ...data, ...(data.config || {}) };
      const schema = data.settings_schema?.fields;
      this._fields = this._editor.fields.filter(key => key in this._values && (!schema || key in schema));
      this.querySelector('[data-body]').innerHTML = this._fields.length
        ? `<form id="ps-settings-form" class="settings-fields">${this._fields.map(key => this._fieldHtml(key)).join('')}</form>`
        : '<p class="hint">No configurable options are available for this system.</p>';
      this.querySelector('[data-body]').removeAttribute('role');
      this.querySelector('form')?.addEventListener('submit', e => { e.preventDefault(); this._save(); });
      this.querySelector('[data-save]').disabled = !this._fields.length;
    } catch (err) { this.querySelector('[data-body]').textContent = ''; this._error(`Could not load settings: ${err?.body?.error || err?.body?.message || err.message || err}`); }
  }

  async _load() {
    const data = await this._hass.callApi('GET', this._editor.path);
    if (!data || data.success === false) throw new Error(data?.error || 'Settings are unavailable.');
    return data;
  }

  _fieldHtml(key) {
    const f = PS_SETTING_FIELDS[key], value = this._values[key];
    const label = psEsc(f.label + (f.unit ? ` (${f.unit})` : ''));
    const help = f.help ? `<small>${psEsc(f.help)}</small>` : '';
    const attrs = `name="${key}" id="setting-${key}"`;
    if (f.type === 'checkbox') {
      return `<label class="row2 settings-check" for="setting-${key}"><span class="lab">${label}${help}</span><input class="settings-checkbox" type="checkbox" ${attrs} ${value ? 'checked' : ''}></label>`;
    }
    const input = f.type === 'select'
      ? `<select class="pick" ${attrs}>${f.options.map(([v, text]) => `<option value="${psEsc(v)}" ${v === value ? 'selected' : ''}>${psEsc(text)}</option>`).join('')}</select>`
      : `<input class="pick" type="${f.type || 'number'}" ${attrs} value="${psEsc(value ?? '')}" ${f.nullable ? '' : 'required'} ${f.min == null ? '' : `min="${f.min}"`} ${f.max == null ? '' : `max="${f.max}"`} step="${f.step || (f.type === 'time' ? 60 : 1)}">`;
    return `<label class="field" for="setting-${key}"><span class="lab">${label}${help}</span>${input}</label>`;
  }

  _changes() {
    const patch = {}, expected = {};
    for (const key of this._fields) {
      const f = PS_SETTING_FIELDS[key], input = this.querySelector(`[name="${key}"]`);
      const value = f.type === 'checkbox' ? input.checked : input.value === '' && f.nullable ? null : !f.type ? Number(input.value) : input.value;
      if (value === this._values[key]) continue;
      if (!f.type && value !== null && !Number.isFinite(value)) throw new Error(`${f.label} must be a number.`);
      patch[key] = f.ratio && value !== null ? value / 100 : value;
      expected[key] = value;
    }
    return { patch, expected };
  }

  _error(message) {
    const el = this.querySelector('[role="alert"]');
    el.textContent = message;
    el.hidden = false;
  }

  _busy(saving) {
    this._saving = saving;
    this.querySelectorAll('button, input, select').forEach(el => { el.disabled = saving; });
    this.querySelector('[data-save]').textContent = saving ? 'Saving…' : 'Save changes';
  }

  async _save() {
    if (this._saving || !this.querySelector('form').reportValidity()) return;
    this.querySelector('[role="alert"]').hidden = true;
    try {
      const { patch, expected } = this._changes();
      if (!Object.keys(patch).length) { this._dialog.close(); return; }
      this._busy(true);
      const result = await this._hass.callApi('POST', this._editor.path, patch);
      if (!result || result.success === false) throw new Error(result?.error || 'The settings update was rejected.');
      const data = await this._load();
      const saved = { ...data, ...(data.config || {}) };
      if (Object.keys(expected).some(key => saved[key] !== expected[key])) {
        throw new Error('The saved values could not be confirmed. Check the settings before saving again.');
      }
      // Other cards must not keep the pre-save optimizer payload.
      psApiCache.clear();
      this.dispatchEvent(new CustomEvent('ps-settings-saved', { detail: this._editor.path, bubbles: true, composed: true }));
      this.dispatchEvent(new CustomEvent('ps-toast', { detail: 'Settings saved', bubbles: true, composed: true }));
      this._dialog.close();
    } catch (err) { this._error(err?.body?.error || err?.body?.message || err?.message || String(err)); }
    finally { if (this.isConnected) this._busy(false); }
  }
}

function psOpenSettings(root, hass, row, opener) {
  if (root.querySelector('ps-settings-dialog')) return;
  const dialog = document.createElement('ps-settings-dialog');
  // Keep the modal outside movable cards so responsive reflow preserves it.
  root.appendChild(dialog);
  dialog.open(hass, row, opener);
}

// Switch, select and number rows bound to entities. Config: { rows: [{ kind:
// 'switch'|'select'|'number', entity, label, help?, labels?: {option: text},
// zeroLabel?, unit? }] }.
let psSettingsCardId = 0;
class PsSettingsCard extends PsCardBase {
  constructor() {
    super();
    this._settingsId = ++psSettingsCardId;
    this.addEventListener('click', e => this._onClick(e));
    this.addEventListener('change', e => this._onChange(e));
  }

  _signature() {
    return (this._config.rows || []).map(r => {
      const s = this._hass?.states?.[r.entity];
      return s ? `${s.state}|${s.last_updated}` : '-';
    }).join(';');
  }

  _render() {
    const hass = this._hass;
    const rows = (this._config.rows || []).filter(r => psState(hass, r.entity));
    if (!rows.length) {
      this.innerHTML = '<p class="empty">None of these settings are available right now.</p>';
      return;
    }
    const html = rows.map(r => {
      const i = this._config.rows.indexOf(r);
      const s = psState(hass, r.entity);
      const unavailable = s.state === 'unavailable';
      const help = r.help ? `<small>${psEsc(r.help)}</small>` : '';
      const label = psEsc(r.label || psFriendly(hass, r.entity));
      const labelHtml = r.editor && PS_SETTING_EDITORS[r.editor]
        ? `<button type="button" class="lab settings-open" id="set-${this._settingsId}-${i}" data-editor="${i}" aria-haspopup="dialog">${label}<span class="settings-chevron" aria-hidden="true">›</span>${help}</button>`
        : `<span class="lab" id="set-${this._settingsId}-${i}">${label}${help}</span>`;
      if (r.kind === 'switch') {
        return `<div class="row2">${labelHtml}<button type="button" class="switch" role="switch" aria-checked="${s.state === 'on'}" aria-labelledby="set-${this._settingsId}-${i}" data-i="${i}" ${unavailable ? 'disabled' : ''}></button></div>`;
      }
      if (r.kind === 'select') {
        const options = Array.isArray(s.attributes?.options) ? s.attributes.options : [];
        const text = o => psEsc(r.labels?.[o] || o);
        if (options.length && options.length <= 3) {
          return `<div class="field"><span class="lab" id="set-${this._settingsId}-${i}">${label}</span><div class="seg" style="--n:${options.length}" role="group" aria-labelledby="set-${this._settingsId}-${i}">${options.map(o => `<button type="button" data-i="${i}" data-opt="${psEsc(o)}" aria-pressed="${o === s.state}" ${unavailable ? 'disabled' : ''}>${text(o)}</button>`).join('')}</div></div>`;
        }
        return `<div class="row2"><span class="lab" id="set-${this._settingsId}-${i}">${label}${help}</span><select class="pick" data-i="${i}" aria-labelledby="set-${this._settingsId}-${i}" ${unavailable ? 'disabled' : ''}>${options.map(o => `<option value="${psEsc(o)}" ${o === s.state ? 'selected' : ''}>${text(o)}</option>`).join('')}</select></div>`;
      }
      if (r.kind === 'number') {
        const v = Number(s.state);
        const min = Number(s.attributes?.min ?? 0), max = Number(s.attributes?.max ?? 100);
        const unit = r.unit ?? s.attributes?.unit_of_measurement ?? '';
        const shown = !Number.isFinite(v) ? '—' : (v === 0 && r.zeroLabel) ? r.zeroLabel : `${+v.toFixed(2)}${unit === '%' ? '%' : unit ? ` ${unit}` : ''}`;
        return `<div class="row2"><span class="lab" id="set-${this._settingsId}-${i}">${label}${help}</span><div class="stepper" role="group" aria-labelledby="set-${this._settingsId}-${i}"><button type="button" data-i="${i}" data-d="-1" aria-label="Lower ${label}" ${unavailable || v <= min ? 'disabled' : ''}>−</button><output class="num">${psEsc(shown)}</output><button type="button" data-i="${i}" data-d="1" aria-label="Raise ${label}" ${unavailable || v >= max ? 'disabled' : ''}>+</button></div></div>`;
      }
      return '';
    }).join('');
    const list = this.querySelector('.switch-list');
    if (list) list.innerHTML = html;
    else this.innerHTML = `<div class="switch-list">${html}</div>`;
  }

  _onClick(e) {
    // Dialog events belong to its draft, never to the underlying card.
    if (e.target.closest('ps-settings-dialog')) return;
    const opener = e.target.closest('button[data-editor]');
    if (opener) {
      psOpenSettings(this.getRootNode(), this._hass, this._config.rows[Number(opener.dataset.editor)], opener);
      return;
    }
    const btn = e.target.closest('button[data-i]');
    if (!btn || btn.disabled) return;
    const r = this._config.rows[Number(btn.dataset.i)];
    if (!r) return;
    if (r.kind === 'switch') {
      const on = btn.getAttribute('aria-checked') === 'true';
      btn.setAttribute('aria-checked', String(!on));
      this._call('homeassistant', on ? 'turn_off' : 'turn_on', { entity_id: r.entity });
    } else if (r.kind === 'select' && btn.dataset.opt != null) {
      this._call('select', 'select_option', { entity_id: r.entity, option: btn.dataset.opt });
    } else if (r.kind === 'number') {
      const s = psState(this._hass, r.entity);
      const step = Number(s.attributes?.step) || 1;
      const min = Number(s.attributes?.min ?? 0), max = Number(s.attributes?.max ?? 100);
      const next = Math.max(min, Math.min(max, (Number(s.state) || 0) + step * (r.stepMultiplier || 1) * Number(btn.dataset.d)));
      this._call('number', 'set_value', { entity_id: r.entity, value: next });
    }
  }

  _onChange(e) {
    if (e.target.closest('ps-settings-dialog')) return;
    const sel = e.target.closest('select[data-i]');
    if (!sel) return;
    const r = this._config.rows[Number(sel.dataset.i)];
    if (r) this._call('select', 'select_option', { entity_id: r.entity, option: sel.value });
  }
}

// Frontend cards that some fallback cards need.
class PsMissingCards extends PsCardBase {
  _signature() { return JSON.stringify(this._config.missing || []); }
  _render() {
    const missing = this._config.missing || [];
    const optional = this._config.optional || [];
    this.innerHTML = `
      ${missing.length ? '<div class="callout amber"><b>Some cards need extra frontend cards</b><span>Install these from HACS, Frontend, then refresh your browser.</span></div>' : ''}
      ${optional.length ? '<div class="callout cyan"><b>Optional frontend cards</b><span>These add extra cards for custom dashboards.</span></div>' : ''}
      <dl class="kv">${[...missing, ...optional].map(c => `<dt>${psEsc(c.name)}</dt><dd class="txt">${psEsc(c.hacs)}</dd>`).join('')}</dl>`;
  }
}

// ─── Right now ───────────────────────────────────────────────────

// A force mode is also used by the optimiser and automations. Only the
// recorded command source can identify who requested it; an enabled plan
// does not establish ownership of an active battery command.
function psBatteryControl(modeState) {
  const request = {
    force_charge: 'Charge', force_discharge: 'Discharge',
    hold_soc: 'Battery hold', self_consumption: 'Self-consumption',
  }[modeState?.state];
  if (!request) return null;
  const rawSource = modeState?.attributes?.control_source;
  const source = typeof rawSource === 'string' ? rawSource.trim().toLowerCase() : '';
  const origins = {
    optimizer: ['Optimiser control', ' by the optimiser', 'on'],
    user: ['Manual override', ' manually', 'override'],
    manual: ['Manual override', ' manually', 'override'],
    automation: ['Automation control', ' by an automation', 'mon'],
    aemo_spike: ['Price spike control', ' by the price spike automation', 'mon'],
  };
  const [label, attribution, pill] = Object.hasOwn(origins, source)
    ? origins[source] : ['Battery control active', '', 'override'];
  return {label, pill, message: `${request} requested${attribution}.`};
}

class PsNowCard extends PsCardBase {
  _signature() {
    const data = psApiPeek('power_sync/optimization');
    return `${super._signature()}#${data?.last_optimization || ''}|${data?.current_action || ''}|${data?.next_action_time || ''}`;
  }

  _render() {
    const hass = this._hass, en = this.ents;
    const data = this._api('power_sync/optimization');
    const soc = psNum(hass, en.soc);
    const solar = psKw(hass, en.solar), home = psKw(hass, en.home);
    const battery = psKw(hass, en.battery), grid = psKw(hass, en.grid);
    const imp = psMinor(hass, en.importPrice), exp = psMinor(hass, en.exportPrice);
    const unit = psMinorUnit(hass);
    const modeState = psState(hass, en.mode);
    const mode = modeState?.state || 'normal';
    const control = psBatteryControl(modeState);
    const remaining = Number(modeState?.attributes?.remaining_minutes);
    const monitoring = data ? !!data.monitoring_mode : psState(hass, en.monitoring)?.state === 'on';
    const enabled = data ? !!data.enabled : psState(hass, en.optimizer)?.state === 'on';
    const { rows } = psSchedule(data);
    const now = Date.now();

    // Mode pill
    let pill = '', modeText = '';
    if (mode !== 'normal' && mode !== 'unknown' && mode !== 'unavailable') {
      pill = `<span class="pill ${control?.pill || 'override'}">${psEsc(control?.label || 'Battery control active')}</span>`;
      modeText = Number.isFinite(remaining) ? `Current command: ${psDuration(remaining)} remaining` : 'Current battery command';
    } else if (monitoring) {
      pill = '<span class="pill mon">Monitoring</span>';
      modeText = 'The optimizer is planning but not controlling the battery';
    } else if (enabled) {
      pill = '<span class="pill on">Optimizing</span>';
      modeText = 'The optimizer is controlling the battery';
    } else if (data || en.optimizer) {
      pill = '<span class="pill off">Optimizer off</span>';
      modeText = 'The battery follows its own settings';
    }

    const commandStatus = modeState?.attributes?.command_status;
    const restoring = commandStatus === 'restore_pending';
    const starting = commandStatus === 'pending';
    if (restoring) modeText = 'Waiting for restoration to finish';
    else if (starting) modeText = 'Waiting for command acknowledgement';

    // Headline
    const actKey = psActionKey(data?.effective_current_action || data?.current_action);
    const { interval } = psSchedule(data);
    const runEnd = psPlannedRunEnd(rows, interval, actKey, now);
    let headline;
    if (restoring) headline = 'Restoring normal operation…';
    else if (starting) headline = mode === 'force_charge' ? 'Starting charge…' : 'Starting discharge…';
    else if (mode === 'force_charge' || mode === 'force_discharge') headline = control.message;
    else if (mode === 'hold_soc') headline = `Holding the battery at ${psFix(soc, 0)}%.`;
    else if (mode === 'self_consumption') headline = 'The battery is powering the home.';
    else if (actKey === 'charge') headline = `Charging${imp <= 0.05 ? ' on free power' : solar > Math.max(0.2, -battery) ? ' from solar' : ''}.`;
    else if (actKey === 'export') headline = 'Exporting to the grid.';
    else if (actKey === 'hold') headline = `Holding the battery at ${psFix(soc, 0)}%.`;
    else if (actKey === 'self') headline = 'The battery is powering the home.';
    else if (Number.isFinite(battery) && battery < -0.1) headline = 'The battery is charging.';
    else if (Number.isFinite(battery) && battery > 0.1) headline = 'The battery is powering the home.';
    else headline = 'The battery is idle.';

    // Lead: next step and the plan's low point in the next 24 hours.
    const parts = [];
    const planDriven = !restoring && !starting && !PS_MODE_TEXT[mode];
    if (planDriven && runEnd && runEnd - now >= 20 * 60_000) {
      parts.push(`The plan keeps this going until <span class="num">${psEsc(psWhen(hass, runEnd))}</span>.`);
    }
    const nextChange = psNextChange(data, actKey, now);
    if (nextChange) {
      parts.push(`Next it will ${PS_ACT[nextChange.key][0].toLowerCase()} at <span class="num">${psEsc(psWhen(hass, nextChange.t))}</span>.`);
    }
    const day = rows.filter(r => r.t >= now && r.t <= now + 24 * 3_600_000 && Number.isFinite(r.soc));
    let low = null;
    if (day.length) {
      low = day.reduce((a, b) => (b.soc < a.soc ? b : a));
      if (Number.isFinite(soc) && low.soc < soc - 1) {
        parts.push(`The plan takes the battery down to <span class="num">${Math.round(low.soc)}%</span> at <span class="num">${psEsc(psWhen(hass, low.t))}</span>.`);
      }
    }
    const lead = parts.join(' ');

    // Gauge
    const floor = psPercent(data?.reserve_visibility?.active_software_floor_percent ?? data?.reserve_recommendation?.recommended_percent);
    const left = psKwh(hass, en.energyLeft), cap = psKwh(hass, en.capacity);
    const backup = psNum(hass, en.backup);
    const socPct = Number.isFinite(soc) ? Math.max(0, Math.min(100, soc)) : 0;
    const rateWord = !Number.isFinite(battery) || Math.abs(battery) < 0.05 ? 'Idle' : battery < 0 ? 'Charging' : 'Discharging';
    const gauge = Number.isFinite(soc) ? `
      <div class="gauge" aria-label="Battery ${Math.round(soc)} percent">
        <div class="cell" aria-hidden="true">
          ${low ? `<div class="low" style="height:${Math.max(0, Math.min(100, low.soc))}%"></div>` : ''}
          <div class="fill" style="height:${socPct}%"></div>
          ${Number.isFinite(floor) && floor > 0 ? `<div class="res" style="bottom:${Math.min(100, floor)}%"></div>` : ''}
        </div>
        <div class="gauge-read">
          <div class="soc num">${Math.round(soc)}<small>%</small></div>
          <span>${rateWord}${rateWord === 'Idle' ? '' : ` <b class="num">${psKwText(battery)}</b>`}</span>
          ${Number.isFinite(left) && Number.isFinite(cap) && cap > 0 ? `<span><b class="num">${psFix(left)}</b> of <b class="num">${psFix(cap)} kWh</b></span>` : ''}
          ${Number.isFinite(floor) && floor > 0 ? `<span>Export floor <b class="num">${Math.round(floor)}%</b></span>` : ''}
          ${Number.isFinite(backup) && backup > 0 ? `<span>About <b class="num">${backup >= 10 ? Math.round(backup) : psFix(backup)} h</b> of backup</span>` : ''}
        </div>
      </div>` : '';

    // Readouts
    const read = (label, color, kw, sub, idle) => {
      const zero = !Number.isFinite(kw) || Math.abs(kw) < 0.05;
      return `<div class="read ${zero && idle ? 'zero' : ''}" style="--c:var(${color})"><span class="k">${label}</span><span class="v num">${Number.isFinite(kw) ? Math.abs(kw).toFixed(1) : '—'}<small>kW</small></span><span class="s">${psEsc(sub)}</span></div>`;
    };
    const dailySolar = psKwh(hass, en.dailySolar);
    const solarSub = Number.isFinite(solar) && solar >= 0.05 ? 'Generating'
      : Number.isFinite(dailySolar) ? (dailySolar < 0.05 ? 'Nothing generated today' : `${psFix(dailySolar)} kWh today`) : 'Not generating';
    const battSub = !Number.isFinite(battery) || Math.abs(battery) < 0.05 ? 'Idle' : battery < 0 ? 'Charging' : 'Discharging';
    const gridSub = !Number.isFinite(grid) || Math.abs(grid) < 0.05 ? 'Idle'
      : grid > 0 ? `Importing${Number.isFinite(imp) ? ` at ${imp.toFixed(1)} ${unit}/kWh` : ''}`
        : `Exporting${Number.isFinite(exp) ? ` at ${exp.toFixed(1)} ${unit}/kWh` : ''}`;
    const pos = v => (Number.isFinite(v) && v > 0.05 ? v : 0);
    const from = [['--yellow', pos(solar)], ['--green', pos(battery)], ['--cyan', pos(grid)]];
    const to = [['--purple', pos(home)], ['--green', pos(-battery)], ['--red', pos(-grid)]];
    const bar = list => list.filter(([, v]) => v > 0).map(([c, v]) => `<span style="--c:var(${c});flex:${v.toFixed(2)}"></span>`).join('');

    this.innerHTML = `
      <div class="now-grid">
        <div>
          ${pill ? `<div class="mode">${pill}<span>${psEsc(modeText)}</span></div>` : ''}
          <h1>${psEsc(headline)}</h1>
          ${lead ? `<p class="lead">${lead}</p>` : ''}
        </div>
        ${gauge}
      </div>
      <div class="power">
        ${en.solar ? read('Solar', '--yellow', solar, solarSub, true) : ''}
        ${en.home ? read('Home', '--purple', home, 'Using', false) : ''}
        ${en.battery ? read('Battery', '--green', battery, battSub, true) : ''}
        ${en.grid ? read('Grid', grid < -0.05 ? '--red' : '--cyan', grid, gridSub, true) : ''}
        <div class="balance" aria-hidden="true">
          <span>From</span><div class="bar">${bar(from)}</div>
          <span>To</span><div class="bar">${bar(to)}</div>
        </div>
      </div>`;
  }
}

// ─── Battery controls ────────────────────────────────────────────

const PS_DURATIONS = [5, 15, 30, 60, 120, 240];
const PS_CONTROL_KINDS = {
  charge: { service: 'force_charge', label: 'Charge', done: 'Charging' },
  discharge: { service: 'force_discharge', label: 'Discharge', done: 'Discharging' },
  self: { service: 'set_self_consumption', label: 'Power the home', done: 'Powering the home' },
  hold: { service: 'hold_battery_soc', label: 'Hold charge', done: 'Holding charge' },
};
const PS_MODE_TEXT = {
  force_charge: ['Charging', 'Charge requested.'],
  force_discharge: ['Discharging', 'Discharge requested.'],
  self_consumption: ['Powering the home', 'The battery is powering the home.'],
  hold_soc: ['Holding charge', 'Holding the battery at its current charge.'],
};

function psUntilPreview(value, timeZone, now = Date.now()) {
  if (!/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(value)) return { error: 'Enter a finish time in HH:mm format.' };
  if (!timeZone) return { error: 'Home Assistant timezone is unavailable.' };
  try {
    const clock = new Intl.DateTimeFormat('en-GB', { timeZone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    const day = new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' });
    for (let at = (Math.floor(now / 60000) + 1) * 60000; at <= now + 48 * 3600000; at += 60000) {
      if (clock.format(at) !== value) continue;
      const minutes = Math.ceil((at - now) / 60000);
      if (minutes > 1440) return { error: 'The next occurrence exceeds the 24-hour maximum. Choose a shorter window.' };
      const tomorrow = day.format(at) !== day.format(now);
      return { minutes, label: `until ${value}${tomorrow ? ' tomorrow' : ' today'} (${timeZone}; about ${psDuration(minutes)})` };
    }
  } catch (_) { return { error: 'Home Assistant timezone is unavailable.' }; }
  return { error: 'No upcoming finish time is available.' };
}

class PsControlsCard extends PsCardBase {
  constructor() {
    super();
    this._duration = null;
    this._customEnabled = null;
    this._hours = null;
    this._minutes = null;
    this._timingMode = 'duration';
    this._until = '';
    this._confirmToken = null;
    this._saveTimer = null;
    this.addEventListener('input', e => this._onTimingInput(e));
    this.addEventListener('change', e => { if (e.target.dataset?.timingInput) this._saveCustomDuration(); });
    this._confirm = null;
    this._confirmTimer = null;
    this.addEventListener('click', e => this._onClick(e));
  }

  _signature() {
    return `${super._signature()}#${this._duration}|${this._customEnabled}|${this._hours}|${this._minutes}|${this._timingMode}|${this._until}|${this._confirm}|${this._hass?.config?.time_zone}|${Math.floor(Date.now() / 60000)}|${psApiPeek('power_sync/optimization')?.monitoring_mode}`;
  }

  _defaultDuration() {
    const v = Number(psState(this._hass, this.ents.chargeDuration)?.state);
    return PS_DURATIONS.includes(v) ? v : 30;
  }

  _render() {
    const hass = this._hass, en = this.ents;
    this._initTiming();
    const focused = this.querySelector('[data-timing-input]:focus');
    const focus = focused ? { key: focused.dataset.timingInput, start: focused.selectionStart, end: focused.selectionEnd } : null;
    if (this._duration == null) this._duration = this._defaultDuration();
    const data = this._api('power_sync/optimization');
    const monitoring = data ? !!data.monitoring_mode : psState(hass, en.monitoring)?.state === 'on';
    const modeState = psState(hass, en.mode);
    const mode = modeState?.state || 'normal';
    const control = psBatteryControl(modeState);
    const status = modeState?.attributes?.command_status;
    const restoring = status === 'restore_pending';
    const starting = status === 'pending';
    const active = restoring ? ['Restoring…', `${control ? `${control.label}. ` : ''}Restoration is in progress; waiting for confirmation.`]
      : starting ? [mode === 'force_charge' ? 'Starting charge…' : 'Starting discharge…', `${control ? `${control.label}. ` : ''}Waiting for command acknowledgement.`]
      : control ? [control.label, control.message] : PS_MODE_TEXT[mode];
    const remaining = restoring || starting ? NaN : Number(modeState?.attributes?.remaining_minutes);
    const power = psState(hass, en.forcePower);
    const powerKw = Number(power?.state);
    if (this._confirm && (this._confirmPower !== power?.state || this._confirmTimezone !== hass?.config?.time_zone)) this._clearConfirm();
    const timing = this._timingRequest();
    const btn = (kind, cls) => {
      const k = PS_CONTROL_KINDS[kind];
      const confirming = this._confirm === kind;
      return `<button type="button" class="btn ${cls}${confirming ? ' confirming' : ''}" data-kind="${kind}">${starting && mode === `force_${kind}` ? `Starting ${kind}…` : confirming ? `Confirm ${k.label.toLowerCase()}` : k.label}</button>`;
    };
    this.innerHTML = `
      ${monitoring ? '<div class="callout cyan"><b>Monitoring mode is on.</b><span>The optimizer won\'t act on its plan, but these controls still change the battery.</span></div>' : ''}
      ${active ? `<div class="callout amber"><b>${psEsc(active[0])}</b><span>${psEsc(active[1])}${Number.isFinite(remaining) ? ` ${psDuration(remaining)} left.` : ''}</span></div>` : ''}
      <div class="field"><span class="lab" id="dur-l">Run for</span>
        <div class="chipsel" role="group" aria-labelledby="dur-l">${this._durations().map(m => `<button type="button" data-min="${m}" aria-pressed="${m === this._duration}">${m < 60 ? `${m}m` : `${m / 60}h`}</button>`).join('')}</div>
      </div>
      <div class="row2"><span class="lab" id="cust-l">Custom charge/discharge time<small>Applies to Charge and Discharge</small></span><button type="button" class="switch" role="switch" aria-checked="${this._customOn()}" aria-labelledby="cust-l" data-custom-toggle></button></div>
      ${this._customOn() ? `<div class="chipsel" role="group" aria-label="Custom timing mode"><button type="button" data-timing-mode="duration" aria-pressed="${this._timingMode === 'duration'}">Run for</button><button type="button" data-timing-mode="until" aria-pressed="${this._timingMode === 'until'}">Until</button></div>
      ${this._timingMode === 'duration' ? `<div class="timing-editor"><button type="button" class="tbtn" data-step="custom" data-d="-1" aria-label="Shorten custom duration by one minute">−</button><label><span class="timing-label">Hours</span> <input class="timing-input" type="text" inputmode="numeric" data-timing-input="hours" value="${psEsc(this._hours)}" aria-label="Custom duration hours"></label><label><span class="timing-label">Minutes</span> <input class="timing-input" type="text" inputmode="numeric" data-timing-input="minutes" value="${psEsc(this._minutes)}" aria-label="Custom duration minutes"></label><button type="button" class="tbtn" data-step="custom" data-d="1" aria-label="Lengthen custom duration by one minute">+</button></div>` : `<label class="row2"><span class="lab">Finish at<small>${psEsc(hass?.config?.time_zone || 'Home Assistant timezone unavailable')}</small></span><input class="timing-input clock" type="text" placeholder="HH:mm" maxlength="5" data-timing-input="until" value="${psEsc(this._until)}" aria-label="Finish time HH:mm"></label>`}` : ''}
      <p class="timing-summary" role="status">${psEsc(timing.error || `Charge / Discharge: ${timing.label}`)}</p>
      <div class="actions">
        ${btn('charge', 'primary')}${btn('discharge', 'amber')}${btn('self', 'quiet')}${btn('hold', 'quiet')}
        ${active ? `<button type="button" class="btn danger full${this._confirm === 'restore' ? ' confirming' : ''}" data-kind="restore">${restoring ? 'Restoring…' : this._confirm === 'restore' ? 'Confirm restore normal' : 'Restore normal'}</button>` : ''}
      </div>
      ${power ? `<div class="row2"><span class="lab">Power limit<small>Max uses the fastest rate allowed</small></span><div class="stepper"><button type="button" data-step="power" data-d="-1" aria-label="Lower power limit">−</button><output class="num">${Number.isFinite(powerKw) && powerKw > 0 ? `${powerKw.toFixed(1)} kW` : 'Max'}</output><button type="button" data-step="power" data-d="1" aria-label="Raise power limit">+</button></div></div>` : ''}
      <p class="hint">Tap a button twice to confirm. Hold charge and Power the home use the Run for preset.</p>`;
    if (focus) {
      const input = this.querySelector(`[data-timing-input="${focus.key}"]`);
      input?.focus({ preventScroll: true });
      if (input && focus.start != null) input.setSelectionRange(focus.start, focus.end);
    }
  }

  _initTiming() {
    if (this._duration == null) this._duration = this._defaultDuration();
    if (this._customEnabled == null) {
      this._customEnabled = psState(this._hass, this.ents.dischargeDuration)?.state === 'Custom';
      const saved = Number(psState(this._hass, this.ents.customDischarge)?.state);
      this._setCustomMinutes(Number.isInteger(saved) && saved >= 1 && saved <= 1440 ? saved : this._duration);
    }
  }

  _customOn() {
    this._initTiming();
    return this._customEnabled;
  }

  _setCustomMinutes(minutes) {
    this._hours = String(Math.floor(minutes / 60));
    this._minutes = String(minutes % 60);
  }

  _customMinutes() {
    if (!/^[0-9]+$/.test(this._hours) || !/^[0-9]+$/.test(this._minutes)) return NaN;
    const hours = Number(this._hours), minutes = Number(this._minutes);
    const total = hours * 60 + minutes;
    return minutes <= 59 && total >= 1 && total <= 1440 ? total : NaN;
  }

  _timingRequest() {
    this._initTiming();
    if (!this._customOn()) return { data: { duration: String(this._duration) }, label: `for ${psDuration(this._duration)}` };
    if (this._timingMode === 'until') {
      const preview = psUntilPreview(this._until, this._hass?.config?.time_zone);
      return preview.error ? preview : { ...preview, data: { until: this._until } };
    }
    const minutes = this._customMinutes();
    return Number.isFinite(minutes)
      ? { data: { duration: String(minutes) }, label: `for ${psDuration(minutes)}` }
      : { error: 'Enter a duration from 1 minute to 24 hours (minutes 0–59).' };
  }

  _clearConfirm() {
    clearTimeout(this._confirmTimer);
    this._confirm = null;
    this._confirmToken = null;
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._clearConfirm();
    clearTimeout(this._saveTimer);
  }

  _setCustomEnabled(enabled, preset = this._duration) {
    this._customEnabled = enabled;
    const entity_id = this.ents.dischargeDuration;
    const state = psState(this._hass, entity_id);
    const option = enabled ? 'Custom' : String(preset);
    if (entity_id && state?.attributes?.options?.includes(option) && state.state !== option) {
      this._call('select', 'select_option', { entity_id, option });
    }
  }

  _onTimingInput(e) {
    const key = e.target.dataset?.timingInput;
    if (!['hours', 'minutes', 'until'].includes(key)) return;
    this[`_${key}`] = e.target.value;
    this._clearConfirm();
    clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(() => this._saveCustomDuration(), 400);
    this._invalidate();
  }

  _saveCustomDuration() {
    if (this._timingMode !== 'duration') return;
    const value = this._customMinutes(), id = this.ents.customDischarge;
    if (id && Number.isFinite(value) && Number(psState(this._hass, id)?.state) !== value) {
      this._call('number', 'set_value', { entity_id: id, value });
    }
  }

  _durations() {
    const opts = psState(this._hass, this.ents.chargeDuration)?.attributes?.options;
    const allowed = Array.isArray(opts) ? new Set(opts.map(Number)) : null;
    const list = PS_DURATIONS.filter(m => !allowed || allowed.has(m));
    return list.length ? list : PS_DURATIONS;
  }

  _armConfirm(kind, token) {
    this._confirm = kind;
    this._confirmToken = token;
    this._confirmPower = psState(this._hass, this.ents.forcePower)?.state;
    this._confirmTimezone = this._hass?.config?.time_zone;
    clearTimeout(this._confirmTimer);
    this._confirmTimer = setTimeout(() => { this._confirm = null; this._invalidate(); }, PS_CONFIRM_MS);
    this._invalidate();
  }

  _onClick(e) {
    const hass = this._hass, en = this.ents;
    this._initTiming();
    const chip = e.target.closest('button[data-min]');
    if (chip) { this._duration = Number(chip.dataset.min); this._setCustomEnabled(false); this._clearConfirm(); this._invalidate(); return; }
    const timingMode = e.target.closest('[data-timing-mode]');
    if (timingMode) { this._timingMode = timingMode.dataset.timingMode; this._clearConfirm(); this._invalidate(); return; }
    if (e.target.closest('[data-custom-toggle]')) {
      this._setCustomEnabled(!this._customOn());
      this._clearConfirm();
      this._invalidate();
      return;
    }
    const step = e.target.closest('button[data-step]');
    if (step) {
      this._clearConfirm();
      if (step.dataset.step === 'custom') {
        const value = this._customMinutes();
        if (!Number.isFinite(value)) return;
        this._setCustomMinutes(Math.max(1, Math.min(1440, value + Number(step.dataset.d))));
        this._saveCustomDuration();
      } else {
        const id = en.forcePower, state = psState(hass, id);
        if (!state) return;
        const inc = Number(state.attributes?.step) || 1;
        const min = Number(state.attributes?.min ?? 0), max = Number(state.attributes?.max ?? 100);
        const next = Math.max(min, Math.min(max, (Number(state.state) || 0) + inc * Number(step.dataset.d)));
        this._call('number', 'set_value', { entity_id: id, value: Math.round(next * 100) / 100 });
      }
      this._invalidate();
      return;
    }
    const b = e.target.closest('button[data-kind]');
    if (!b) return;
    const kind = b.dataset.kind;
    const customKind = kind === 'charge' || kind === 'discharge';
    const timing = customKind ? this._timingRequest() : { data: { duration: String(this._duration) }, label: `for ${psDuration(this._duration)}` };
    if (kind !== 'restore' && timing.error) { this._clearConfirm(); this._toast(timing.error); this._invalidate(); return; }
    const kw = Number(psState(hass, en.forcePower)?.state);
    const data = { ...timing.data };
    if (customKind && Number.isFinite(kw) && kw > 0) data.power_w = Math.round(kw * 1000);
    const token = JSON.stringify([kind, data, timing.label]);
    if (this._confirm !== kind || this._confirmToken !== token) { this._armConfirm(kind, token); return; }
    this._clearConfirm();
    if (kind === 'restore') {
      this._call('power_sync', 'restore_normal').then(ok => ok && this._toast('Restoring normal operation'));
      this._invalidate();
      return;
    }
    const k = PS_CONTROL_KINDS[kind];
    this._call('power_sync', k.service, data).then(ok => ok && this._toast(`${k.label} requested ${timing.label}`));
    this._invalidate();
  }

}

// ─── Optimizer plan ──────────────────────────────────────────────
// Last 12 hours measured (history), next 24 hours planned (optimizer).

class PsPlanCard extends PsCardBase {
  constructor() {
    super();
    this._wantsResize = true;
    this._hist = {};
    this._histAt = 0;
    this._scrubT = null;
    this._geo = null;
    this._model = null;
  }

  refresh() {
    psApi(this._hass, 'power_sync/optimization', PS_API_TTL_MS, true).then(() => {
      this._invalidate();
      this._toast('Plan refreshed');
    });
  }

  _signature() {
    const data = psApiPeek('power_sync/optimization');
    return `${data?.last_optimization || ''}|${data?.monitoring_mode}|${this._histAt}|${psState(this._hass, this.ents.soc)?.state}`;
  }

  _loadHistory() {
    const en = this.ents;
    if (Date.now() - this._histAt < PS_HISTORY_TTL_MS) return;
    this._histAt = Date.now();
    psHistory(this._hass, [en.soc, en.battery, en.grid, en.importPrice, en.exportPrice], new Date(Date.now() - 12.5 * 3_600_000))
      .then(hist => { this._hist = hist; this._histAt = Date.now(); this._invalidate(); });
  }

  _buildModel(data) {
    const hass = this._hass, en = this.ents;
    const now = new Date();
    const nowH = psHoursFromMidnight(now, now);
    const { rows, interval } = psSchedule(data);
    const H = this._hist;
    const toH = ms => psHoursFromMidnight(new Date(ms), now);
    const fromH = h => new Date(new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() + h * 3_600_000).getTime();
    const plan = rows.map(r => ({ ...r, h: toH(r.t) }));
    const planAt = h => {
      let row = null;
      for (const r of plan) { if (r.h <= h + 1e-6) row = r; else break; }
      return row && h < row.h + interval / 60 ? row : null;
    };
    const unitMinor = s => {
      const unit = String(psState(hass, s)?.attributes?.unit_of_measurement || '').toLowerCase();
      return /c\/|p\/|cent|pence/.test(unit) ? 1 : 100;
    };
    const histVal = (id, h, scale = 1) => {
      const v = psAt(H[id], fromH(h));
      return Number.isFinite(v) ? v * scale : NaN;
    };
    const kwScale = id => (String(psState(hass, id)?.attributes?.unit_of_measurement || '').toLowerCase() === 'w' ? 0.001 : 1);
    const socLive = psNum(hass, en.soc);
    const m = {
      nowH, interval, plan,
      start: nowH - 12, end: nowH + 24,
      imp: h => (h < nowH ? histVal(en.importPrice, h, unitMinor(en.importPrice)) : planAt(h)?.imp ?? NaN),
      exp: h => (h < nowH ? histVal(en.exportPrice, h, unitMinor(en.exportPrice)) : planAt(h)?.exp ?? NaN),
      soc: h => {
        if (h <= nowH) {
          const v = histVal(en.soc, h);
          return Number.isFinite(v) ? v : (Math.abs(h - nowH) < 0.25 ? socLive : NaN);
        }
        const r = planAt(h);
        return r ? r.soc : NaN;
      },
      act: h => {
        if (h < nowH) {
          const bp = histVal(en.battery, h, kwScale(en.battery));
          const g = histVal(en.grid, h, kwScale(en.grid));
          if (!Number.isFinite(bp)) return '';
          if (bp < -0.3) return 'charge';
          if (bp > 0.3) return Number.isFinite(g) && g < -0.3 ? 'export' : 'self';
          return 'hold';
        }
        return planAt(h)?.act || '';
      },
      car: h => (h < nowH ? 0 : planAt(h)?.evKw || 0),
    };
    m.hasCar = plan.some(r => r.evKw > 0);
    const prices = [];
    for (let h = m.start; h < m.end; h += 0.25) prices.push(m.imp(h), m.exp(h));
    const finite = prices.filter(Number.isFinite);
    m.pMin = Math.min(0, ...finite);
    m.pMax = psNiceMax(finite, 20);
    if (m.pMin < 0) m.pMin = -psNiceMax(finite.filter(v => v < 0).map(v => -v), 5);
    return m;
  }

  _render() {
    const hass = this._hass;
    const data = this._api('power_sync/optimization');
    this._loadHistory();
    if (!data) {
      this.innerHTML = '<p class="empty">Loading the optimizer plan.</p>';
      this._model = null;
      return;
    }
    if (!data.schedule?.timestamps?.length) {
      this.innerHTML = `<p class="empty">${psEsc(data.status_message || 'The optimizer has no plan yet. It plans again within a few minutes.')}</p>`;
      this._model = null;
      return;
    }
    const m = this._model = this._buildModel(data);
    const now = Date.now();
    const unit = psMinorUnit(hass);
    const curKey = psActionKey(data.effective_current_action || data.current_action);
    const nextChange = psNextChange(data, curKey, now);
    const nowRunEnd = psPlannedRunEnd(m.plan, m.interval, curKey, now);
    const floor = psPercent(data.reserve_visibility?.active_software_floor_percent);
    const zerohero = data.daily_cost_breakdown?.zerohero;
    const bonusCap = zerohero?.status === 'enabled' && zerohero.bonus_export_cap_kwh != null
      ? Number(zerohero.bonus_export_cap_kwh) : NaN;
    const hasBonusCap = Number.isFinite(bonusCap) && bonusCap >= 0;
    const actChip = key => (key === 'charge' ? 'gr' : key === 'export' ? 'rd' : key === 'self' ? 'pu' : '');
    const chips = [
      `<span class="chip ${data.monitoring_mode ? 'cy' : data.enabled ? 'gr' : ''}"><span>Mode</span><b>${data.monitoring_mode ? 'Monitoring' : data.enabled ? 'Optimizing' : 'Off'}</b></span>`,
      curKey ? `<span class="chip ${actChip(curKey)}"><span>Now</span><b>${psEsc(PS_ACT[curKey][0])}${nowRunEnd ? ` until ${psEsc(psWhen(hass, nowRunEnd))}` : ''}</b></span>` : '',
      nextChange ? `<span class="chip ${actChip(nextChange.key)}"><span>Next control</span><b>${psEsc(PS_ACT[nextChange.key][0])} ${psEsc(psWhen(hass, nextChange.t))}</b></span>` : '',
      data.last_optimization ? `<span class="chip"><span>Optimized</span><b class="num">${psEsc(psTime(hass, data.last_optimization))}</b></span>` : '',
      Number.isFinite(floor) && floor > 0 ? `<span class="chip"><span>Export floor</span><b class="num">${Math.round(floor)}%</b></span>` : '',
      hasBonusCap ? `<span class="chip"><span>Super Export bonus cap</span><b class="num">${psFix(bonusCap)} kWh/day</b></span>` : '',
      Number.isFinite(Number(data.predicted_cost)) ? `<span class="chip"><span>Cost today</span><b class="num">${psEsc(psMoney(hass, data.predicted_cost))}</b></span>` : '',
      Number.isFinite(Number(data.predicted_savings)) && Number(data.predicted_savings) > 0 ? `<span class="chip gr"><span>Saved today</span><b class="num">${psEsc(psMoney(hass, data.predicted_savings))}</b></span>` : '',
      data.is_stale || data.optimization_status === 'stale' ? '<span class="chip am"><span>Plan</span><b>Out of date</b></span>' : '',
    ].join('');

    // Upcoming charge and export windows.
    const wins = [
      ...psWindows(m.plan, m.interval, 'charge', now).map(w => ({ ...w, kind: 'charge' })),
      ...psWindows(m.plan, m.interval, 'solar', now).map(w => ({ ...w, kind: 'solar' })),
      ...psWindows(m.plan, m.interval, 'export', now).map(w => ({ ...w, kind: 'export' })),
    ].filter(w => w.start < now + 36 * 3_600_000).sort((a, b) => a.start - b.start).slice(0, 4);
    const winHtml = wins.map(w => {
      const charging = w.kind !== 'export';
      const kw = w.rows.map(r => (charging ? r.chargeKw : r.exportKw));
      const hours = r => Math.max(0, Math.min(r.t + m.interval * 60_000, w.end) - Math.max(r.t, w.start)) / 3_600_000;
      const kwh = w.rows.reduce((a, r, i) => a + kw[i] * hours(r), 0);
      const gridKnown = w.rows.every(r => Number.isFinite(r.gridChargeKw));
      const gridKwh = charging ? w.rows.reduce((a, r) => a + (Number.isFinite(r.gridChargeKw) ? r.gridChargeKw : r.chargeKw) * hours(r), 0) : 0;
      const money = w.rows.reduce((a, r) => {
        const p = charging ? r.imp : r.exportValuePrice;
        const e = (charging ? (Number.isFinite(r.gridChargeKw) ? r.gridChargeKw : r.chargeKw) : r.exportKw) * hours(r);
        return Number.isFinite(p) ? a + e * p / 100 : a;
      }, 0);
      const pricedKwh = charging ? gridKwh : kwh;
      const hasPrice = w.rows.some(r => Number.isFinite(charging ? r.imp : r.exportValuePrice));
      const avg = hasPrice && pricedKwh > 0 ? money / pricedKwh * 100 : NaN;
      const soc0 = w.rows[0]?.soc, soc1 = w.rows[w.rows.length - 1]?.soc;
      const socText = Number.isFinite(soc0) && Number.isFinite(soc1)
        ? (Math.abs(soc1 - soc0) < 1 ? `holds ${Math.round(soc0)}%` : `${Math.round(soc0)}% to ${Math.round(soc1)}%`) : '';
      const peak = Math.max(0, ...kw);
      const gridText = charging && gridKnown && gridKwh > 0 && gridKwh < kwh - 0.01 ? `${psFix(gridKwh)} kWh from grid` : '';
      const desc = [psDuration((w.end - w.start) / 60_000), socText, peak ? psKwText(peak) : '', `${psFix(kwh, kwh < 1 ? 2 : 1)} kWh`, gridText].filter(Boolean).join(', ');
      return `<div class="win"><span class="tag ${w.kind}">${PS_ACT[w.kind][0]}</span><div><b>${psEsc(psWhen(hass, w.start))}–${psEsc(psTime(hass, w.end))}</b><div class="d">${psEsc(desc)}</div></div><div class="c">${psEsc(psMoney(hass, hasPrice || gridKnown && gridKwh === 0 ? money : NaN))}${Number.isFinite(avg) ? `<small>${avg.toFixed(1)} ${unit} avg</small>` : ''}</div></div>`;
    }).join('') || '<p class="empty">No charge or export windows planned in the next day and a half.</p>';

    const rv = data.reserve_visibility || {};
    const pct = v => (Number.isFinite(psPercent(v)) ? `${Math.round(psPercent(v))}%` : null);
    const resRows = [
      ['Export floor in use', pct(rv.active_software_floor_percent)],
      ['Recommended floor', pct(rv.recommended_percent)],
      ['Your minimum', pct(rv.manual_minimum_percent)],
      ['Hardware reserve', pct(rv.hardware_baseline_percent)],
      ['Applies recommended floor', rv.auto_apply_enabled == null ? null : rv.auto_apply_enabled ? 'Yes' : 'No'],
    ].filter(([, v]) => v != null);

    const legend = [
      '<span><i style="--c:var(--cyan)"></i>Import price</span>',
      '<span><i class="dash" style="--c:var(--red)"></i>Export price</span>',
      '<span><i class="line" style="--c:var(--green)"></i>Charge level</span>',
      '<span><i style="--c:var(--green)"></i>Charge</span>',
      '<span><i style="--c:var(--yellow)"></i>Solar charge</span>',
      '<span><i style="--c:var(--purple)"></i>Power the home</span>',
      '<span><i style="--c:var(--hold)"></i>Hold</span>',
      '<span><i style="--c:var(--red)"></i>Export</span>',
      m.hasCar ? '<span><i style="--c:var(--blue)"></i>Car charging</span>' : '',
    ].join('');

    this.innerHTML = `
      <div class="chips">${chips}</div>
      <div class="scrub" aria-live="polite">
        <div><span>Time</span><b data-sc="t"></b></div>
        <div><span>Import</span><b data-sc="i"></b></div>
        <div><span>Export</span><b data-sc="e"></b></div>
        <div><span>Battery</span><b class="actv" data-sc="a"></b></div>
        <div><span>Charge level</span><b data-sc="s"></b></div>
        ${m.hasCar ? '<div><span>Car</span><b data-sc="c"></b></div>' : ''}
      </div>
      <svg class="dayline" role="img" tabindex="0" aria-label="Import and export prices with the battery's measured and planned charge level. Use the left and right arrow keys to step through the hours."></svg>
      <div class="legend" aria-hidden="true">${legend}</div>
      <div class="plan-foot">
        <div class="windows">${winHtml}${hasBonusCap ? `<p class="hint">The daily cap limits premium-rate energy, not total export. ${zerohero.bonus_export_kwh_remaining != null && Number.isFinite(Number(zerohero.bonus_export_kwh_remaining)) ? `${psFix(zerohero.bonus_export_kwh_remaining)} kWh remaining today. ` : ''}Export earnings include the allowance shared with solar export.</p>` : ''}</div>
        ${resRows.length ? `<details class="res"><summary>Reserve details</summary><dl class="kv">${resRows.map(([k, v]) => `<dt>${psEsc(k)}</dt><dd>${psEsc(v)}</dd>`).join('')}</dl></details>` : ''}
      </div>`;
    const svg = this.querySelector('svg.dayline');
    svg.addEventListener('pointermove', e => {
      if (!this._geo) return;
      const r = svg.getBoundingClientRect(), g = this._geo;
      this._setScrub(m.start + ((e.clientX - r.left - g.L) / (g.W - g.L - g.R)) * (m.end - m.start));
    });
    svg.addEventListener('pointerleave', () => this._setScrub(null));
    svg.addEventListener('blur', () => this._setScrub(null));
    svg.addEventListener('keydown', e => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        const base = this._scrubT ?? Math.floor(m.nowH * 4) / 4;
        this._setScrub(base + (e.key === 'ArrowRight' ? 0.5 : -0.5));
      } else if (e.key === 'Escape') this._setScrub(null);
    });
  }

  _draw() {
    const m = this._model;
    const svg = this.querySelector('svg.dayline');
    if (!m || !svg) return;
    const W = svg.clientWidth, H = svg.clientHeight;
    if (!W) return;
    const css = n => psCss(svg, n);
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.textContent = '';
    const narrow = W < 560, L = narrow ? 28 : 50, R = narrow ? 34 : 44, T = 18, bandH = 10, axisH = 22;
    const carY = H - axisH - bandH - 6;
    const actY = m.hasCar ? carY - bandH - 4 : carY;
    const plotB = actY - 10;
    const x = t => L + ((t - m.start) / (m.end - m.start)) * (W - L - R);
    const yP = p => T + (1 - (p - m.pMin) / (m.pMax - m.pMin)) * (plotB - T);
    const yS = s => T + (1 - s / 100) * (plotB - T);
    this._geo = { x, L, R, W, yP, yS };
    const hass = this._hass, now = new Date();

    psSvg('rect', { x: L, y: T, width: Math.max(0, x(m.nowH) - L), height: plotB - T, fill: css('--past') }, svg);
    for (const day of [24, 48]) {
      if (day > m.start && day < m.end) {
        psSvg('line', { x1: x(day), x2: x(day), y1: T, y2: carY + bandH, stroke: css('--border-hi') }, svg);
        psSvg('text', { x: x(day) + 6, y: T + 2, class: 'day-lbl' }, svg).textContent = psFmtHour(hass, day, now).replace(/\s.*$/, '');
      }
    }
    if (m.start < 0) {
      psSvg('line', { x1: x(0), x2: x(0), y1: T, y2: carY + bandH, stroke: css('--border-hi') }, svg);
      psSvg('text', { x: x(0) + 6, y: T + 2, class: 'day-lbl' }, svg).textContent = 'Today';
    }
    psTicks(m.pMin, m.pMax, 3).forEach(p => {
      psSvg('line', { x1: L, x2: W - R, y1: yP(p), y2: yP(p), stroke: p === 0 ? css('--border-hi') : css('--grid-line') }, svg);
      psSvg('text', { x: L - 6, y: yP(p) + 4, 'text-anchor': 'end' }, svg).textContent = `${p}${narrow ? '' : psMinorUnit(hass)}`;
    });
    [0, 50, 100].forEach(s => { psSvg('text', { x: W - R + 6, y: yS(s) + 4 }, svg).textContent = `${s}%`; });

    // Prices as steps.
    const seg = (fn) => {
      let d = '';
      let pen = false;
      for (let t = m.start; t < m.end; t += 0.25) {
        const v = fn(t), t2 = Math.min(t + 0.25, m.end);
        if (!Number.isFinite(v)) { pen = false; continue; }
        d += `${pen ? 'L' : 'M'}${x(t)},${yP(v)} L${x(t2)},${yP(v)} `;
        pen = true;
      }
      return d;
    };
    const li = seg(m.imp), le = seg(m.exp);
    if (li) {
      let fill = '';
      let open = false;
      for (let t = m.start; t < m.end; t += 0.25) {
        const v = m.imp(t), t2 = Math.min(t + 0.25, m.end);
        if (!Number.isFinite(v)) {
          if (open) { fill += `L${x(t)},${yP(0)} Z `; open = false; }
          continue;
        }
        if (!open) { fill += `M${x(t)},${yP(0)} `; open = true; }
        fill += `L${x(t)},${yP(v)} L${x(t2)},${yP(v)} `;
      }
      if (open) fill += `L${x(m.end)},${yP(0)} Z`;
      psSvg('path', { d: fill, fill: css('--import-fill') }, svg);
      psSvg('path', { d: li, fill: 'none', stroke: css('--cyan'), 'stroke-width': 1.75 }, svg);
    }
    if (le) psSvg('path', { d: le, fill: 'none', stroke: css('--red'), 'stroke-width': 1.25, 'stroke-dasharray': '4 3' }, svg);

    // Charge level: solid measured, dashed planned.
    const past = [], fut = [];
    for (let t = m.start; t <= m.end + 0.001; t += 0.25) {
      const tt = Math.min(t, m.end), v = m.soc(tt);
      if (!Number.isFinite(v)) continue;
      (tt <= m.nowH ? past : fut).push([x(tt), yS(v)]);
    }
    const nowSoc = m.soc(m.nowH);
    if (Number.isFinite(nowSoc)) { past.push([x(m.nowH), yS(nowSoc)]); fut.unshift([x(m.nowH), yS(nowSoc)]); }
    const all = [...past, ...fut];
    if (all.length > 1) {
      psSvg('path', { d: `M${all.map(p => p.join(',')).join(' L')} L${all[all.length - 1][0]},${plotB} L${all[0][0]},${plotB} Z`, fill: css('--soc-fill') }, svg);
    }
    if (past.length > 1) psSvg('path', { d: `M${past.map(p => p.join(',')).join(' L')}`, fill: 'none', stroke: css('--green'), 'stroke-width': 2.5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, svg);
    if (fut.length > 1) psSvg('path', { d: `M${fut.map(p => p.join(',')).join(' L')}`, fill: 'none', stroke: css('--green'), 'stroke-width': 2, 'stroke-dasharray': '5 4', 'stroke-linecap': 'round' }, svg);

    const data = psApiPeek('power_sync/optimization');
    const floor = psPercent(data?.reserve_visibility?.active_software_floor_percent);
    if (Number.isFinite(floor) && floor > 0) {
      psSvg('line', { x1: L, x2: W - R, y1: yS(floor), y2: yS(floor), stroke: css('--fg-2'), 'stroke-dasharray': '2 4', opacity: 0.6 }, svg);
      if (!narrow) psSvg('text', { x: L + 6, y: yS(floor) - 5 }, svg).textContent = `Export floor ${Math.round(floor)}%`;
    }
    const future = m.plan.filter(r => r.h > m.nowH && r.h <= m.end && Number.isFinite(r.soc));
    if (future.length) {
      const low = future.reduce((a, b) => (b.soc < a.soc ? b : a));
      if (Number.isFinite(nowSoc) && low.soc < nowSoc - 1) {
        psSvg('circle', { cx: x(low.h), cy: yS(low.soc), r: 3.5, fill: css('--card'), stroke: css('--green'), 'stroke-width': 1.5 }, svg);
        psSvg('text', { x: x(low.h), y: yS(low.soc) - 8, 'text-anchor': 'middle' }, svg).textContent = `${Math.round(low.soc)}%`;
      }
    }

    // Action and car bands.
    for (let t = m.start; t < m.end - 0.001; t += 0.25) {
      const a = m.act(t + 0.01);
      const w = Math.max(0, x(t + 0.25) - x(t) - 0.6), op = t < m.nowH - 0.01 ? 0.45 : 1;
      if (a) psSvg('rect', { x: x(t) + 0.3, y: actY, width: w, height: bandH, fill: css(PS_ACT[a][1]), opacity: op }, svg);
      if (m.hasCar && m.car(t + 0.01) > 0) psSvg('rect', { x: x(t) + 0.3, y: carY, width: w, height: bandH, fill: css('--blue'), opacity: op }, svg);
    }
    if (m.hasCar) psSvg('rect', { x: L, y: carY, width: W - L - R, height: bandH, fill: 'none', stroke: css('--border-subtle'), rx: 2 }, svg);
    if (!narrow) {
      psSvg('text', { x: L - 6, y: actY + 9, 'text-anchor': 'end', class: 'row-lbl' }, svg).textContent = 'Battery';
      if (m.hasCar) psSvg('text', { x: L - 6, y: carY + 9, 'text-anchor': 'end', class: 'row-lbl' }, svg).textContent = 'Car';
    }
    const step = narrow ? 6 : 3;
    for (let h = Math.ceil(m.start / step) * step; h <= m.end; h += step) {
      const label = psTime(hass, new Date(new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() + h * 3_600_000));
      psSvg('text', { x: x(h), y: H - 4, 'text-anchor': 'middle' }, svg).textContent = label;
    }
    psSvg('line', { x1: x(m.nowH), x2: x(m.nowH), y1: T - 6, y2: carY + bandH, stroke: css('--fg'), 'stroke-width': 1.25 }, svg);
    if (Number.isFinite(nowSoc)) psSvg('circle', { cx: x(m.nowH), cy: yS(nowSoc), r: 5, fill: css('--green'), stroke: css('--card'), 'stroke-width': 2 }, svg);
    psSvg('text', { x: x(m.nowH) + 6, y: T + 2, class: 'lbl-now' }, svg).textContent = 'Now';
    const g = psSvg('g', { 'data-scrub': '', 'pointer-events': 'none' }, svg);
    psSvg('line', { 'data-l': '', y1: T, y2: carY + bandH, stroke: css('--cyan'), 'stroke-dasharray': '2 3' }, g);
    psSvg('circle', { 'data-p': '', r: 4, fill: css('--cyan'), stroke: css('--card'), 'stroke-width': 2 }, g);
    psSvg('circle', { 'data-s': '', r: 4, fill: css('--green'), stroke: css('--card'), 'stroke-width': 2 }, g);
    this._setScrub(this._scrubT);
  }

  _setScrub(t) {
    const m = this._model;
    if (!m) return;
    const hass = this._hass, unit = psMinorUnit(hass);
    const isNow = t == null;
    const tt = isNow ? m.nowH : Math.max(m.start, Math.min(m.end - 0.01, t));
    this._scrubT = isNow ? null : tt;
    const q = isNow ? m.nowH : Math.floor(tt * 4) / 4;
    const set = (k, text, color) => {
      const el = this.querySelector(`[data-sc="${k}"]`);
      if (!el) return;
      el.textContent = text;
      if (color !== undefined) el.style.color = color;
    };
    const now = new Date();
    set('t', isNow ? `${psTime(hass, now)} now` : psFmtHour(hass, q, now));
    const iv = m.imp(q), ev = m.exp(q);
    set('i', Number.isFinite(iv) ? `${iv.toFixed(1)} ${unit}` : '—');
    set('e', Number.isFinite(ev) ? `${ev.toFixed(1)} ${unit}` : '—');
    const a = m.act(q + 0.01);
    set('a', a ? PS_ACT[a][0] : '—', a && a !== 'hold' ? `var(${PS_ACT[a][1]})` : 'var(--fg)');
    const sv = m.soc(isNow ? m.nowH : q);
    set('s', Number.isFinite(sv) ? `${Math.round(sv)}%${q > m.nowH && !isNow ? ' planned' : ''}` : '—');
    const c = m.car(q + 0.01);
    set('c', c ? `${c.toFixed(1)} kW` : 'None');
    const g = this.querySelector('[data-scrub]'), geo = this._geo;
    if (!g || !geo) return;
    g.style.display = isNow ? 'none' : '';
    if (isNow) return;
    const cx = geo.x(tt);
    const l = g.querySelector('[data-l]'), p = g.querySelector('[data-p]'), s = g.querySelector('[data-s]');
    l.setAttribute('x1', cx); l.setAttribute('x2', cx);
    p.setAttribute('cx', cx); p.setAttribute('cy', Number.isFinite(iv) ? geo.yP(iv) : -20);
    s.setAttribute('cx', cx); s.setAttribute('cy', Number.isFinite(sv) ? geo.yS(sv) : -20);
  }
}

// ─── Tariff today ────────────────────────────────────────────────

class PsTariffCard extends PsCardBase {
  _signature() {
    const s = psState(this._hass, this.ents.tariff);
    const minute = Math.floor(Date.now() / 60_000);
    return `${s?.attributes?.last_sync}|${s?.state}|${minute}`;
  }

  _render() {
    const hass = this._hass;
    const attrs = psState(hass, this.ents.tariff)?.attributes || {};
    const all = Array.isArray(attrs.schedule) ? attrs.schedule : [];
    let slots = all.filter(s => !s.date_label || s.date_label === 'Today');
    if (!slots.length) slots = all.slice(0, 48);
    if (!slots.length) {
      this.innerHTML = '<p class="empty">No tariff has been synced yet.</p>';
      return;
    }
    const slotMin = s => { const [h, mm] = String(s.time || '0:0').split(':').map(Number); return h * 60 + (mm || 0); };
    slots = slots.slice().sort((a, b) => slotMin(a) - slotMin(b));
    const scale = slots.some(s => Number(s.buy) > 2) ? 1 : 100;
    const periods = [];
    slots.forEach((s, i) => {
      const buy = Number(s.buy) * scale, sell = Number(s.sell) * scale;
      const start = slotMin(s);
      const end = i + 1 < slots.length ? slotMin(slots[i + 1]) : 24 * 60;
      const last = periods[periods.length - 1];
      if (last && Math.abs(last.buy - buy) < 0.01 && Math.abs(last.sell - sell) < 0.01 && last.end === start) last.end = end;
      else periods.push({ buy, sell, start, end });
    });
    // Join a period that runs past midnight back onto the morning one.
    if (periods.length > 2) {
      const first = periods[0], last = periods[periods.length - 1];
      if (first.start === 0 && last.end === 1440 && Math.abs(first.buy - last.buy) < 0.01 && Math.abs(first.sell - last.sell) < 0.01) {
        last.wrapEnd = first.end;
      }
    }
    const buys = periods.map(p => p.buy);
    const maxBuy = Math.max(...buys), minBuy = Math.min(...buys);
    const nameMap = {};
    (attrs.tou_schedule || []).forEach(p => { nameMap[Number(p.buy).toFixed(2)] = p.period; });
    const pretty = raw => String(raw).replace(/_/g, ' ').toLowerCase().replace(/^\w/, c => c.toUpperCase());
    const label = p => {
      if (p.buy <= 0.001) return ['Free', '--green', 1];
      const named = nameMap[p.buy.toFixed(2)];
      if (p.buy === maxBuy && maxBuy > minBuy) return [named ? pretty(named) : 'Peak', '--red', 1];
      if (p.buy === minBuy && maxBuy > minBuy) return [named ? pretty(named) : 'Off-peak', '--green', 0.8];
      return [named ? pretty(named) : 'Shoulder', '--cyan', 0.45];
    };
    const now = new Date();
    const nowMin = now.getHours() * 60 + now.getMinutes();
    const hm = mins => psTime(hass, new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, mins));
    const ribbon = periods.map(p => {
      const [, c, op] = label(p);
      return `<span style="--c:var(${c});flex:${p.end - p.start};opacity:${op}"></span>`;
    }).join('');
    const unit = psMinorUnit(hass);
    const shown = periods.filter((p, i) => !(i === 0 && periods[periods.length - 1].wrapEnd === p.end));
    const rowsHtml = shown.map(p => {
      const [name, c] = label(p);
      const isNow = (nowMin >= p.start && nowMin < p.end) || (p.wrapEnd && nowMin < p.wrapEnd);
      const endMin = p.wrapEnd ?? p.end;
      const left = isNow ? ((p.wrapEnd && nowMin < p.wrapEnd ? p.wrapEnd : p.end) - nowMin) : 0;
      const range = `${hm(p.start)}–${endMin === 1440 ? hm(0) : hm(endMin)}`;
      return `<div class="prow ${isNow ? 'now' : ''}"><i style="--c:var(${c})"></i><span class="n"><b>${psEsc(name)}${isNow ? ', now' : ''}</b><span>${psEsc(range)}${isNow ? `, ends in ${psDuration(left)}` : ''}</span></span><span class="i">${p.buy.toFixed(1)}</span><span class="e">${p.sell.toFixed(1)}</span></div>`;
    }).join('');
    this.innerHTML = `
      <div class="tou-ribbon" aria-hidden="true">${ribbon}<i class="now-tick" style="left:calc(${(nowMin / 1440 * 100).toFixed(2)}% - 1px)"></i></div>
      <div class="tou-axis" aria-hidden="true"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
      <div>
        <div class="prow phead"><span></span><span>Period</span><span>Import</span><span>Export</span></div>
        ${rowsHtml}
      </div>
      <p class="hint">Prices in ${psEsc(unit)}/kWh.</p>`;
  }
}

// ─── Prices today ────────────────────────────────────────────────

class PsPricesCard extends PsCardBase {
  constructor() { super(); this._wantsResize = true; this._hist = {}; this._histAt = 0; }

  _signature() {
    return `${super._signature()}#${this._histAt}|${psApiPeek('power_sync/optimization')?.last_optimization}`;
  }

  _render() {
    const hass = this._hass, en = this.ents;
    this._api('power_sync/optimization');
    if (Date.now() - this._histAt > PS_HISTORY_TTL_MS) {
      this._histAt = Date.now();
      const now = new Date();
      psHistory(hass, [en.importPrice, en.exportPrice], new Date(now.getFullYear(), now.getMonth(), now.getDate()))
        .then(h => { this._hist = h; this._histAt = Date.now(); this._invalidate(); });
    }
    const unit = psMinorUnit(hass);
    const imp = psMinor(hass, en.importPrice), exp = psMinor(hass, en.exportPrice);
    this.innerHTML = `
      <div class="stat-grid">
        <div class="stat"><span class="k">Import now</span><span class="v" style="color:var(--cyan)">${psFix(imp)}<small>${unit}/kWh</small></span></div>
        ${en.exportPrice ? `<div class="stat"><span class="k">Export now</span><span class="v" style="color:var(--red)">${psFix(exp)}<small>${unit}/kWh</small></span></div>` : ''}
      </div>
      <svg class="chart" role="img" aria-label="Import and export prices today"></svg>
      <div class="legend"><span><i class="line" style="--c:var(--cyan)"></i>Import</span>${en.exportPrice ? '<span><i class="dash" style="--c:var(--red)"></i>Export</span>' : ''}<span>Solid before now, planned after</span></div>`;
  }

  _draw() {
    const svg = this.querySelector('svg.chart');
    if (!svg) return;
    const hass = this._hass, en = this.ents, now = new Date();
    const scale = id => (/c\/|p\/|cent|pence/.test(String(psState(hass, id)?.attributes?.unit_of_measurement || '').toLowerCase()) ? 1 : 100);
    const { rows } = psSchedule(psApiPeek('power_sync/optimization'));
    const series = (id, key) => {
      const pts = (this._hist[id] || []).map(([t, v]) => [psHoursFromMidnight(new Date(t), now), v * scale(id)]).filter(p => p[0] >= 0);
      const nowH = psHoursFromMidnight(now, now);
      const live = psMinor(hass, id);
      if (Number.isFinite(live)) pts.push([nowH, live]);
      for (const r of rows) {
        const h = psHoursFromMidnight(new Date(r.t), now);
        if (h > nowH && h <= 24 && Number.isFinite(r[key])) pts.push([h, r[key]]);
      }
      if (pts.length) pts.push([24, pts[pts.length - 1][1]]);
      return pts;
    };
    const ip = series(en.importPrice, 'imp');
    const ep = en.exportPrice ? series(en.exportPrice, 'exp') : [];
    const vals = [...ip, ...ep].map(p => p[1]);
    const y1 = psNiceMax(vals, 20);
    const y0 = Math.min(0, ...vals.filter(Number.isFinite));
    const u = psMinorUnit(hass);
    psChart(svg, {
      unit: `${u}/kWh`,
      x0: 0, x1: 24, y0: y0 < 0 ? y0 * 1.1 : -y1 * 0.03, y1, yTicks: psTicks(Math.min(0, y0), y1, 3), yFmt: v => `${v}${u}`,
      tooltipTime: h => psWhen(hass, new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, h * 60)),
      xFmt: h => String(h).padStart(2, '0'), now: psHoursFromMidnight(now, now),
      series: [
        { name: 'Import', pts: ip, c: '--cyan', step: true, fill: '--cyan', fillOp: 0.12 },
        { name: 'Export', pts: ep, c: '--red', step: true, dash: '4 3', w: 1.25 },
      ],
    });
  }
}

// ─── Today so far ────────────────────────────────────────────────

class PsTodayCard extends PsCardBase {
  _signature() { return `${super._signature()}#${psApiPeek('power_sync/optimization')?.predicted_savings}`; }

  _render() {
    const hass = this._hass, en = this.ents;
    const data = this._api('power_sync/optimization');
    const money = id => psNum(hass, id);
    const kwh = id => psKwh(hass, id);
    const imported = kwh(en.gridImport), exported = kwh(en.gridExport);
    const stat = (k, v, s) => `<div class="stat"><span class="k">${psEsc(k)}</span><span class="v">${v}</span>${s ? `<span class="s">${psEsc(s)}</span>` : ''}</div>`;
    const unit = psMinorUnit(hass);
    const stats = [];
    if (en.importCost) stats.push(stat('Grid cost', psEsc(psMoney(hass, money(en.importCost))), Number.isFinite(imported) ? `${psFix(imported)} kWh imported` : ''));
    if (en.exportEarnings) stats.push(stat('Export earnings', psEsc(psMoney(hass, money(en.exportEarnings))), Number.isFinite(exported) ? (exported < 0.05 ? 'Nothing exported' : `${psFix(exported)} kWh exported`) : ''));
    if (en.avgCost) {
      const mtd = psNum(hass, en.mtdAvgCost);
      const avg = psNum(hass, en.avgCost);
      const avgMinor = Number.isFinite(avg) && avg < 5 ? avg * 100 : avg;
      const mtdMinor = Number.isFinite(mtd) && mtd < 5 ? mtd * 100 : mtd;
      stats.push(stat('Average import cost', `${psFix(avgMinor)}<small>${unit}/kWh</small>`, Number.isFinite(mtdMinor) ? `Month to date ${mtdMinor.toFixed(1)} ${unit}` : ''));
    }
    if (en.netCost) {
      const attrs = psState(hass, en.netCost)?.attributes || {};
      const supply = Number(attrs.supply_charge ?? attrs.daily_supply_charge);
      stats.push(stat('Net cost today', psEsc(psMoney(hass, money(en.netCost))), Number.isFinite(supply) ? `With ${psMoney(hass, supply)} supply charge` : 'Includes supply charge'));
    }
    const savings = Number(data?.predicted_savings);
    const kvRows = [
      ['Home used', en.load], ['Battery charged', en.batteryCharge], ['Battery discharged', en.batteryDischarge],
      ['Grid imported', en.gridImport], ['Grid exported', en.gridExport], ['Solar generated', en.solar],
    ].filter(([, id]) => psState(hass, id));
    const extra = (this._config.extraRows || []).filter(r => psState(hass, r.entity));
    this.innerHTML = `
      ${stats.length ? `<div class="stat-grid">${stats.join('')}</div>` : ''}
      ${Number.isFinite(savings) && savings > 0 ? `<div class="callout green"><b>Saving ${psEsc(psMoney(hass, savings))} with the battery today</b><span>Compared with the same day without a battery, as the optimizer estimates it.</span></div>` : ''}
      ${kvRows.length || extra.length ? `<dl class="kv">${kvRows.map(([k, id]) => `<dt>${psEsc(k)}</dt><dd>${psFix(kwh(id))} kWh</dd>`).join('')}${extra.map(r => `<dt>${psEsc(r.name)}</dt><dd class="txt">${psEsc(psFormatState(hass, r.entity, r.attribute))}</dd>`).join('')}</dl>` : ''}`;
  }
}

// ─── Energy since midnight ───────────────────────────────────────

class PsEnergyCard extends PsCardBase {
  constructor() { super(); this._wantsResize = true; this._hist = {}; this._histAt = 0; }

  _signature() { return String(this._histAt); }

  _render() {
    const en = this.ents;
    if (Date.now() - this._histAt > PS_HISTORY_TTL_MS) {
      this._histAt = Date.now();
      const now = new Date();
      psHistory(this._hass, [en.home, en.battery, en.grid, en.solar], new Date(now.getFullYear(), now.getMonth(), now.getDate()))
        .then(h => { this._hist = h; this._histAt = Date.now(); this._invalidate(); });
    }
    this.innerHTML = `
      <svg class="chart" style="height:190px" role="img" aria-label="Home, battery, grid and solar power since midnight"></svg>
      <div class="legend">
        ${en.home ? '<span><i class="line" style="--c:var(--purple)"></i>Home</span>' : ''}
        ${en.battery ? '<span><i class="line" style="--c:var(--green)"></i>Battery, charging above zero</span>' : ''}
        ${en.grid ? '<span><i class="line" style="--c:var(--cyan)"></i>Grid</span>' : ''}
        ${en.solar ? '<span><i class="line" style="--c:var(--yellow)"></i>Solar</span>' : ''}
      </div>`;
  }

  _draw() {
    const svg = this.querySelector('svg.chart');
    if (!svg) return;
    const hass = this._hass, en = this.ents, now = new Date();
    const scale = id => (String(psState(hass, id)?.attributes?.unit_of_measurement || '').toLowerCase() === 'w' ? 0.001 : 1);
    // Downsample to 5-minute steps so long histories stay light.
    const pts = (id, sign = 1) => {
      const raw = this._hist[id] || [];
      const out = [];
      let lastBucket = -1;
      for (const [t, v] of raw) {
        const h = psHoursFromMidnight(new Date(t), now);
        if (h < 0) continue;
        const bucket = Math.floor(h * 12);
        if (bucket === lastBucket) out[out.length - 1] = [h, v * scale(id) * sign];
        else out.push([h, v * scale(id) * sign]);
        lastBucket = bucket;
      }
      const live = psKw(hass, id);
      if (Number.isFinite(live)) out.push([psHoursFromMidnight(now, now), live * sign]);
      return out;
    };
    const series = [
      en.solar ? { name: 'Solar', pts: pts(en.solar), c: '--yellow', w: 1.25 } : null,
      en.grid ? { name: 'Grid', pts: pts(en.grid), c: '--cyan' } : null,
      en.battery ? { name: 'Battery (charging +)', pts: pts(en.battery, -1), c: '--green', fill: '--green', fillOp: 0.1 } : null,
      en.home ? { name: 'Home', pts: pts(en.home), c: '--purple' } : null,
    ].filter(Boolean);
    const vals = series.flatMap(s => s.pts.map(p => p[1])).filter(Number.isFinite);
    const y1 = psNiceMax(vals, 2), y0 = -psNiceMax(vals.filter(v => v < 0).map(v => -v), 1);
    psChart(svg, {
      x0: 0, x1: 24, y0, y1, yTicks: psTicks(y0, y1, 4),
      tooltipTime: h => psWhen(hass, new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, h * 60)),
      xFmt: h => String(h).padStart(2, '0'), now: psHoursFromMidnight(now, now), series,
    });
  }
}

// ─── Forecast cards ──────────────────────────────────────────────

function psForecastSeries(data) {
  const fs = data?.forecast_series;
  if (!fs || !Array.isArray(fs.timestamps)) return null;
  const t = fs.timestamps.map(ts => Date.parse(ts));
  return {
    t,
    interval: Number(fs.interval_minutes) || 30,
    solar: Array.isArray(fs.planned_forecast_values_kw) ? fs.planned_forecast_values_kw.map(Number) : [],
    load: Array.isArray(fs.load_forecast_values_kw) ? fs.load_forecast_values_kw.map(Number) : [],
  };
}

class PsLoadForecastCard extends PsCardBase {
  constructor() { super(); this._wantsResize = true; }

  _signature() { return `${super._signature()}#${psApiPeek('power_sync/optimization')?.last_optimization}`; }

  _render() {
    const hass = this._hass, en = this.ents;
    const fs = psForecastSeries(this._api('power_sync/optimization'));
    const now = Date.now();
    const peak = fs ? Math.max(0, ...fs.load.filter((v, i) => fs.t[i] >= now - 3_600_000 && fs.t[i] <= now + 48 * 3_600_000 && Number.isFinite(v))) : NaN;
    const solar24 = fs ? fs.solar.reduce((a, v, i) => (fs.t[i] >= now && fs.t[i] < now + 24 * 3_600_000 && Number.isFinite(v) ? a + v * fs.interval / 60 : a), 0) : NaN;
    const stat = (k, v, u, color) => `<div class="stat"><span class="k">${psEsc(k)}</span><span class="v"${color ? ` style="color:${color}"` : ''}>${v}<small>${u}</small></span></div>`;
    const stats = [
      en.todayRemaining ? stat('Rest of today', psFix(psKwh(hass, en.todayRemaining)), 'kWh') : '',
      en.tomorrow ? stat('Tomorrow', psFix(psKwh(hass, en.tomorrow)), 'kWh') : '',
      fs && peak > 0 ? stat('Peak load', psFix(peak), 'kW') : '',
      fs ? stat('Solar, next 24 h', psFix(solar24), 'kWh', solar24 < 0.05 ? 'var(--muted)' : 'var(--yellow)') : '',
    ].join('');
    this.innerHTML = `
      ${stats ? `<div class="stat-grid">${stats}</div>` : ''}
      ${fs ? '<svg class="chart" role="img" aria-label="Home load forecast for 48 hours"></svg>' : ''}
      ${psState(hass, en.away)?.state === 'on' ? '<div class="callout amber"><b>Away mode is on</b><span>The forecast assumes a lower home load.</span></div>' : ''}`;
  }

  _draw() {
    const svg = this.querySelector('svg.chart');
    const fs = psForecastSeries(psApiPeek('power_sync/optimization'));
    if (!svg || !fs) return;
    const now = new Date(), nowH = psHoursFromMidnight(now, now);
    const pts = fs.load.map((v, i) => [psHoursFromMidnight(new Date(fs.t[i]), now), v]).filter(p => p[0] >= nowH - 0.5 && p[0] <= nowH + 48);
    const y1 = psNiceMax(pts.map(p => p[1]), 2);
    psChart(svg, {
      x0: nowH, x1: nowH + 48, y0: 0, y1, yTicks: psTicks(0, y1, 3), xFmt: h => psFmtHour(this._hass, h, now),
      tooltipTime: h => psWhen(this._hass, new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, h * 60)),
      series: [{ name: 'Home load', pts, c: '--purple', step: true, fill: '--purple', fillOp: 0.15 }],
    });
  }
}

class PsBatteryPlanCard extends PsCardBase {
  constructor() { super(); this._wantsResize = true; }

  _signature() { return String(psApiPeek('power_sync/optimization')?.last_optimization); }

  _render() {
    const data = this._api('power_sync/optimization');
    if (!data?.schedule?.timestamps?.length) {
      this.innerHTML = '<p class="empty">Waiting for the optimizer plan.</p>';
      return;
    }
    this.innerHTML = `
      <svg class="chart" style="height:180px" role="img" aria-label="Planned battery power for 48 hours"></svg>
      <div class="legend"><span><i style="--c:var(--green)"></i>Charging</span><span><i style="--c:var(--purple)"></i>Discharging</span></div>`;
  }

  _draw() {
    const svg = this.querySelector('svg.chart');
    if (!svg) return;
    const { rows } = psSchedule(psApiPeek('power_sync/optimization'));
    const now = Date.now();
    const shown = rows.filter(r => r.t >= now - 30 * 60_000).slice(0, 48 * 4);
    const vals = shown.map(r => (r.chargeKw > 0 ? r.chargeKw : -(r.dischargeKw || r.exportKw)));
    const y1 = psNiceMax(vals.filter(v => v > 0), 2), y0 = -psNiceMax(vals.filter(v => v < 0).map(v => -v), 2);
    const labels = [];
    shown.forEach((r, i) => {
      const d = new Date(r.t);
      if (i === 0) labels.push([0, psTime(this._hass, d), 'start']);
      else if (d.getHours() === 0 && d.getMinutes() === 0) labels.push([i, psWhen(this._hass, d)]);
    });
    psBars(svg, { times: shown.map(r => psWhen(this._hass, r.t)), vals, y0, y1, yTicks: psTicks(y0, y1, 3), pos: '--green', neg: '--purple', labels });
  }
}

class PsSolarLoadCard extends PsCardBase {
  constructor() { super(); this._wantsResize = true; }

  _signature() { return `${psApiPeek('power_sync/optimization')?.last_optimization}|${psState(this._hass, this.ents.solar)?.state}`; }

  _render() {
    const fs = psForecastSeries(this._api('power_sync/optimization'));
    if (!fs) {
      this.innerHTML = '<p class="empty">Waiting for the next forecast.</p>';
      return;
    }
    const now = Date.now();
    const solarAhead = fs.solar.filter((v, i) => fs.t[i] >= now && Number.isFinite(v));
    const noSolar = solarAhead.length > 0 && Math.max(...solarAhead) < 0.05;
    this.innerHTML = `
      <svg class="chart" style="height:180px" role="img" aria-label="Solar and load forecast for 48 hours"></svg>
      <div class="legend"><span><i class="line" style="--c:var(--purple)"></i>Home load</span><span><i class="line" style="--c:var(--yellow)"></i>Solar</span></div>
      ${noSolar ? '<div class="callout amber"><b>No solar in the forecast</b><span>Check the inverter and the solar forecast source if you expected generation.</span></div>' : ''}`;
  }

  _draw() {
    const svg = this.querySelector('svg.chart');
    const fs = psForecastSeries(psApiPeek('power_sync/optimization'));
    if (!svg || !fs) return;
    const now = new Date(), nowH = psHoursFromMidnight(now, now);
    const toPts = arr => arr.map((v, i) => [psHoursFromMidnight(new Date(fs.t[i]), now), v]).filter(p => p[0] >= nowH - 0.5 && p[0] <= nowH + 48);
    const solar = toPts(fs.solar), load = toPts(fs.load);
    const y1 = psNiceMax([...solar, ...load].map(p => p[1]), 2);
    psChart(svg, {
      x0: nowH, x1: nowH + 48, y0: 0, y1, yTicks: psTicks(0, y1, 3), xFmt: h => psFmtHour(this._hass, h, now),
      tooltipTime: h => psWhen(this._hass, new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, h * 60)),
      series: [{ name: 'Solar', pts: solar, c: '--yellow', w: 2, fill: '--yellow', fillOp: 0.12 }, { name: 'Home load', pts: load, c: '--purple', step: true }],
    });
  }
}

// ─── Registration ────────────────────────────────────────────────

const PS_ELEMENTS = {
  'power-sync-board': PowerSyncBoard,
  'ps-kv-card': PsKvCard,
  'ps-settings-card': PsSettingsCard,
  'ps-settings-dialog': PsSettingsDialog,
  'ps-missing-cards': PsMissingCards,
  'ps-now-card': PsNowCard,
  'ps-controls-card': PsControlsCard,
  'ps-plan-card': PsPlanCard,
  'ps-tariff-card': PsTariffCard,
  'ps-prices-card': PsPricesCard,
  'ps-today-card': PsTodayCard,
  'ps-energy-card': PsEnergyCard,
  'ps-load-forecast-card': PsLoadForecastCard,
  'ps-battery-plan-card': PsBatteryPlanCard,
  'ps-solar-load-card': PsSolarLoadCard,
};
for (const [tag, cls] of Object.entries(PS_ELEMENTS)) {
  if (!customElements.get(tag)) customElements.define(tag, cls);
}

export { PS_DEFAULT_LAYOUT, psSchedule, psWindows, psActionKey };
