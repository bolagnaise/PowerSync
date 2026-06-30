"""Regression tests for AC inverter model selector options."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONST_PATH = ROOT / "custom_components" / "power_sync" / "const.py"


def _load_ac_inverter_namespace():
    source = CONST_PATH.read_text()
    start = source.index("# AC-Coupled Inverter Curtailment configuration")
    end = source.index("# Smart Optimization Configuration")
    namespace = {"BATTERY_SYSTEM_SUNGROW": "sungrow"}
    exec(compile(source[start:end], str(CONST_PATH), "exec"), namespace)
    return namespace


def test_solax_model_options_do_not_fall_back_to_sungrow():
    const = _load_ac_inverter_namespace()

    models = const["get_models_for_brand"]("solax")

    assert "x1-hybrid" in models
    assert "x3-hybrid" in models
    assert not set(models).intersection(const["SUNGROW_MODELS"])


def test_every_visible_ac_inverter_brand_has_own_model_options():
    const = _load_ac_inverter_namespace()

    for brand in const["INVERTER_BRANDS"]:
        models = const["get_models_for_brand"](brand)
        assert models, brand
        if brand != "sungrow":
            assert models != const["SUNGROW_MODELS"], brand


def test_sungrow_battery_system_can_select_hybrid_model_for_curtailment():
    const = _load_ac_inverter_namespace()

    models = const["get_models_for_brand"]("sungrow", const["BATTERY_SYSTEM_SUNGROW"])

    assert "sh20t" in models
    assert "sh10rs" in models
    assert "sg2.5rs" in models


def test_brand_defaults_match_controller_defaults_for_hybrid_brands():
    const = _load_ac_inverter_namespace()

    assert const["get_brand_defaults"]("sigenergy") == {"port": 502, "slave_id": 247}
    assert const["get_brand_defaults"]("solax") == {"port": 502, "slave_id": 1}
    assert const["get_brand_defaults"]("alphaess") == {"port": 502, "slave_id": 85}
    assert const["get_brand_defaults"]("solaredge") == {"port": 502, "slave_id": 1}


def test_solaredge_ac_inverter_options_are_available():
    const = _load_ac_inverter_namespace()

    assert const["INVERTER_BRANDS"]["solaredge"] == "SolarEdge"
    models = const["get_models_for_brand"]("solaredge")

    assert "energy-hub" in models
    assert "three-phase" in models
    assert models != const["SUNGROW_MODELS"]
