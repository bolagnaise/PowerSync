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
    1 kW house, 7 kW car read 13.5 kW available instead of 6.5 kW).
    """
    tree = ast.parse(INIT.read_text())
    for name in ("SolarSurplusStatusView", "PriceRecommendationView"):
        view = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
        dicts = [
            node.value for node in ast.walk(view)
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "live_status" for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ]
        assert dicts, name
        for value in dicts:
            keys = {k.value for k in value.keys if isinstance(k, ast.Constant)}
            assert "home_load_basis" in keys, name
        reads = [
            node for node in ast.walk(view)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "get"
            and node.args and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "home_load_basis"
        ]
        assert len(reads) == 4, name  # tesla, sigenergy, sungrow, foxess
