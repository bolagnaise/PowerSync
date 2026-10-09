"""Runtime regression tests for the current export reserve decision callout."""

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


def test_current_export_reserve_decision_renders_only_current_evidence():
    """The callout explains current reserve outcomes without inferring causes."""
    runtime_checks = r"""
      const fs = require('fs');
      const vm = require('vm');
      const source = fs.readFileSync(process.argv[1], 'utf8');
      const FIXED_NOW = Date.parse('2026-01-01T00:00:00Z');
      class FixedDate extends Date {
        static now() { return FIXED_NOW; }
      }
      class StubElement { attachShadow() { return {}; } }
      const registry = new Map();
      const context = {
        window: {}, HTMLElement: StubElement,
        customElements: {
          get: name => registry.get(name),
          define: (name, value) => registry.set(name, value),
        },
        Date: FixedDate, Map, Set, console,
      };
      vm.createContext(context);
      vm.runInContext(`${source}\nglobalThis.ReservePlan = PowerSyncOptimizationPlan;`, context);
      const plan = new context.ReservePlan();
      plan._hass = { config: { time_zone: 'Australia/Sydney' } };
      const expect = (condition, message) => {
        if (!condition) throw new Error(message);
      };
      const baseDecision = {
        status: 'blocked',
        reason: 'projected_below_floor',
        control_outcome: 'self_consumption_accepted',
        evaluated_at: '2026-01-01T00:00:00Z',
        slot_start: '2025-12-31T23:30:00Z',
        slot_end: '2026-01-01T01:00:00Z',
        current_soc_percent: 47,
        projected_soc_percent: 44,
        projection_basis: 'planned_slot',
        software_floor_percent: 45,
        requested_minutes: 30,
        allowed_minutes: 0,
        plan_generated_at: '2026-01-01T00:00:00Z',
        plan_snapshot_id: 'snapshot-1',
      };
      const render = (decision = baseDecision, overrides = {}) => {
        plan._error = null;
        plan._data = {
          enabled: true,
          monitoring_mode: false,
          config: { interval_minutes: 5 },
          last_optimization: '2026-01-01T00:00:00Z',
          export_reserve_decision: decision,
          ...overrides,
        };
        return plan._renderExportReserveDecision();
      };

      // A current plan-backed projection explains the 47% -> 44% comparison
      // with the 45% floor and identifies the projection as a forecast.
      let html = render();
      expect(html && /47/.test(html) && /44/.test(html) && /45/.test(html),
        'projected-below-floor callout omitted a measured percentage');
      expect(/forecast|projected|planned/i.test(html),
        'planned-slot projection was not identified as forecast evidence');
      expect(/self-consumption|self consumption/i.test(html),
        'accepted self-consumption outcome was not explained');

      // A missing decision record is not permission to infer a held export
      // from a planned export slot or the current self-consumption setting.
      expect(render(null) === '', 'missing decision record produced a causal claim');

      // At the floor is distinguishable from a projection below it.
      html = render({ ...baseDecision, reason: 'at_floor', current_soc_percent: 45,
        projected_soc_percent: null, projection_basis: null });
      expect(/at|floor/i.test(html) && /45/.test(html), 'at-floor reason was not surfaced');

      // A duration-based safety rejection must say that the full-power estimate
      // is a safety assumption, rather than present it as a planned-slot forecast.
      html = render({ ...baseDecision, reason: 'insufficient_safe_duration',
        current_soc_percent: 52, projected_soc_percent: null,
        projection_basis: 'full_power_duration', requested_minutes: 30,
        allowed_minutes: 0 });
      expect(/complete interval/i.test(html) && /full.power estimate/i.test(html),
        'duration rejection omitted its full-power estimate basis');

      // Limited permission must show both requested and allowed time and make
      // the reduction explicit.
      html = render({ ...baseDecision, status: 'limited', reason: 'insufficient_safe_duration',
        requested_minutes: 30, allowed_minutes: 25 });
      expect(/30/.test(html) && /25/.test(html), 'limited decision omitted requested/allowed minutes');
      expect(/limit|short|reduc|allow/i.test(html), 'limited decision was not distinguished from blocked');

      // Unknown verification states stay uncertain, and unconfirmed restore or
      // commands never assert that physical export stopped.
      html = render({ ...baseDecision, status: 'unknown', reason: 'safe_duration_unverified',
        control_outcome: 'command_unconfirmed', projected_soc_percent: null,
        projection_basis: null });
      expect(/unknown|unverified|confirm/i.test(html), 'unknown outcome was presented as verified');
      expect(!/reserve (was|is) binding/i.test(html), 'unknown verification claimed reserve caused the result');
      for (const control_outcome of ['restore_unconfirmed', 'command_unconfirmed']) {
        html = render({ ...baseDecision, control_outcome, projected_soc_percent: null,
          projection_basis: null });
        expect(!/export (was|has been|is) stopped/i.test(html),
          `${control_outcome} claimed physical export stopped`);
      }

      // Missing and non-finite values remain unknown; they must not become a
      // fabricated zero-percent battery reading or limit.
      for (const invalid of [null, 'bad', 'Infinity', true, '']) {
        html = render({ ...baseDecision, current_soc_percent: invalid,
          projected_soc_percent: invalid, software_floor_percent: invalid,
          requested_minutes: invalid, allowed_minutes: invalid });
        expect(!/0\s*%/.test(html), `invalid values fabricated 0% for ${String(invalid)}`);
        expect((html.match(/Unknown/g) || []).length >= 3,
          `invalid percentages were not shown as unknown for ${String(invalid)}`);
      }

      // Exact slot end is expired. Retained data after a refresh error, disabled
      // optimization, and monitoring mode are also ineligible for this callout.
      html = render({ ...baseDecision, slot_end: '2026-01-01T00:00:00Z' });
      expect(html === '', 'decision remained visible at the exact slot end');
      html = render({ ...baseDecision, evaluated_at: '2025-12-31T23:44:00Z' });
      expect(html === '', 'stale same-client decision remained visible');
      html = render({ ...baseDecision, plan_generated_at: '2025-12-31T23:59:00Z' });
      expect(html === '', 'decision from a superseded plan remained visible');
      html = render({ ...baseDecision, evaluated_at: 'not-a-time' });
      expect(html === '', 'decision with an invalid evaluation time remained visible');
      expect(render(baseDecision, { enabled: false }) === '', 'disabled optimization showed a decision');
      expect(render(baseDecision, { monitoring_mode: true }) === '', 'monitoring mode showed a decision');
      plan._data = {
        enabled: true, monitoring_mode: false, config: { interval_minutes: 5 },
        last_optimization: '2026-01-01T00:00:00Z', export_reserve_decision: baseDecision,
      };
      plan._error = 'refresh failed';
      expect(plan._renderExportReserveDecision() === '', 'refresh error showed a retained decision');

      // Unknown external fields cannot inject markup into the rendered HTML.
      html = render({ ...baseDecision, plan_snapshot_id: '<img src=x onerror=alert(1)>',
        current_soc_percent: '<script>alert(1)</script>' });
      expect(!/<img\b|<script\b/i.test(html), 'decision rendered unescaped payload markup');

      // Current and future windows are both listed. Attach the explanation to
      // the active export segment, including a segment of a merged window.
      render();
      const currentWindow = {
        action: 'export', timestamp: baseDecision.slot_start,
        end_time: baseDecision.slot_end, durationMinutes: 90,
        spanDurationMinutes: 90, power_w: 2100,
      };
      const futureWindow = { ...currentWindow,
        timestamp: '2026-01-01T02:00:00Z', end_time: '2026-01-01T02:30:00Z' };
      const priceMeta = { minorUnit: 'c', displayCurrency: 'AUD' };
      expect(plan._exportReserveDecisionForWindow(currentWindow) === baseDecision,
        'current export window did not receive its decision');
      expect(plan._exportReserveDecisionForWindow({ ...currentWindow, action: 'discharge' }) === baseDecision,
        'current discharge window did not receive its decision');
      expect(plan._exportReserveDecisionForWindow(futureWindow) === null,
        'future window was labelled with a current decision');
      expect(plan._exportReserveDecisionForWindow({ ...currentWindow, action: 'charge' }) === null,
        'charge window was labelled with an export decision');
      expect(plan._exportReserveDecisionForWindow({ ...currentWindow,
        timestamp: '2025-12-31T23:00:00Z', end_time: '2026-01-01T00:00:00Z' }) === null,
        'a window ending now retained the decision');
      const merged = { ...currentWindow, end_time: futureWindow.end_time,
        segments: [currentWindow, futureWindow] };
      expect(plan._exportReserveDecisionForWindow(merged) === baseDecision,
        'active segment of a merged export window lost its decision');
      const gap = { ...merged, segments: [
        { timestamp: '2025-12-31T23:30:00Z', end_time: '2025-12-31T23:55:00Z' },
        { timestamp: '2026-01-01T00:05:00Z', end_time: '2026-01-01T01:00:00Z' },
      ] };
      expect(plan._exportReserveDecisionForWindow(gap) === null,
        'gap inside a merged window was treated as an active export segment');
      html = plan._renderBatteryWindows([currentWindow, futureWindow], priceMeta);
      const noticeAt = html.indexOf('role="status"');
      expect(noticeAt > html.indexOf('class="window-row export"') &&
        noticeAt < html.lastIndexOf('class="window-row export"'),
        'current explanation was not inside the current window');
      expect((html.match(/role="status"/g) || []).length === 1,
        'current explanation was duplicated across windows');
      expect(/current slot only/.test(html) && /23:30 - 01:00/.test(html),
        'window explanation omitted its decision slot scope');

      // If its row is missing, preserve the explanation at the windows section
      // without labelling an unrelated future row. Expired evidence stays hidden.
      html = plan._renderBatteryWindows([futureWindow], priceMeta);
      expect(html.indexOf('role="status"') < html.indexOf('class="window-row export"'),
        'missing current row attributed its decision to the future window');
      html = plan._renderBatteryWindows([], priceMeta);
      expect(/role="status"/.test(html), 'missing windows hid valid current evidence');
      render({ ...baseDecision, slot_end: '2026-01-01T00:00:00Z' });
      expect(plan._exportReserveDecisionForWindow(currentWindow) === null,
        'expired evidence remained attached to a window');
      html = plan._renderBatteryWindows([currentWindow, futureWindow], priceMeta);
      expect(!/role="status"/.test(html), 'expired evidence remained in the window list');
    """

    subprocess.run(
        ["node", "-e", runtime_checks, str(STRATEGY_PATH)],
        check=True,
    )
