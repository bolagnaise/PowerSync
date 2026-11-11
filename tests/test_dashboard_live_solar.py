"""Live solar wording follows the same measured-power contract as mobile."""
from test_dashboard_board import _run_board


def test_solar_home_headline_in_optimizer_override_and_monitoring():
    out = _run_board(r"""
    const st = (state) => ({state:String(state), attributes:{unit_of_measurement:'kW'}});
    const result = [];
    for (const mode of ['normal', 'self_consumption']) {
      for (const monitoring of [false,true]) {
        for (const [battery, grid] of [[-1.8,0],[0,0],[-1.8,0.5],[-1.8,'unavailable']]) {
          const card = new T.PsNowCard();
          card._config = {weather:false, entities:{battery:'b',solar:'s',home:'h',grid:'g',mode:'m'}};
          card._hass = {config:{},states:{b:st(battery),s:st(5.7),h:st(2),g:st(grid),m:st(mode)}};
          card._api = () => ({enabled:true, monitoring_mode:monitoring, current_action:'self_consumption'});
          card._render();
          result.push({battery,grid,headline:card.innerHTML.split('<h1>')[1].split('</h1>')[0]});
        }
      }
    }
    console.log(JSON.stringify(result));
    """)
    for sample in out:
        expected = ("The battery is charging." if sample["grid"] != 0 else
                    "Solar is powering the home." if sample["battery"] == 0 else
                    "Solar is powering the home and charging the battery.")
        assert sample["headline"] == expected
