"""Runtime regression tests for the optimizer reserve display."""

from __future__ import annotations

from pathlib import Path
import subprocess


STRATEGY_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "frontend"
    / "power-sync-strategy.js"
)


def test_optimizer_reserve_display_uses_trusted_known_values():
    """Reserve details and graph inputs preserve unknown, zero, and active state."""
    runtime_checks = r"""
      const fs = require('fs');
      const vm = require('vm');
      const source = fs.readFileSync(process.argv[1], 'utf8');
      const FIXED_NOW = Date.parse('2026-01-01T00:00:00Z');
      class FixedDate extends Date {
        static now() { return FIXED_NOW; }
      }
      class StubElement {
        attachShadow() { return {}; }
      }
      const registry = new Map();
      const context = {
        window: {},
        HTMLElement: StubElement,
        customElements: {
          get: name => registry.get(name),
          define: (name, value) => registry.set(name, value),
        },
        Date: FixedDate,
        Map,
        Set,
        console,
      };
      vm.createContext(context);
      vm.runInContext(`${source}\nglobalThis.ReservePlan = PowerSyncOptimizationPlan;`, context);
      const plan = new context.ReservePlan();
      const now = '2026-01-01T00:00:00Z';
      const normalized = (overrides = {}) => ({
        manual_minimum_percent: 10,
        recommended_percent: 45,
        active_software_floor_percent: 45,
        hardware_baseline_percent: 10,
        hardware_value_quality: 'saved_baseline',
        auto_apply_enabled: true,
        forecast_quality: 'current',
        forecast_updated_at: now,
        ...overrides,
      });
      const data = reserveVisibility => ({
        reserve_visibility: reserveVisibility,
        config: { interval_minutes: 5 },
      });
      const expect = (condition, message) => {
        if (!condition) throw new Error(message);
      };
      const detail = payload => {
        plan._data = payload;
        return plan._renderReserveDetails();
      };
      const closeTo = (actual, expected, message) =>
        expect(Number.isFinite(actual) && Math.abs(actual - expected) < 1e-9,
          `${message}: expected ${expected}, got ${actual}`);

      // Auto-applied recommendation becomes the active graph floor and all
      // four separately labelled values appear in the rendered details.
      let payload = data(normalized());
      plan._data = payload;
      let graph = plan._optimizerReserve(payload);
      closeTo(graph.percent, 45, 'active graph floor');
      expect(graph.calculated === true, 'auto-applied floor should be calculated');
      let html = detail(payload);
      for (const text of [
        'Manual minimum</dt><dd>10%',
        'Forecast recommendation</dt><dd>45%',
        'Active software export floor</dt><dd>45%',
        'Hardware reserve (saved baseline)</dt><dd>10%',
        'Auto-Apply is on.',
      ]) expect(html.includes(text), `missing rendered reserve detail: ${text}`);

      // A recommendation does not become active until it is applied.
      payload = data(normalized({ active_software_floor_percent: 10, auto_apply_enabled: false }));
      graph = plan._optimizerReserve(payload);
      closeTo(graph.percent, 10, 'unapplied recommendation graph floor');
      expect(graph.calculated === false, 'manual graph floor should not be calculated');
      expect(detail(payload).includes('Forecast recommendation</dt><dd>45%'),
        'unapplied recommendation should remain visible');

      // Null, malformed, non-finite, boolean, and empty normalized values stay
      // unknown instead of being converted to zero or legacy fallbacks.
      for (const invalid of [null, 'invalid', 'Infinity', true, '']) {
        payload = {
          ...data(normalized({
            manual_minimum_percent: invalid,
            recommended_percent: invalid,
            active_software_floor_percent: invalid,
            hardware_baseline_percent: invalid,
            hardware_value_quality: 'saved_baseline',
          })),
          backup_reserve: 0,
          manual_backup_reserve: 12,
          reserve_recommendation: {
            suggested_optimizer_reserve_percent: 34,
            hardware_reserve_percent: 56,
          },
        };
        plan._data = payload;
        graph = plan._optimizerReserve(payload);
        expect(Number.isNaN(graph.percent), `invalid active value became ${graph.percent}`);
        html = detail(payload);
        expect((html.match(/<dd>Unknown<\/dd>/g) || []).length === 4,
          `invalid normalized fields were not all unknown for ${String(invalid)}`);
      }

      // Real zero and one percent values remain known values.
      payload = data(normalized({
        manual_minimum_percent: 0,
        recommended_percent: 1,
        active_software_floor_percent: 0,
        hardware_baseline_percent: 1,
      }));
      plan._data = payload;
      graph = plan._optimizerReserve(payload);
      closeTo(graph.percent, 0, 'measured zero graph floor');
      html = detail(payload);
      expect(html.includes('Manual minimum</dt><dd>0%'), 'measured zero manual value lost');
      expect(html.includes('Forecast recommendation</dt><dd>1%'), 'one percent recommendation lost');
      expect(html.includes('Hardware reserve (saved baseline)</dt><dd>1%'), 'one percent baseline lost');

      // Hardware numbers are hidden unless their quality says they are a
      // saved baseline. Normalized payloads never borrow legacy values.
      payload = {
        ...data(normalized({ hardware_baseline_percent: 20, hardware_value_quality: 'unknown' })),
        hardware_backup_reserve: 30,
        reserve_recommendation: { hardware_reserve_percent: 40 },
      };
      html = detail(payload);
      expect(html.includes('Hardware reserve (saved baseline)</dt><dd>Unknown'),
        'unknown-quality hardware value was displayed');
      payload = {
        ...data(normalized({
          manual_minimum_percent: null,
          recommended_percent: null,
          active_software_floor_percent: null,
          hardware_baseline_percent: null,
          auto_apply_enabled: null,
          forecast_quality: 'unavailable',
          forecast_updated_at: null,
        })),
        backup_reserve: 22,
        manual_backup_reserve: 23,
        auto_apply_reserve_enabled: false,
        reserve_recommendation: {
          suggested_optimizer_reserve_percent: 24,
          manual_optimizer_reserve_percent: 25,
          hardware_reserve_percent: 26,
          auto_apply_enabled: true,
        },
      };
      plan._data = payload;
      graph = plan._optimizerReserve(payload);
      expect(Number.isNaN(graph.percent), 'normalized null active value used a legacy fallback');
      html = detail(payload);
      expect((html.match(/<dd>Unknown<\/dd>/g) || []).length === 4,
        'normalized nulls used legacy fallback values');
      expect(html.includes('Auto-Apply status is unknown.'), 'unknown auto-apply rendered as on or off');

      // Legacy hardware values are not proven baselines, including explicit
      // recommendation values. Normalized legitimate 0% was checked above.
      payload = {
        config: { backup_reserve: 0, hardware_backup_reserve: 0 },
        reserve_recommendation: { suggested_optimizer_reserve_percent: 0, hardware_reserve_percent: null },
        last_optimization: now,
      };
      html = detail(payload);
      expect(html.includes('Active software export floor</dt><dd>0%'),
        'legacy active zero was lost');
      expect(html.includes('Hardware reserve (saved baseline)</dt><dd>Unknown'),
        'legacy config hardware zero invented a baseline');
      payload.reserve_recommendation.hardware_reserve_percent = 0;
      expect(detail(payload).includes('Hardware reserve (saved baseline)</dt><dd>Unknown'),
        'legacy recommendation hardware zero was treated as a known baseline');
      payload.reserve_recommendation.hardware_reserve_percent = 10;
      expect(detail(payload).includes('Hardware reserve (saved baseline)</dt><dd>Unknown'),
        'legacy recommendation hardware value was treated as a known baseline');

      // Inactive legacy export-floor fields must not create a second floor.
      payload = {
        config: { backup_reserve: 0.1 },
        reserve_recommendation: {
          auto_apply_enabled: true,
          suggested_optimizer_reserve_percent: 45,
          applied_optimizer_reserve_percent: 10,
          applied_export_reserve_floor_percent: 45,
          home_load_export_floor_percent: 60,
        },
        last_optimization: now,
      };
      plan._data = payload;
      graph = plan._optimizerReserve(payload);
      closeTo(graph.percent, 10, 'legacy graph floor');
      expect(Number.isNaN(graph.exportPercent), 'legacy inactive floor painted a second graph floor');
      const chips = plan._renderChips({
        raw: payload,
        reservePercent: graph.percent,
        idleHoldActive: false,
      }, { displayCurrency: 'AUD' });
      expect(chips.includes('Software export floor:</span><span>10%'),
        'legacy chip did not show the active floor');
      expect(!chips.includes('45%') && !chips.includes('60%'),
        'legacy inactive floor leaked into reserve chips');

      // Legacy auto-apply off may use the active floor as manual only when its
      // manual value is absent. Unknown auto-apply remains unknown.
      payload = {
        config: { backup_reserve: 0.12 },
        reserve_recommendation: { auto_apply_enabled: false },
        last_optimization: now,
      };
      expect(detail(payload).includes('Manual minimum</dt><dd>12%'),
        'auto-apply off did not fall back to the known active manual floor');
      payload.reserve_recommendation.auto_apply_enabled = null;
      html = detail(payload);
      expect(html.includes('Manual minimum</dt><dd>Unknown'),
        'unknown auto-apply was treated as off for the manual fallback');
      expect(html.includes('Auto-Apply status is unknown.'), 'unknown legacy auto-apply was lost');

      // Current timestamp, stale quality, age cutoff, invalid/missing time,
      // missing recommendation, and retained data after an error all have
      // explicit user-facing freshness states.
      plan._hass = { locale: { language: 'en-US' }, config: { time_zone: 'Australia/Sydney' } };
      payload = data(normalized());
      html = detail(payload);
      expect(html.includes('Forecast recommendation updated '), 'current forecast was not marked current');
      expect(html.includes('11:00 AM'), 'forecast copy did not use the Home Assistant timezone');
      payload = data(normalized({ forecast_quality: 'stale' }));
      expect(detail(payload).includes('Forecast recommendation is stale.'),
        'stale forecast quality was not shown');
      payload = data(normalized({ forecast_updated_at: '2025-12-31T23:00:00Z' }));
      expect(detail(payload).includes('Forecast recommendation is stale.'),
        'old forecast timestamp was not marked stale');
      for (const overrides of [
        { forecast_updated_at: 'not-a-time' },
        { forecast_updated_at: null },
        { recommended_percent: null },
      ]) {
        html = detail(data(normalized(overrides)));
        expect(html.includes('Forecast recommendation unavailable.'),
          `unavailable freshness state missing for ${JSON.stringify(overrides)}`);
      }
      payload = data(normalized());
      plan._error = 'refresh failed';
      html = detail(payload);
      expect(html.includes('Forecast recommendation is stale.'),
        'retained current data after refresh error was not marked stale');
    """

    subprocess.run(
        ["node", "-e", runtime_checks, str(STRATEGY_PATH)],
        check=True,
    )
