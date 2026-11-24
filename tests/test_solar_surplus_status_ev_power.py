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
