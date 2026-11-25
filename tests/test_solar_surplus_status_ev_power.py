"""Status/recommendation views must add the EV's own draw back into surplus."""

import ast
from pathlib import Path

INIT = Path(__file__).parents[1] / "custom_components/power_sync/__init__.py"


def test_no_view_computes_surplus_with_a_hardcoded_zero_ev_draw():
    calls = [
        node for node in ast.walk(ast.parse(INIT.read_text()))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_calculate_solar_surplus"
    ]
    assert calls
    for call in calls:
        ev_arg = call.args[1] if len(call.args) > 1 else None
        assert not (isinstance(ev_arg, ast.Constant) and ev_arg.value == 0), ast.unparse(call)


def test_views_keep_the_coordinator_home_load_basis():
    """Adding the car's draw back is only right when load includes it.

    Tesla/Sigenergy coordinators publish ``home_load_basis="excludes_ev"``;
    dropping it made the direct method subtract the car twice (8 kW solar,
    1 kW house, 7 kW car read 13.5 kW available instead of 6.5 kW). Both
    views read the site through the shared builder, which keeps the basis.
    """
    import importlib.util

    tree = ast.parse(INIT.read_text())
    for name in ("SolarSurplusStatusView", "PriceRecommendationView"):
        view = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
        calls = [
            node for node in ast.walk(view)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_surplus_view_site"
        ]
        assert calls, name

    path = INIT.parent / "automations" / "live_status.py"
    spec = importlib.util.spec_from_file_location("live_status_basis_test", path)
    live_status_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live_status_module)
    for basis in ("excludes_ev", "includes_ev"):
        status = live_status_module.coordinator_data_to_ev_live_status({
            "solar_power": 8.0, "load_power": 1.0, "grid_power": 0.0,
            "battery_power": 0.0, "ev_power": 7.0, "home_load_basis": basis,
        })
        assert status["home_load_basis"] == basis
