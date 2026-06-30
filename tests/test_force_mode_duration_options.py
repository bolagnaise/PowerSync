"""Regression tests for manual force-mode duration options."""

from pathlib import Path
import ast


ROOT = Path(__file__).resolve().parents[1]


def _const_duration_values() -> list[int]:
    tree = ast.parse((ROOT / "custom_components/power_sync/const.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DISCHARGE_DURATIONS":
                    return ast.literal_eval(node.value)
    raise AssertionError("DISCHARGE_DURATIONS not found")


def test_force_mode_durations_accept_mobile_quarter_hour_values():
    durations = _const_duration_values()
    assert durations == [5, 10, *range(15, 241, 15)]
    assert 195 in durations
