"""Home load stays readable when a car's charging state is unknown.

PowerSync withholds the EV-adjusted Home Load (state ``unknown``,
``normalization_quality: incomplete``) when a car may still be charging but
has stopped reporting, e.g. a stalled Tesla Fleet integration. The sensor and
the optimizer's load history stay unknown; the boards show the measured house
load instead of "--", flagged as possibly including the car.
"""
import json
import subprocess
from pathlib import Path

from test_dashboard_board import _run_board

FLOW_CARD = Path(__file__).resolve().parents[1] / "custom_components/power_sync/frontend/power-sync-energy-flow.js"


def test_now_card_shows_measured_load_when_ev_adjustment_is_withheld():
    out = _run_board(r"""
    const st = (state, attributes = {}) => ({state:String(state), attributes:{unit_of_measurement:'kW', ...attributes}});
    const render = home => {
      const card = new T.PsNowCard();
      card._config = {weather:false, entities:{battery:'b',solar:'s',home:'h',grid:'g'}};
      card._hass = {config:{}, states:{b:st(1.738), s:st(0), h:home, g:st(0)}};
      card._api = () => null;
      card._render();
      const html = card.innerHTML;
      const tile = html.slice(html.indexOf('<span class="k">Home</span>'));
      return tile.slice(0, tile.indexOf('</div>'));
    };
    console.log(JSON.stringify({
      incomplete: render(st('unknown', {normalization_quality:'incomplete', raw_home_load_kw:1.731})),
      complete: render(st(1.2, {normalization_quality:'complete', raw_home_load_kw:1.2})),
      unknownNoRaw: render(st('unknown', {normalization_quality:'incomplete'})),
      unavailable: render(st('unavailable', {})),
      helper: [
        context.psHomeLoad({states:{h:st('unknown', {normalization_quality:'incomplete', raw_home_load_kw:1.731})}}, 'h'),
        context.psHomeLoad({states:{h:st('unknown', {normalization_quality:'complete', raw_home_load_kw:1.731})}}, 'h'),
        context.psHomeLoad({states:{h:st('unknown', {normalization_quality:'incomplete', raw_home_load_kw:-1})}}, 'h'),
      ],
    }));
    """)
    assert "1.7" in out["incomplete"] and "May include car charging" in out["incomplete"]
    assert "1.2" in out["complete"] and "Using" in out["complete"]
    assert "May include" not in out["complete"]
    for key in ("unknownNoRaw", "unavailable"):
        assert "—" in out[key], key
        assert "May include" not in out[key], key
    assert out["helper"][0] == {"kw": 1.731, "mayIncludeEv": True}
    # Only the withheld-for-EV case falls back; never a negative or other unknown.
    assert out["helper"][1]["mayIncludeEv"] is False
    assert out["helper"][2]["mayIncludeEv"] is False


def test_energy_flow_card_uses_measured_load_when_ev_adjustment_is_withheld():
    source = FLOW_CARD.read_text()
    start = source.index("  function toOptionalWatt(")
    end = source.index("  function displayedHomeLoadPower(")
    script = source[start:end] + r"""
    const st = (state, attributes = {}) => ({state:String(state), attributes:{unit_of_measurement:'kW', ...attributes}});
    console.log(JSON.stringify([
      homeLoadWatt(st('unknown', {normalization_quality:'incomplete', raw_home_load_kw:1.731})),
      homeLoadWatt(st(1.2, {normalization_quality:'complete'})),
      homeLoadWatt(st('unknown', {normalization_quality:'complete', raw_home_load_kw:1.731})),
      homeLoadWatt(st('unavailable')),
      homeLoadWatt(null),
    ]));
    """
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, timeout=20)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == [1731, 1200, None, None, None]
