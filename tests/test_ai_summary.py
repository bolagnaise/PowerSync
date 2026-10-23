"""Regression tests for descriptive optimizer AI summaries."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = (
    ROOT / "custom_components" / "power_sync" / "optimization" / "ai_summary.py"
)
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
COORDINATOR_PATH = (
    ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
)


def _load_module():
    names = (
        "custom_components",
        "custom_components.power_sync",
        "custom_components.power_sync.optimization",
    )
    previous = {name: sys.modules.get(name) for name in names}
    previous_const = sys.modules.get("custom_components.power_sync.const")
    try:
        for name in names:
            package = ModuleType(name)
            package.__path__ = []
            sys.modules[name] = package
        const = ModuleType("custom_components.power_sync.const")
        const.CONF_OPTIMIZATION_AI_SUMMARY_API_KEY = "optimization_ai_summary_api_key"
        const.CONF_OPTIMIZATION_AI_SUMMARY_AUTO_REFRESH = "optimization_ai_summary_auto_refresh"
        const.CONF_OPTIMIZATION_AI_SUMMARY_LOCAL_ENDPOINT = "optimization_ai_summary_local_endpoint"
        const.CONF_OPTIMIZATION_AI_SUMMARY_LOCAL_TIMEOUT = "optimization_ai_summary_local_timeout"
        const.CONF_OPTIMIZATION_AI_SUMMARY_LOCAL_MODEL = "optimization_ai_summary_local_model"
        const.CONF_OPTIMIZATION_AI_SUMMARY_OPENAI_MODEL = "optimization_ai_summary_openai_model"
        const.CONF_OPTIMIZATION_AI_SUMMARY_OPENROUTER_MODEL = "optimization_ai_summary_openrouter_model"
        const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER = "optimization_ai_summary_provider"
        const.DEFAULT_OPTIMIZATION_AI_SUMMARY_PROVIDER = "gemini"
        sys.modules[const.__name__] = const

        spec = importlib.util.spec_from_file_location(
            "custom_components.power_sync.optimization.ai_summary",
            MODULE_PATH,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if previous_const is None:
            sys.modules.pop("custom_components.power_sync.const", None)
        else:
            sys.modules["custom_components.power_sync.const"] = previous_const


def _snapshot() -> dict:
    return {
        "optimizer_available": True,
        "optimization_status": "active",
        "last_optimization": "2026-08-01T12:00:00+10:00",
        "monitoring_mode": False,
        "currency": "AUD",
        "price_unit": "AUD/kWh",
        "minor_price_unit": "c/kWh",
        "battery_soc_percent": 25.0,
        "planned_current_action": "charge",
        "planned_current_power_w": 5000,
        "current_action": "charge",
        "current_action_end_time": "2026-08-01T12:30:00+10:00",
        "next_action": "export",
        "next_action_time": "2026-08-01T13:00:00+10:00",
        "next_action_power_w": 4500,
        "predicted_cost": 1.2,
        "predicted_savings": 2.3,
        "schedule": {
            "timestamps": [
                "2026-08-01T12:00:00+10:00",
                "2026-08-01T12:30:00+10:00",
                "2026-08-01T13:00:00+10:00",
                "2026-08-01T13:30:00+10:00",
            ],
            "import_price": [0.10, 0.12, 0.40, 0.45],
            "export_price": [0.03, 0.04, 0.65, 0.70],
            "soc": [0.25, 0.35, 0.45, 0.38],
            "serial_number": "must-not-leak",
        },
        "next_actions": [
            {
                "timestamp": "2026-08-01T12:00:00+10:00",
                "end_time": "2026-08-01T13:00:00+10:00",
                "action": "charge",
                "power_w": 5000,
                "soc": 0.25,
            },
            {
                "timestamp": "2026-08-01T13:00:00+10:00",
                "end_time": "2026-08-01T14:00:00+10:00",
                "action": "export",
                "power_w": 4500,
                "soc": 0.45,
            },
        ],
        "summary": {
            "total_cost": 1.2,
            "total_import_kwh": 5.0,
            "total_export_kwh": 3.5,
            "savings": 2.3,
        },
        "forecast_summary": {
            "load_today_remaining_kwh": 8.4,
            "load_tomorrow_kwh": 18.1,
            "load_peak_kw": 4.2,
            "solar_next_24h_kwh": 21.6,
            "solar_peak_kw": 6.4,
            "history_diagnostics": {"entity_id": "sensor.private"},
        },
        "warnings": [
            {
                "type": "forecast",
                "title": "Forecast limited",
                "message": "Solar forecast is incomplete.",
                "api_key": "must-not-leak",
            }
        ],
        "config": {
            "backup_reserve": 0.2,
            "allow_grid_charge": True,
            "max_grid_export_w": 5000,
            "interval_minutes": 30,
            "horizon_hours": 48,
            "planned_ev_load_entity": "sensor.private_ev",
        },
        "ev": {
            "deadline": "2026-08-02T06:00:00+10:00",
            "required_energy_kwh": 14,
            "vin": "must-not-leak",
            "vehicles": [{"name": "private"}],
        },
        "planned_ev_load_kwh": 10.5,
        "planned_ev_load_peak_kw": 7.2,
        "api_key": "must-not-leak",
        "entity_id": "sensor.private",
    }


def _model_output() -> dict:
    return {
        "headline": "Charge now, then export in the higher-value period.",
        "now": "The battery is charging now at up to 5 kW.",
        "next": "At 1:00 pm, the plan switches to exporting.",
        "why_it_matters": "The supplied export price is higher at 1:00 pm than it is now.",
        "expected_outcome": "PowerSync expects today's plan to save AUD 2.30 against its baseline.",
        "action_explanations": [
            {"window_id": "w1", "reason": "This window has the highest supplied export price."}
        ],
        "caveats": ["Forecast inputs may change."],
    }


def test_context_is_compact_deterministic_and_excludes_private_fields():
    module = _load_module()
    context = module.build_compact_context(_snapshot())
    serialized = module.canonical_context_json(context)

    assert [window["window_id"] for window in context["action_windows"]] == ["w0", "w1"]
    assert context["action_windows"][0]["import_price"] == {
        "min": 0.1,
        "average": 0.11,
        "max": 0.12,
    }
    assert context["action_windows"][1]["export_price"]["average"] == 0.675
    assert context["action_windows"][0]["peak_power_kw"] == 5.0
    assert context["action_windows"][0]["end_soc_percent"] == 35.0
    assert context["horizon"] == {
        "start": "2026-08-01T12:00:00+10:00",
        "end": "2026-08-01T14:00:00+10:00",
        "hours": 24,
    }
    assert context["summary"] == {
        "predicted_cost_today": 1.2,
        "predicted_savings_today": 2.3,
        "baseline_cost_today": 3.5,
    }
    assert context["instruction_contract_version"] == "powersync.optimizer-explainer.v3"
    assert context["units"] == {
        "power": "kW",
        "energy": "kWh",
        "soc": "percent",
        "price_values": "major_currency_per_kwh",
        "currency": "AUD",
        "price_unit": "AUD/kWh",
        "minor_price_unit": "c/kWh",
    }
    assert context["current_status"]["battery_soc_percent"] == 25.0
    assert context["ev"]["plan_available"] is True
    for secret in (
        "must-not-leak",
        "sensor.private",
        "sensor.private_ev",
        "serial_number",
        '"vin"',
        '"api_key"',
        '"vehicles"',
        "history_diagnostics",
    ):
        assert secret not in serialized

    reordered = dict(reversed(list(context.items())))
    assert module.context_fingerprint(context, "gemini", "model") == module.context_fingerprint(
        reordered, "gemini", "model"
    )
    live_status_changed = {
        **context,
        "current_status": {**context["current_status"], "planned_action": "export"},
        "plan_generated_at": "later",
    }
    assert module.context_fingerprint(context, "gemini", "model") != module.context_fingerprint(
        live_status_changed, "gemini", "model"
    )
    soc_only_changed = {
        **context,
        "current_status": {
            **context["current_status"],
            "battery_soc_percent": 24.8,
        },
    }
    assert module.context_fingerprint(context, "gemini", "model") == module.context_fingerprint(
        soc_only_changed, "gemini", "model"
    )
    soc_bucket_changed = {
        **context,
        "current_status": {
            **context["current_status"],
            "battery_soc_percent": 23.9,
        },
    }
    assert module.context_fingerprint(context, "gemini", "model") != module.context_fingerprint(
        soc_bucket_changed, "gemini", "model"
    )
    assert module.plan_guard_fingerprint(
        context, "gemini", "model"
    ) == module.plan_guard_fingerprint(soc_bucket_changed, "gemini", "model")
    monitoring_changed = {**context, "monitoring_mode": True}
    assert module.context_fingerprint(context, "gemini", "model") != module.context_fingerprint(
        monitoring_changed, "gemini", "model"
    )
    assert module.plan_guard_fingerprint(
        context, "gemini", "model"
    ) != module.plan_guard_fingerprint(monitoring_changed, "gemini", "model")


def test_price_summary_excludes_outliers_beyond_compact_horizon():
    module = _load_module()
    snapshot = _snapshot()
    snapshot["schedule"]["timestamps"].append("2026-08-02T13:00:00+10:00")
    snapshot["schedule"]["import_price"].append(99.0)
    snapshot["schedule"]["export_price"].append(99.0)
    snapshot["schedule"]["soc"].append(0.5)

    context = module.build_compact_context(snapshot)

    assert context["price_summary"]["import"] == {
        "min": 0.1,
        "average": 0.268,
        "max": 0.45,
    }
    assert context["price_summary"]["export"] == {
        "min": 0.03,
        "average": 0.355,
        "max": 0.7,
    }


def test_context_distinguishes_soft_missing_inputs_from_unusable_plan():
    module = _load_module()
    snapshot = _snapshot()
    snapshot["schedule"].pop("import_price")
    snapshot["schedule"].pop("export_price")
    snapshot.pop("forecast_summary")
    snapshot.pop("predicted_cost")
    snapshot.pop("predicted_savings")
    snapshot.pop("ev")
    snapshot.pop("planned_ev_load_kwh")
    snapshot.pop("planned_ev_load_peak_kw")

    context = module.build_compact_context(snapshot)
    assert context["missing_inputs"] == [
        "cost_and_energy_summary",
        "export_prices",
        "forecast_evidence",
        "forecast_summary",
        "import_prices",
    ]
    assert context["ev"] == {
        "configured": False,
        "plan_available": False,
    }

    snapshot["next_actions"] = []
    with pytest.raises(module.AISummaryError, match="usable action plan") as caught:
        module.build_compact_context(snapshot)
    assert caught.value.code == "optimizer_unavailable"


def test_context_treats_configured_ev_without_active_plan_as_valid_empty_state():
    module = _load_module()
    snapshot = _snapshot()
    snapshot.pop("ev")
    snapshot.pop("planned_ev_load_kwh")
    snapshot.pop("planned_ev_load_peak_kw")
    snapshot["features"] = {
        "ev_integration": True,
        "planned_ev_load": False,
    }

    context = module.build_compact_context(snapshot)

    assert context["ev"] == {
        "configured": True,
        "plan_available": False,
    }
    assert "ev_plan" not in context["missing_inputs"]


def test_v3_prompt_is_provider_neutral_decision_first_and_descriptive_only():
    module = _load_module()

    assert module.PROMPT_VERSION == "3"
    assert module.SCHEMA_VERSION == "3"
    assert "PowerSync Optimizer Explainer contract" in module.SYSTEM_PROMPT
    assert "What is happening now" in module.SYSTEM_PROMPT
    assert "What happens next and when" in module.SYSTEM_PROMPT
    assert "cannot control, execute, modify, or" in module.SYSTEM_PROMPT
    assert "Never calculate or invent prices" in module.SYSTEM_PROMPT
    assert "supplied currency and" in module.SYSTEM_PROMPT
    assert "Gemini" not in module.SYSTEM_PROMPT
    assert "Grok" not in module.SYSTEM_PROMPT


def test_forecast_evidence_requires_exact_schedule_snapshot_and_complete_slots():
    module = _load_module()
    snapshot = _snapshot()
    snapshot["schedule"]["plan_snapshot_id"] = "plan-a"
    snapshot["forecast_evidence"] = {
        "plan_snapshot_id": "plan-a",
        "timestamps": list(snapshot["schedule"]["timestamps"]),
        "solar_forecast_values_kw": [1.0, 1.5, 2.0, 1.0],
        "load_forecast_values_kw": [0.8, 1.1, 1.4, 0.7],
    }

    context = module.build_compact_context(snapshot)
    evidence = context["forecast_evidence"]
    assert evidence["status"] == "available"
    assert evidence["windows"][0]["values"] == [
        {"timestamp": "2026-08-01T12:00:00+10:00", "solar_kw": 1.0, "load_kw": 0.8},
        {"timestamp": "2026-08-01T12:30:00+10:00", "solar_kw": 1.5, "load_kw": 1.1},
    ]

    snapshot["forecast_evidence"]["plan_snapshot_id"] = "other-plan"
    assert module.build_compact_context(snapshot)["forecast_evidence"] == {
        "status": "unavailable", "reason": "plan_mismatch", "windows": [],
    }
    snapshot["forecast_evidence"]["plan_snapshot_id"] = "plan-a"
    snapshot["forecast_evidence"]["load_forecast_values_kw"].pop()
    assert module.build_compact_context(snapshot)["forecast_evidence"] == {
        "status": "unavailable", "reason": "incomplete_or_misaligned", "windows": [],
    }


def test_forecast_evidence_aggregates_long_windows_without_losing_bounds():
    module = _load_module()
    snapshot = _snapshot()
    timestamps = [f"2026-08-01T{hour:02d}:00:00+10:00" for hour in range(26)]
    snapshot["schedule"]["timestamps"] = timestamps
    snapshot["schedule"]["import_price"] = [0.1] * len(timestamps)
    snapshot["schedule"]["export_price"] = [0.05] * len(timestamps)
    snapshot["schedule"]["soc"] = [0.25] * len(timestamps)
    snapshot["schedule"]["plan_snapshot_id"] = "long-plan"
    snapshot["next_actions"] = [{
        "timestamp": timestamps[0], "end_time": timestamps[-1], "action": "charge", "power_w": 5000,
    }]
    snapshot["forecast_evidence"] = {
        "plan_snapshot_id": "long-plan", "timestamps": timestamps,
        "solar_forecast_values_kw": list(range(26)),
        "load_forecast_values_kw": [1.0] * 26,
    }

    evidence = module.build_compact_context(snapshot)["forecast_evidence"]["windows"][0]
    assert "values" not in evidence
    assert evidence["summary"] == {
        "slot_count": 25,
        "solar_kw": {"min": 0.0, "average": 12.0, "max": 24.0},
        "load_kw": {"min": 1.0, "average": 1.0, "max": 1.0},
    }

def test_forecast_evidence_marks_changed_values_without_model_provenance():
    module = _load_module()
    current = {
        "status": "available",
        "plan_snapshot_id": "current",
        "windows": [{"window_id": "w0", "values": [{"solar_kw": 2.0, "load_kw": 1.0}]}],
    }
    previous = {
        "status": "available",
        "plan_snapshot_id": "previous",
        "windows": [{"window_id": "w0", "values": [{"solar_kw": 1.0, "load_kw": 1.0}]}],
    }
    result = module.forecast_evidence_for_display(current, previous)
    assert result["status"] == "changed"
    assert result["changed_since_last_explained"] is True


def test_verified_feedback_reports_observed_changes_without_invented_cause():
    module = _load_module()
    previous = module.build_compact_context(_snapshot())
    current_snapshot = _snapshot()
    current_snapshot["schedule"]["import_price"][0] = 0.25
    current_snapshot["forecast_summary"]["solar_next_24h_kwh"] = 12.1
    current_snapshot["battery_soc_percent"] = 22.0
    current_snapshot["next_actions"][0]["action"] = "idle"
    current = module.build_compact_context(current_snapshot)

    unavailable = module.build_verified_feedback(None, current)
    assert unavailable == {
        "available": False,
        "comparison": "no_previous_explained_plan",
        "plan_changed": None,
        "verified_input_changes": [],
    }

    feedback = module.build_verified_feedback(previous, current)
    assert feedback["available"] is True
    assert feedback["plan_changed"] is True
    assert {item["kind"] for item in feedback["verified_input_changes"]} >= {
        "tariff",
        "forecast",
        "battery_soc",
    }
    assert "cause" not in feedback


def test_model_output_is_strict_and_server_owns_action_metadata():
    module = _load_module()
    context = module.build_compact_context(_snapshot())
    output = module.validate_model_output(_model_output(), context["action_windows"])

    assert output["contract_version"] == "powersync.optimizer-explainer.v3"
    assert output["overview"] == " ".join(
        _model_output()[key] for key in ("headline", "now", "next")
    )
    assert output["strategy"] == " ".join(
        _model_output()[key] for key in ("why_it_matters", "expected_outcome")
    )
    assert output["important_actions"] == [
        {
            "window_id": "w1",
            "start": "2026-08-01T13:00:00+10:00",
            "end": "2026-08-01T14:00:00+10:00",
            "action": "export",
            "reason": "This window has the highest supplied export price.",
        }
    ]

    unknown = _model_output()
    unknown["action_explanations"] = [{"window_id": "w99", "reason": "invented"}]
    with pytest.raises(module.AISummaryError) as caught:
        module.validate_model_output(unknown, context["action_windows"])
    assert caught.value.code == "invalid_provider_response"

    extra = {**_model_output(), "hardware_command": "force_charge"}
    with pytest.raises(module.AISummaryError):
        module.validate_model_output(extra, context["action_windows"])

    excessive = _model_output()
    excessive["headline"] = "x" * 181
    with pytest.raises(module.AISummaryError):
        module.validate_model_output(excessive, context["action_windows"])


def test_write_only_settings_replace_preserve_clear_and_isolate_provider_key():
    module = _load_module()
    key_name = module.CONF_OPTIMIZATION_AI_SUMMARY_API_KEY
    provider_name = module.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER

    options, changes = module.apply_ai_summary_settings(
        {}, {"ai_summary_provider": "gemini", "ai_summary_api_key": "secret-key"}
    )
    assert options == {provider_name: "gemini", key_name: "secret-key"}
    assert changes == ["updated AI summary API key"]

    preserved, changes = module.apply_ai_summary_settings(
        options, {"ai_summary_api_key": "   "}
    )
    assert preserved[key_name] == "secret-key"
    assert changes == []

    switched, changes = module.apply_ai_summary_settings(
        options, {"ai_summary_provider": "grok"}
    )
    assert switched == {provider_name: "grok", key_name: ""}
    assert changes == [
        "updated AI summary provider",
        "cleared AI summary API key",
    ]
    assert options == {provider_name: "gemini", key_name: "secret-key"}

    changed, _ = module.apply_ai_summary_settings(
        options,
        {"ai_summary_provider": "grok", "ai_summary_api_key": "grok-secret"},
    )
    assert changed == {provider_name: "grok", key_name: "grok-secret"}

    cleared, changes = module.apply_ai_summary_settings(
        changed, {"clear_ai_summary_api_key": True}
    )
    assert cleared[key_name] == ""
    assert changes == ["cleared AI summary API key"]

    public = module.ai_summary_settings(SimpleNamespace(data={}, options=changed))
    assert public == {
        "ai_summary_local_timeout": 120,
        "ai_summary_client_timeout_seconds": 35,
        "ai_summary_provider": "grok",
        "ai_summary_api_key_configured": True,
        "ai_summary_key_configured": True,
        "ai_summary_model": "grok-4.5",
        "ai_summary_local_endpoint": "",
        "ai_summary_local_model": "",
        "ai_summary_openrouter_model": "",
        "ai_summary_openai_model": "gpt-6.1-sol",
        "ai_summary_auto_refresh": False,
    }
    assert "grok-secret" not in json.dumps(public)

    with pytest.raises(module.AISummaryError) as caught:
        module.apply_ai_summary_settings(
            changed,
            {
                "ai_summary_api_key": "replacement",
                "clear_ai_summary_api_key": True,
            },
        )
    assert caught.value.code == "invalid_ai_settings"
    assert changed == {provider_name: "grok", key_name: "grok-secret"}


def test_openrouter_settings_require_model_preserve_it_and_isolate_key():
    module = _load_module()
    options, changes = module.apply_ai_summary_settings(
        {},
        {
            "ai_summary_provider": "openrouter",
            "ai_summary_openrouter_model": "openai/gpt-4.1-mini",
            "ai_summary_api_key": "openrouter-secret",
        },
    )
    assert changes == [
        "updated AI summary provider",
        "updated AI summary API key",
        "updated OpenRouter AI model",
    ]
    assert options[module.CONF_OPTIMIZATION_AI_SUMMARY_OPENROUTER_MODEL] == "openai/gpt-4.1-mini"
    public = module.ai_summary_settings(SimpleNamespace(data={}, options=options))
    assert public["ai_summary_provider"] == "openrouter"
    assert public["ai_summary_key_configured"] is True
    assert public["ai_summary_model"] == "openai/gpt-4.1-mini"
    assert "openrouter-secret" not in json.dumps(public)

    switched, changes = module.apply_ai_summary_settings(
        options, {"ai_summary_provider": "gemini"}
    )
    assert switched[module.CONF_OPTIMIZATION_AI_SUMMARY_API_KEY] == ""
    assert switched[module.CONF_OPTIMIZATION_AI_SUMMARY_OPENROUTER_MODEL] == "openai/gpt-4.1-mini"
    assert changes == ["updated AI summary provider", "cleared AI summary API key"]

    with pytest.raises(module.AISummaryError) as caught:
        module.apply_ai_summary_settings({}, {"ai_summary_provider": "openrouter"})
    assert caught.value.code == "invalid_openrouter_model"


class _FakeResponse:
    def __init__(self, status: int, body):
        self.status = status
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_openrouter_reasoning_route_does_not_require_unsupported_temperature():
    """#63: require_parameters must not exclude a route lacking temperature."""
    module = _load_module()
    class ReasoningRoute(_FakeSession):
        def post(self, url, **kwargs):
            # Public OpenRouter model capabilities for the reported route list
            # structured outputs and max_tokens, but no temperature support.
            if 'temperature' in kwargs['json']:
                return _FakeResponse(400, {'error': {'message': 'Unsupported temperature'}})
            return super().post(url, **kwargs)
    session = ReasoningRoute(_FakeResponse(200, {
        'choices': [{'message': {'content': json.dumps(_model_output())}}]}))
    result = asyncio.run(module.OpenRouterAISummaryProvider().generate(
        session=session, api_key='test-only', model='openai/gpt-5.6-sol', context={}))
    assert result == _model_output()
    payload = session.calls[0][1]['json']
    assert payload['provider'] == {'require_parameters': True}
    assert payload['response_format']['json_schema']['strict'] is True


class _TimeoutContext:
    async def __aenter__(self):
        raise asyncio.TimeoutError

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _TimeoutSession:
    def post(self, url, **kwargs):
        return _TimeoutContext()


def test_provider_adapters_use_structured_requests_without_key_in_body():
    module = _load_module()
    context = module.build_compact_context(_snapshot())

    gemini_session = _FakeSession(
        _FakeResponse(
            200,
            {
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {"type": "text", "text": json.dumps(_model_output())}
                        ],
                    }
                ]
            },
        )
    )
    result = asyncio.run(
        module.GeminiAISummaryProvider().generate(
            session=gemini_session,
            api_key="gemini-secret",
            model="gemini-3.5-flash-lite",
            context=context,
        )
    )
    assert result == _model_output()
    gemini_url, gemini_request = gemini_session.calls[0]
    assert gemini_url.endswith("/v1beta/interactions")
    assert gemini_request["headers"]["x-goog-api-key"] == "gemini-secret"
    assert gemini_request["json"]["store"] is False
    assert "temperature" not in gemini_request["json"]["generation_config"]
    assert gemini_request["json"]["response_format"]["schema"] == module.MODEL_OUTPUT_SCHEMA
    assert "gemini-secret" not in json.dumps(gemini_request["json"])

    grok_session = _FakeSession(
        _FakeResponse(
            200,
            {"choices": [{"message": {"content": json.dumps(_model_output())}}]},
        )
    )
    result = asyncio.run(
        module.GrokAISummaryProvider().generate(
            session=grok_session,
            api_key="grok-secret",
            model="grok-4.5",
            context=context,
        )
    )
    assert result == _model_output()
    grok_url, grok_request = grok_session.calls[0]
    assert grok_url.endswith("/v1/chat/completions")
    assert grok_request["headers"]["Authorization"] == "Bearer grok-secret"
    assert grok_request["json"]["response_format"]["json_schema"]["strict"] is True
    assert "grok-secret" not in json.dumps(grok_request["json"])

    openrouter_session = _FakeSession(
        _FakeResponse(
            200,
            {"choices": [{"message": {"content": json.dumps(_model_output())}}]},
        )
    )
    result = asyncio.run(
        module.OpenRouterAISummaryProvider().generate(
            session=openrouter_session,
            api_key="openrouter-secret",
            model="openai/gpt-4.1-mini",
            context=context,
        )
    )
    assert result == _model_output()
    openrouter_url, openrouter_request = openrouter_session.calls[0]
    assert openrouter_url == "https://openrouter.ai/api/v1/chat/completions"
    assert openrouter_request["headers"]["Authorization"] == "Bearer openrouter-secret"
    assert openrouter_request["json"]["model"] == "openai/gpt-4.1-mini"
    assert openrouter_request["json"]["response_format"]["json_schema"]["strict"] is True
    assert openrouter_request["json"]["provider"] == {"require_parameters": True}
    assert "openrouter-secret" not in json.dumps(openrouter_request["json"])


def test_local_openai_endpoint_rules_optional_auth_and_schema_fallback(monkeypatch):
    module = _load_module()
    context = module.build_compact_context(_snapshot())

    assert module.normalize_local_endpoint("http://127.0.0.1:3000") == (
        "http://127.0.0.1:3000/v1/chat/completions"
    )
    for unsafe in (
        "https://user:secret@192.168.1.20/v1",
        "http://169.254.169.254/v1",
        "ftp://192.168.1.20/v1",
        "http://192.168.1.20/not-chat",
    ):
        with pytest.raises(module.AISummaryError):
            module.normalize_local_endpoint(unsafe)

    monkeypatch.setattr(module.socket, "getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("127.0.0.1", 0))
    ])

    class SequenceSession:
        def __init__(self):
            self.calls = []
            self.responses = [
                _FakeResponse(400, {}),
                _FakeResponse(200, {"choices": [{"message": {"content": json.dumps(_model_output())}}]}),
            ]

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)

    session = SequenceSession()
    result = asyncio.run(
        module.LocalOpenAICompatibleAISummaryProvider().generate(
            session=session,
            api_key="",
            model="local-model",
            context=context,
            endpoint="http://127.0.0.1:3000/v1",
        )
    )
    assert result == _model_output()
    assert len(session.calls) == 2
    assert "Authorization" not in session.calls[0][1]["headers"]
    assert session.calls[0][1]["allow_redirects"] is False
    assert "response_format" in session.calls[0][1]["json"]
    assert "response_format" not in session.calls[1][1]["json"]


def test_local_settings_are_write_only_and_provider_switch_invalidates_values():
    module = _load_module()
    options, changes = module.apply_ai_summary_settings(
        {},
        {
            "ai_summary_provider": "local_openai_compatible",
            "ai_summary_local_endpoint": "http://127.0.0.1:3000",
            "ai_summary_local_model": "llama-local",
            "ai_summary_api_key": "local-secret",
            "ai_summary_auto_refresh": True,
        },
    )
    assert options[module.CONF_OPTIMIZATION_AI_SUMMARY_LOCAL_ENDPOINT].endswith("/v1/chat/completions")
    assert options[module.CONF_OPTIMIZATION_AI_SUMMARY_LOCAL_MODEL] == "llama-local"
    assert options[module.CONF_OPTIMIZATION_AI_SUMMARY_AUTO_REFRESH] is True
    public = module.ai_summary_settings(SimpleNamespace(data={}, options=options))
    assert "local-secret" not in json.dumps(public)
    assert public["ai_summary_local_endpoint"] == "http://127.0.0.1:3000/v1/chat/completions"
    switched, _ = module.apply_ai_summary_settings(options, {"ai_summary_provider": "gemini"})
    assert module.CONF_OPTIMIZATION_AI_SUMMARY_LOCAL_ENDPOINT not in switched
    assert switched[module.CONF_OPTIMIZATION_AI_SUMMARY_API_KEY] == ""


@pytest.mark.parametrize(
    ("status", "code", "http_status"),
    (
        (401, "provider_auth_failed", 502),
        (403, "provider_auth_failed", 502),
        (429, "provider_rate_limited", 429),
        (500, "provider_unavailable", 502),
        (400, "provider_rejected", 502),
    ),
)
def test_provider_http_errors_are_safe_and_not_ha_auth(status, code, http_status):
    module = _load_module()
    session = _FakeSession(_FakeResponse(status, {"error": "secret upstream body"}))
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(
            module.GrokAISummaryProvider().generate(
                session=session,
                api_key="not-returned",
                model="grok-4.5",
                context=module.build_compact_context(_snapshot()),
            )
        )
    assert caught.value.code == code
    assert caught.value.http_status == http_status
    assert "secret upstream body" not in caught.value.message
    assert "not-returned" not in caught.value.message


def test_provider_timeout_and_malformed_json_have_stable_errors():
    module = _load_module()
    context = module.build_compact_context(_snapshot())
    with pytest.raises(module.AISummaryError) as timed_out:
        asyncio.run(
            module.GeminiAISummaryProvider().generate(
                session=_TimeoutSession(),
                api_key="not-returned",
                model="gemini-3.5-flash-lite",
                context=context,
            )
        )
    assert timed_out.value.code == "provider_timeout"
    assert timed_out.value.http_status == 504

    malformed = _FakeSession(_FakeResponse(200, ValueError("bad json")))
    with pytest.raises(module.AISummaryError) as invalid:
        asyncio.run(
            module.GrokAISummaryProvider().generate(
                session=malformed,
                api_key="not-returned",
                model="grok-4.5",
                context=context,
            )
        )
    assert invalid.value.code == "invalid_provider_response"


def test_service_cache_refresh_deduplication_and_plan_change_guard():
    module = _load_module()
    snapshot = _snapshot()

    class Adapter:
        def __init__(self):
            self.calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            await asyncio.sleep(0)
            return _model_output()

    adapter = Adapter()
    module.PROVIDER_ADAPTERS["gemini"] = adapter
    service = module.AISummaryService(object(), lambda: snapshot)

    async def exercise():
        first, joined = await asyncio.gather(
            service.generate(provider="gemini", api_key="key", refresh=False),
            service.generate(provider="gemini", api_key="key", refresh=False),
        )
        cached = await service.generate(provider="gemini", api_key="key", refresh=False)
        refreshed = await service.generate(provider="gemini", api_key="key", refresh=True)
        return first, joined, cached, refreshed

    first, joined, cached, refreshed = asyncio.run(exercise())
    assert adapter.calls == 2
    assert first["cache_hit"] is False
    assert joined["summary"] == first["summary"]
    assert cached["cache_hit"] is True
    assert refreshed["cache_hit"] is False
    assert service.status(
        snapshot=snapshot, provider="gemini", api_key="key"
    )["state"] == "ready"

    class MutatingAdapter:
        async def generate(self, **kwargs):
            snapshot["next_actions"][1]["action"] = "idle"
            return _model_output()

    service.invalidate()
    module.PROVIDER_ADAPTERS["gemini"] = MutatingAdapter()
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(service.generate(provider="gemini", api_key="key", refresh=False))
    assert caught.value.code == "plan_changed"


def test_invalidated_manual_explanation_cannot_reappear_in_fresh_status():
    module = _load_module()
    snapshot = _snapshot()

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        class DeferredAdapter:
            async def generate(self, **kwargs):
                entered.set()
                await release.wait()
                return {**_model_output(), "headline": "Explanation from the old account."}

        module.PROVIDER_ADAPTERS["gemini"] = DeferredAdapter()
        service = module.AISummaryService(object(), lambda: snapshot)
        pending = asyncio.create_task(service.generate(provider="gemini", api_key="old-key", refresh=False))
        await entered.wait()
        service.invalidate()
        release.set()
        result = (await asyncio.gather(pending, return_exceptions=True))[0]
        status = service.status(snapshot=snapshot, provider="gemini", api_key="new-key")
        assert status["summary"] is None, "An old account's result must not survive invalidation in fresh status"
        assert status["state"] == "not_generated" and status["last_error"] is None
        assert isinstance(result, module.AISummaryError) and result.code == "settings_changed"
    asyncio.run(exercise())


@pytest.mark.parametrize("old_failure", [False, True])
def test_new_generation_does_not_join_or_publish_old_credentials_work(old_failure):
    module = _load_module()
    snapshot = _snapshot()

    async def exercise():
        old_entered, old_release = asyncio.Event(), asyncio.Event()

        class DeferredAdapter:
            async def generate(self, **kwargs):
                if kwargs["api_key"] == "old-key":
                    old_entered.set()
                    await old_release.wait()
                    if old_failure:
                        raise module.AISummaryError("provider_timeout", "The old provider request timed out.")
                    return {**_model_output(), "headline": "Explanation from the old account."}
                return {**_model_output(), "headline": "Explanation from the new account."}

        module.PROVIDER_ADAPTERS["gemini"] = DeferredAdapter()
        service = module.AISummaryService(object(), lambda: snapshot)
        old_request = asyncio.create_task(service.generate(provider="gemini", api_key="old-key", refresh=False))
        await old_entered.wait()
        service.invalidate()
        new_request = asyncio.create_task(service.generate(provider="gemini", api_key="new-key", refresh=False))
        new_started = asyncio.Event()
        asyncio.get_running_loop().call_soon(new_started.set)
        await new_started.wait()
        old_release.set()
        old_result, new_result = await asyncio.gather(old_request, new_request, return_exceptions=True)
        assert isinstance(new_result, dict), "Current settings must not join the retired generation's request"
        assert new_result["summary"]["headline"] == "Explanation from the new account."
        assert isinstance(old_result, module.AISummaryError) and old_result.code == "settings_changed"
        fresh_status = service.status(snapshot=snapshot, provider="gemini", api_key="new-key")
        assert fresh_status["summary"] == new_result["summary"] and fresh_status["last_error"] is None
        cached = await service.generate(provider="gemini", api_key="new-key", refresh=False)
        assert cached["cache_hit"] and cached["summary"] == new_result["summary"]
    asyncio.run(exercise())


def test_service_sends_feedback_against_last_successfully_explained_plan():
    module = _load_module()
    snapshot = _snapshot()

    class RecordingAdapter:
        def __init__(self):
            self.contexts = []

        async def generate(self, **kwargs):
            self.contexts.append(kwargs["context"])
            return _model_output()

    adapter = RecordingAdapter()
    module.PROVIDER_ADAPTERS["gemini"] = adapter
    service = module.AISummaryService(object(), lambda: snapshot)

    asyncio.run(service.generate(provider="gemini", api_key="key", refresh=False))
    snapshot["next_actions"][0]["action"] = "idle"
    snapshot["schedule"]["import_price"][0] = 0.25
    asyncio.run(service.generate(provider="gemini", api_key="key", refresh=False))

    assert len(adapter.contexts) == 2
    assert adapter.contexts[0]["feedback"]["available"] is False
    second_feedback = adapter.contexts[1]["feedback"]
    assert second_feedback["available"] is True
    assert second_feedback["plan_changed"] is True
    assert "tariff" in {
        item["kind"] for item in second_feedback["verified_input_changes"]
    }


def test_malformed_refresh_keeps_last_valid_summary_as_fallback():
    module = _load_module()
    snapshot = _snapshot()

    class Adapter:
        def __init__(self):
            self.fail = False

        async def generate(self, **kwargs):
            if self.fail:
                raise module.AISummaryError(
                    "invalid_provider_response",
                    "The AI provider returned an invalid structured response.",
                )
            return _model_output()

    adapter = Adapter()
    module.PROVIDER_ADAPTERS["gemini"] = adapter
    service = module.AISummaryService(object(), lambda: snapshot)
    first = asyncio.run(
        service.generate(provider="gemini", api_key="key", refresh=False)
    )
    adapter.fail = True

    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(
            service.generate(provider="gemini", api_key="key", refresh=True)
        )

    assert caught.value.code == "invalid_provider_response"
    status = service.status(snapshot=snapshot, provider="gemini", api_key="key")
    assert status["state"] == "ready"
    assert status["summary"] == first["summary"]
    assert status["last_error"] == {
        "code": "invalid_provider_response",
        "message": "The AI provider returned an invalid structured response.",
    }


def test_local_provider_reports_length_stop_without_parsing_partial_content(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.socket, "getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("127.0.0.1", 0))
    ])
    session = _FakeSession(_FakeResponse(200, {
        "choices": [{
            "finish_reason": "length",
            "message": {"content": '{"headline":"partial",'},
        }],
    }))
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(module.LocalOpenAICompatibleAISummaryProvider().generate(
            session=session, api_key="", model="local-model",
            context=module.build_compact_context(_snapshot()),
            endpoint="http://127.0.0.1:3000/v1",
        ))
    assert caught.value.code == "provider_response_truncated"
    assert session.calls[0][1]["json"]["max_tokens"] == module.LOCAL_OPENAI_MAX_TOKENS


def test_auto_refresh_is_material_debounced_coalesced_and_cancelled():
    module = _load_module()
    snapshot = _snapshot()
    module.AUTO_REFRESH_DEBOUNCE_SECONDS = 0
    module.AUTO_REFRESH_COOLDOWN_SECONDS = 0

    class Adapter:
        def __init__(self):
            self.calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            return _model_output()

    adapter = Adapter()
    module.PROVIDER_ADAPTERS["gemini"] = adapter
    service = module.AISummaryService(object(), lambda: snapshot)

    async def exercise():
        service.schedule_auto_refresh(provider="gemini", api_key="key")
        # An equivalent committed solve must not add a request.
        service.schedule_auto_refresh(provider="gemini", api_key="key")
        for _ in range(5):
            await asyncio.sleep(0)
        assert adapter.calls == 1
        snapshot["next_actions"][0]["action"] = "idle"
        service.schedule_auto_refresh(provider="gemini", api_key="key")
        for _ in range(5):
            await asyncio.sleep(0)
        assert adapter.calls == 2
        await service.async_shutdown()
        assert service._auto_task is None

    asyncio.run(exercise())


def test_timeout_status_is_rendered_after_the_ha_card_reloads():
    """A retained provider error must not become a generic ready state on reload."""
    source = (ROOT / "custom_components" / "power_sync" / "frontend" / "power-sync-strategy.js").read_text()
    load_status = source[source.index("async _loadStatusOnce()"):source.index("  _retryStatus()")]

    assert "else if (status.last_error)" in load_status
    assert "this._errorCode = status.last_error.code || 'request_failed';" in load_status
    assert "refreshError: status.last_error?.code || null" in load_status


def test_http_views_register_explicit_generation_and_never_return_key():
    source = INIT_PATH.read_text()
    assert "hass.http.register_view(OptimizationAISummaryView(hass))" in source
    assert 'url = "/api/power_sync/optimization/ai_summary"' in source
    optimization_get = source[
        source.index("class OptimizationView") : source.index("class OptimizationSettingsView")
    ]
    assert ".generate(" not in optimization_get
    summary_view = source[source.index("class OptimizationAISummaryView") :]
    assert 'set(payload) - {"refresh"}' in summary_view
    assert 'api_key=configured_api_key(config_entry)' in summary_view
    assert '"ai_summary_api_key"' not in summary_view
    assert 'configured_local_endpoint' in summary_view
    assert 'async_shutdown()' in source
    filter_source = source[
        source.index("class SensitiveDataFilter") : source.index("class CalendarHistoryView")
    ]
    assert "xai-[a-zA-Z0-9_-]" in filter_source
    assert "AIza[a-zA-Z0-9_-]" in filter_source


def test_optimizer_snapshot_exposes_verified_currency_soc_and_solar_context():
    source = COORDINATOR_PATH.read_text()
    api_source = source[source.index("def get_api_data") : source.index("async def set_settings")]

    assert "currency_metadata(currency_for_entry(self._entry, self.hass))" in api_source
    assert 'data["battery_soc_percent"]' in api_source
    assert '"solar_next_24h_kwh"' in api_source
    assert '"solar_peak_kw"' in api_source


def test_openai_and_chatgpt_settings_use_explicit_auth_and_bounded_model():
    module = _load_module()
    options, _ = module.apply_ai_summary_settings({}, {
        "ai_summary_provider": "openai", "ai_summary_api_key": "api-secret",
        "ai_summary_openai_model": "gpt-6.1-sol",
    })
    entry = SimpleNamespace(data={}, options=options)
    assert module.configured_openai_model(entry) == "gpt-6.1-sol"
    assert module.ai_summary_settings(entry)["ai_summary_key_configured"] is True
    options, _ = module.apply_ai_summary_settings(options, {"ai_summary_provider": "chatgpt"})
    entry.options = options
    assert module.configured_api_key(entry) == ""
    assert module.ai_summary_settings(entry)["ai_summary_key_configured"] is False
    public = module.ai_summary_settings(entry, chatgpt_status={"connected": True, "access_token": "oauth-secret"})
    assert public["ai_summary_key_configured"] is True
    assert public["ai_summary_api_key_configured"] is False
    assert public["ai_summary_model"] == "gpt-6.1-sol"
    assert "oauth-secret" not in json.dumps(public)
    for value in ("", "bad model", "x" * 129, 123, "bad\x00id"):
        with pytest.raises(module.AISummaryError) as caught:
            module.apply_ai_summary_settings(options, {"ai_summary_openai_model": value})
        assert caught.value.code == "invalid_openai_model"


class _SSEContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class _SSEResponse(_FakeResponse):
    def __init__(self, events=(), *, chunks=None, status=200, content_type="text/event-stream"):
        super().__init__(status, {})
        self.headers = {"Content-Type": content_type}
        if chunks is None:
            data = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\r\n\r\n" for event in events).encode()
            chunks = [data[i:i + 3] for i in range(0, len(data), 3)]
        self.content = _SSEContent(chunks)


def _completed_events(output=None):
    value = _model_output() if output is None else output
    return [
        {"type": "response.output_text.delta", "delta": json.dumps(value, ensure_ascii=False)},
        {"type": "response.completed", "response": {"status": "completed"}},
    ]


def test_responses_adapters_use_public_route_with_distinct_request_contracts():
    module = _load_module()
    output = {**_model_output(), "headline": "Charge at £0.10 — then export."}
    context = module.build_compact_context(_snapshot())
    api = _FakeSession(_SSEResponse(_completed_events(output)))
    assert asyncio.run(module.OpenAIResponsesAISummaryProvider().generate(
        session=api, api_key="api-secret", model="gpt-6.1-sol", context=context)) == output
    url, request = api.calls[0]
    assert url == "https://api.openai.com/v1/responses"
    assert request["headers"]["Authorization"] == "Bearer api-secret"
    assert request["allow_redirects"] is False
    assert request["json"]["text"]["format"]["strict"] is True
    assert request["json"]["instructions"] == module.SYSTEM_PROMPT
    assert request["json"]["stream"] is True
    assert request["json"]["store"] is False
    assert "api-secret" not in json.dumps(request["json"])
    token_calls = []
    async def token(**kwargs):
        token_calls.append(kwargs)
        return "oauth-secret"
    subscription = _FakeSession(_SSEResponse(_completed_events(output), content_type=""))
    assert asyncio.run(module.ChatGPTResponsesAISummaryProvider().generate(
        session=subscription, model="gpt-6.1-sol", context=context, access_token=token)) == output
    payload = subscription.calls[0][1]["json"]
    assert token_calls == [{}]
    assert set(payload) == {"model", "instructions", "input", "stream", "store"}
    assert "OUTPUT_SCHEMA_JSON" in payload["input"][0]["content"]
    assert "oauth-secret" not in json.dumps(payload)
    assert "OpenAI-Project" not in subscription.calls[0][1]["headers"]


def test_chatgpt_retries_only_one_http_401_with_fresh_credentials():
    module = _load_module()
    class Sessions:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []
        def post(self, url, **kwargs):
            self.calls.append(kwargs)
            return self.responses.pop(0)
    token_calls = []
    async def token(**kwargs):
        token_calls.append(kwargs)
        return "new-token" if kwargs.get("force_refresh") else "old-token"
    session = Sessions([_SSEResponse(status=401), _SSEResponse(_completed_events())])
    assert asyncio.run(module.ChatGPTResponsesAISummaryProvider().generate(
        session=session, model="gpt-6.1-sol", context={}, access_token=token)) == _model_output()
    assert token_calls == [{}, {"force_refresh": True, "rejected_token": "old-token"}]
    assert [call["headers"]["Authorization"] for call in session.calls] == ["Bearer old-token", "Bearer new-token"]
    token_calls.clear()
    session = Sessions([_SSEResponse(status=401), _SSEResponse(status=401)])
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(module.ChatGPTResponsesAISummaryProvider().generate(
            session=session, model="gpt-6.1-sol", context={}, access_token=token))
    assert caught.value.code == "chatgpt_reconnect_required"
    assert len(session.calls) == 2


@pytest.mark.parametrize("events, code", [
    ([{"type": "response.output_text.delta", "delta": "partial secret"}], "provider_stream_interrupted"),
    ([{"type": "response.incomplete"}], "provider_response_truncated"),
    ([{"type": "response.failed", "response": {"error": {"message": "secret"}}}], "provider_unavailable"),
    ([{"type": "error", "error": {"code": "usage_limit_reached", "message": "secret"}}], "chatgpt_usage_limit"),
])
def test_chatgpt_stream_failures_are_sanitized_and_never_replayed(events, code):
    module = _load_module()
    session = _FakeSession(_SSEResponse(events))
    async def token(**kwargs):
        assert kwargs == {}
        return "oauth-secret"
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(module.ChatGPTResponsesAISummaryProvider().generate(
            session=session, model="gpt-6.1-sol", context={}, access_token=token))
    assert caught.value.code == code
    assert "secret" not in caught.value.message
    assert len(session.calls) == 1


@pytest.mark.parametrize("status, code", [(403, "chatgpt_not_enabled"), (429, "chatgpt_usage_limit"), (500, "provider_unavailable")])
def test_chatgpt_http_failures_are_safe_and_not_retried(status, code):
    module = _load_module()
    session = _FakeSession(_SSEResponse(status=status))
    async def token(**kwargs):
        return "oauth-secret"
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(module.ChatGPTResponsesAISummaryProvider().generate(
            session=session, model="gpt-6.1-sol", context={}, access_token=token))
    assert caught.value.code == code
    assert len(session.calls) == 1


def test_responses_stream_requires_completion_and_bounds_output_and_events(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "MAX_RESPONSES_OUTPUT_CHARS", 10)
    session = _FakeSession(_SSEResponse(_completed_events()))
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(module.OpenAIResponsesAISummaryProvider().generate(
            session=session, api_key="key", model="gpt-6.1-sol", context={}))
    assert caught.value.code == "invalid_provider_response"
    monkeypatch.setattr(module, "MAX_RESPONSES_OUTPUT_CHARS", 64 * 1024)
    monkeypatch.setattr(module, "MAX_RESPONSES_EVENTS", 1)
    session = _FakeSession(_SSEResponse(_completed_events()))
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(module.OpenAIResponsesAISummaryProvider().generate(
            session=session, api_key="key", model="gpt-6.1-sol", context={}))
    assert caught.value.code == "invalid_provider_response"
    session = _FakeSession(_SSEResponse(chunks=[b'data: {broken}\n\n']))
    with pytest.raises(module.AISummaryError):
        asyncio.run(module.OpenAIResponsesAISummaryProvider().generate(
            session=session, api_key="key", model="gpt-6.1-sol", context={}))


def test_chatgpt_service_and_auto_refresh_do_not_require_or_queue_a_token(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "AUTO_REFRESH_DEBOUNCE_SECONDS", 0)
    monkeypatch.setattr(module, "AUTO_REFRESH_COOLDOWN_SECONDS", 0)
    class Auth:
        calls = 0
        connected = True
        def status(self):
            return {"connected": self.connected}
        async def async_access_token(self, **kwargs):
            self.calls += 1
            return "oauth-secret"
    auth = Auth()
    session = _FakeSession(_SSEResponse(_completed_events()))
    service = module.AISummaryService(session, _snapshot, chatgpt_auth=auth)
    assert service.status(snapshot=_snapshot(), provider="chatgpt", api_key="")["configured"] is True
    assert auth.calls == 0
    async def run():
        service.schedule_auto_refresh(provider="chatgpt", api_key="ignored-api-key", openai_model="gpt-6.1-sol")
        assert auth.calls == 0
        assert service._auto_pending[2] == ""
        await service._auto_task
        result = await service.generate(provider="chatgpt", api_key="", refresh=False)
        assert result["cache_hit"] is True
        await service.async_shutdown()
    asyncio.run(run())
    assert auth.calls == 1
    assert service.status(snapshot=_snapshot(), provider="chatgpt", api_key="")["state"] == "ready"
    auth.connected = False
    assert service.status(snapshot=_snapshot(), provider="chatgpt", api_key="api-secret")["configured"] is False
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(service.generate(provider="chatgpt", api_key="api-secret", refresh=False))
    assert caught.value.code == "chatgpt_reconnect_required"


def test_responses_final_only_completion_and_stream_byte_limit(monkeypatch):
    module = _load_module()
    final = {"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(_model_output())}]}]}}
    assert asyncio.run(module._read_responses_stream(_SSEResponse([final]), subscription=False)) == _model_output()
    monkeypatch.setattr(module, "MAX_RESPONSES_STREAM_BYTES", 8)
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(module._read_responses_stream(_SSEResponse([final]), subscription=False))
    assert caught.value.code == "invalid_provider_response"
    monkeypatch.setattr(module, "MAX_RESPONSES_STREAM_BYTES", 1024)
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(module._read_responses_stream(_SSEResponse(chunks=[b'\xff']), subscription=False))
    assert caught.value.code == "invalid_provider_response"


def test_chatgpt_service_maps_auth_failures_without_leaking_error_details(monkeypatch):
    module = _load_module()
    auth_module = ModuleType("custom_components.power_sync.optimization.chatgpt_auth")
    class ChatGPTAuthError(Exception):
        def __init__(self, code):
            self.code = code
            super().__init__("private-refresh-token")
    auth_module.ChatGPTAuthError = ChatGPTAuthError
    auth_module._TERMINAL_ERRORS = {"invalid_grant", "invalid_client", "unauthorized_client", "access_denied", "future_terminal_error"}
    monkeypatch.setitem(sys.modules, auth_module.__name__, auth_module)
    class Auth:
        def status(self):
            return {"connected": True}
        async def async_access_token(self, **kwargs):
            raise ChatGPTAuthError("chatgpt_not_enabled")
    service = module.AISummaryService(_FakeSession(_SSEResponse()), _snapshot, chatgpt_auth=Auth())
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(service.generate(provider="chatgpt", api_key="", refresh=False))
    assert caught.value.code == "chatgpt_not_enabled"
    assert "private-refresh-token" not in caught.value.message
    assert service._last_error["code"] == "chatgpt_not_enabled"


def test_config_flow_keeps_chatgpt_selection_and_uses_server_auth_readiness():
    """Execute the options metadata block without loading the unrelated HA flows."""
    import textwrap
    module = _load_module()
    source = (ROOT / "custom_components" / "power_sync" / "config_flow.py").read_text()
    block = source[source.index("        current_ai_provider = str("):source.index("        current_form_values: dict[str, Any] = {", source.index("        current_ai_provider = str("))]
    options = {
        module.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER: "chatgpt",
        module.CONF_OPTIMIZATION_AI_SUMMARY_API_KEY: "unrelated-api-key",
        module.CONF_OPTIMIZATION_AI_SUMMARY_OPENAI_MODEL: "gpt-6.1-sol",
    }
    entry = SimpleNamespace(data={}, options=options, entry_id="entry")
    auth = SimpleNamespace(status=lambda: {"connected": False})
    flow = SimpleNamespace(config_entry=entry, hass=SimpleNamespace(data={"power_sync": {"entry": {}}}), _get_option=lambda key, default: options.get(key, default))
    globals_ = {name: getattr(module, name) for name in dir(module) if name.startswith("CONF_OPTIMIZATION_AI_")}
    globals_.update(DEFAULT_OPTIMIZATION_AI_SUMMARY_PROVIDER="gemini", DOMAIN="power_sync", self=flow,
                    configured_openai_model=module.configured_openai_model,
                    get_chatgpt_auth=lambda hass, entry_id: auth)
    exec(textwrap.dedent(block), globals_)
    assert globals_["current_ai_provider"] == "chatgpt"
    assert globals_["current_ai_key_configured"] is False
    assert globals_["current_ai_openai_model"] == "gpt-6.1-sol"
    auth.status = lambda: {"connected": True}
    exec(textwrap.dedent(block), globals_)
    assert globals_["current_ai_key_configured"] is True
    flow.hass.data.clear()
    exec(textwrap.dedent(block), globals_)
    assert globals_["current_ai_key_configured"] is False


def test_config_flow_model_field_is_migrated_saved_and_translated():
    import ast
    path = ROOT / "custom_components" / "power_sync" / "config_flow.py"
    source = path.read_text()
    tree = ast.parse(source)
    # Both auth modes are explicit selector options; each model marker has only
    # one positional field key, guarding accidental mixed selector fields.
    choices = {keyword.value.value for call in ast.walk(tree) if isinstance(call, ast.Call)
               and isinstance(call.func, ast.Name) and call.func.id == "SelectOptionDict"
               for keyword in call.keywords if keyword.arg == "value" and isinstance(keyword.value, ast.Constant)}
    assert {"openai", "chatgpt"}.issubset(choices)
    model_markers = [call for call in ast.walk(tree) if isinstance(call, ast.Call)
                     and isinstance(call.func, ast.Attribute) and call.func.attr in {"Optional", "Required"}
                     and any(isinstance(arg, ast.Name) and arg.id == "CONF_OPTIMIZATION_AI_SUMMARY_OPENAI_MODEL" for arg in call.args)]
    assert len(model_markers) == 1
    assert len(model_markers[0].args) == 1
    assert '"ai_summary_openai_model": user_input.get(' in source
    assert "new_data.pop(CONF_OPTIMIZATION_AI_SUMMARY_OPENAI_MODEL, None)" in source
    assert "CONF_OPTIMIZATION_AI_SUMMARY_OPENAI_MODEL: current_ai_openai_model" in source
    for name in ("strings.json", "translations/en.json"):
        data = json.loads((ROOT / "custom_components" / "power_sync" / name).read_text())
        section = data["options"]["step"]["optimization"]["sections"]["ai_explanations"]
        assert "optimization_ai_summary_openai_model" in section["data"]
        assert "gpt-6.1-sol" in section["data_description"]["optimization_ai_summary_openai_model"]
        assert "dashboard AI explanation card" in section["description"]


@pytest.mark.parametrize("payload", [
    {"clear_ai_summary_api_key": True},
    {"ai_summary_provider": "grok"},
])
def test_clear_and_switch_suppress_inherited_key_with_persistent_empty_option(payload):
    module = _load_module()
    key = module.CONF_OPTIMIZATION_AI_SUMMARY_API_KEY
    provider = module.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER
    entry = SimpleNamespace(data={key: "old-inherited-secret", provider: "gemini"}, options={})
    assert module.configured_api_key(entry) == "old-inherited-secret"
    # Clearing/switching must suppress entry.data even when the helper's options
    # contain no key. The empty override also survives a blank-key save/reload.
    entry.options, _ = module.apply_ai_summary_settings(entry.options, payload)
    assert entry.options[key] == ""
    assert module.configured_api_key(entry) == ""
    public = module.ai_summary_settings(entry)
    assert public["ai_summary_api_key_configured"] is False
    assert public["ai_summary_key_configured"] is False
    entry.options, changes = module.apply_ai_summary_settings(entry.options, {"ai_summary_api_key": " "})
    assert changes == []
    assert module.configured_api_key(entry) == ""
    # An explicit replacement overrides both the sentinel and inherited data.
    entry.options, _ = module.apply_ai_summary_settings(entry.options, {"ai_summary_api_key": "new-replacement"})
    assert module.configured_api_key(entry) == "new-replacement"
    assert module.ai_summary_settings(entry)["ai_summary_api_key_configured"] is True
    assert entry.data[key] == "old-inherited-secret"


def test_clearing_an_empty_override_is_idempotent():
    module = _load_module()
    key = module.CONF_OPTIMIZATION_AI_SUMMARY_API_KEY
    options, changes = module.apply_ai_summary_settings({key: ""}, {"clear_ai_summary_api_key": True})
    assert options[key] == ""
    assert changes == []


def test_public_settings_preload_provider_models_without_echoing_credentials():
    module = _load_module()
    entry = SimpleNamespace(data={}, options={
        module.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER: "local_openai_compatible",
        module.CONF_OPTIMIZATION_AI_SUMMARY_LOCAL_ENDPOINT: "http://127.0.0.1:11434/v1",
        module.CONF_OPTIMIZATION_AI_SUMMARY_LOCAL_MODEL: "llama3.2",
        module.CONF_OPTIMIZATION_AI_SUMMARY_OPENROUTER_MODEL: "anthropic/claude-sonnet-4",
        module.CONF_OPTIMIZATION_AI_SUMMARY_API_KEY: "SECRET-api-key",
    })
    public = module.ai_summary_settings(entry)
    assert public["ai_summary_local_endpoint"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert public["ai_summary_local_model"] == "llama3.2"
    assert public["ai_summary_openrouter_model"] == "anthropic/claude-sonnet-4"
    assert "SECRET" not in json.dumps(public) and "ai_summary_api_key" not in public
    # Legacy malformed URLs must not expose embedded credentials when preloaded.
    entry.options[module.CONF_OPTIMIZATION_AI_SUMMARY_LOCAL_ENDPOINT] = "http://user:SECRET-password@127.0.0.1"
    public = module.ai_summary_settings(entry)
    assert public["ai_summary_local_endpoint"] == ""
    assert public["ai_summary_key_configured"] is False
    assert "SECRET" not in json.dumps(public)


@pytest.mark.parametrize("failure, expected_code, expected_status", [
    ("network_error", "provider_unavailable", 503),
    ("provider_unavailable", "provider_unavailable", 503),
    ("storage_unavailable", "storage_unavailable", 503),
    ("discovery_failed", "provider_unavailable", 503),
    ("identity_verification_unavailable", "provider_unavailable", 503),
    ("invalid_response", "provider_unavailable", 503),
    ("integration_unavailable", "provider_unavailable", 503),
    (None, "provider_unavailable", 503),
    ("invalid_grant", "chatgpt_reconnect_required", 400),
    ("invalid_client", "chatgpt_reconnect_required", 400),
    ("unauthorized_client", "chatgpt_reconnect_required", 400),
    ("not_connected", "chatgpt_reconnect_required", 400),
    ("future_terminal_error", "chatgpt_reconnect_required", 400),
    ("access_denied", "chatgpt_reconnect_required", 400),
    ("account_mismatch", "chatgpt_reconnect_required", 400),
    ("missing_scope", "chatgpt_reconnect_required", 400),
    ("invalid_id_token", "chatgpt_reconnect_required", 400),
    ("invalid_token_response", "chatgpt_reconnect_required", 400),
])
def test_chatgpt_refresh_distinguishes_retryable_and_auth_errors(monkeypatch, failure, expected_code, expected_status):
    module = _load_module()
    auth_module = ModuleType("custom_components.power_sync.optimization.chatgpt_auth")
    class ChatGPTAuthError(Exception):
        def __init__(self, code):
            self.code = code
            super().__init__("SECRET-refresh-token")
    auth_module.ChatGPTAuthError = ChatGPTAuthError
    auth_module._TERMINAL_ERRORS = {"invalid_grant", "invalid_client", "unauthorized_client", "access_denied", "future_terminal_error"}
    monkeypatch.setitem(sys.modules, auth_module.__name__, auth_module)
    class Auth:
        async def async_access_token(self, **kwargs):
            raise ChatGPTAuthError(failure) if failure else OSError("SECRET-storage-path")
    service = module.AISummaryService(None, _snapshot, chatgpt_auth=Auth())
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(service._chatgpt_access_token())
    assert caught.value.code == expected_code
    assert caught.value.http_status == expected_status
    assert "SECRET" not in caught.value.message
    if expected_code == "provider_unavailable":
        assert "reconnect" not in caught.value.message.lower()


@pytest.mark.parametrize("timeout", [30, 120, 600])
def test_local_timeout_settings_round_trip_and_client_budget(timeout):
    module = _load_module()
    options, changes = module.apply_ai_summary_settings({}, {"ai_summary_local_timeout": timeout})
    entry = SimpleNamespace(data={}, options=options)
    assert module.configured_local_timeout(entry) == timeout
    options[module.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER] = "local_openai_compatible"
    public = module.ai_summary_settings(entry)
    assert public["ai_summary_local_timeout"] == timeout
    assert public["ai_summary_client_timeout_seconds"] == timeout + 5
    assert "updated local AI timeout" in changes


@pytest.mark.parametrize("value", [None, True, False, 29, 601, 30.5, "120"])
def test_local_timeout_rejects_invalid_writes_and_guards_persisted_values(value):
    module = _load_module()
    with pytest.raises(module.AISummaryError) as caught:
        module.apply_ai_summary_settings({}, {"ai_summary_local_timeout": value})
    assert caught.value.code == "invalid_local_ai_timeout"
    assert caught.value.http_status == 400
    assert module.configured_local_timeout(SimpleNamespace(data={}, options={module.CONF_OPTIMIZATION_AI_SUMMARY_LOCAL_TIMEOUT: value})) == 120


def test_local_service_passes_saved_timeout_to_http_and_cloud_stays_at_30_seconds():
    module = _load_module()
    response = {"choices": [{"message": {"content": json.dumps(_model_output())}}]}
    session = _FakeSession(_FakeResponse(200, response))
    service = module.AISummaryService(session, _snapshot)
    asyncio.run(service.generate(provider="local_openai_compatible", api_key="", refresh=True,
        endpoint="http://127.0.0.1:11434/v1", local_model="local-model", local_timeout=240))
    assert session.calls[0][1]["timeout"].total == 240
    cloud = _FakeSession(_FakeResponse(200, response))
    asyncio.run(module.GrokAISummaryProvider().generate(session=cloud, api_key="key", model="grok-4.5", context=module.build_compact_context(_snapshot())))
    assert cloud.calls[0][1]["timeout"].total == 30


def test_local_compatibility_retry_shares_total_timeout_and_cleans_up(monkeypatch):
    module = _load_module()
    attempts = []
    cancelled = []

    async def post_json(*args, **kwargs):
        attempts.append(kwargs)
        try:
            await asyncio.sleep(0.04)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        if len(attempts) == 1:
            raise module.AISummaryError("provider_rejected", "Schema unsupported")
        return {"choices": [{"message": {"content": json.dumps(_model_output())}}]}

    monkeypatch.setattr(module, "_post_json", post_json)
    with pytest.raises(module.AISummaryError) as caught:
        asyncio.run(module.LocalOpenAICompatibleAISummaryProvider().generate(
            session=None, api_key="", model="local", context=module.build_compact_context(_snapshot()),
            endpoint="http://127.0.0.1:11434/v1", timeout_seconds=0.06))
    assert caught.value.code == "provider_timeout"
    assert caught.value.http_status == 504
    assert len(attempts) == 2
    assert cancelled == [True]


def test_local_auto_refresh_keeps_configured_timeout(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "AUTO_REFRESH_DEBOUNCE_SECONDS", 0)
    monkeypatch.setattr(module, "AUTO_REFRESH_COOLDOWN_SECONDS", 0)
    session = _FakeSession(_FakeResponse(200, {"choices": [{"message": {"content": json.dumps(_model_output())}}]}))

    async def exercise():
        service = module.AISummaryService(session, _snapshot)
        service.schedule_auto_refresh(provider="local_openai_compatible", api_key="", endpoint="http://127.0.0.1:11434/v1", local_model="local-model", local_timeout=600)
        await service._auto_task
        assert session.calls[0][1]["timeout"].total == 600
        await service.async_shutdown()

    asyncio.run(exercise())


@pytest.mark.parametrize('savings,comparison', [(-1.04, 'more than'), (0, 'the same as'), (1.04, 'less than')])
def test_concise_outcome_uses_honest_server_cost_comparison(savings, comparison):
    module = _load_module()
    snapshot = _snapshot()
    snapshot.update(predicted_cost=15.90, predicted_savings=savings)
    context = module.build_compact_context(snapshot)
    output = module.prepare_model_output(_model_output(), context)
    assert 'AUD 15.90' in output['expected_outcome']
    assert comparison in output['expected_outcome']
    assert '-1.04' not in output['expected_outcome']
    assert output['strategy'].endswith(output['expected_outcome'])


def test_concise_fallback_never_copies_verbose_or_unknown_window_claims():
    module = _load_module()
    context = module.build_compact_context(_snapshot())
    verbose = _model_output()
    verbose['why_it_matters'] = 'Unsupported causal assertion. ' * 15
    output = module.prepare_model_output(verbose, context)
    assert output['summary_source'] == 'plan'
    assert 'Unsupported causal assertion' not in json.dumps(output)
    assert len(' '.join(output[k] for k in ('headline', 'now', 'next', 'why_it_matters', 'expected_outcome')).split()) <= 140
    assert 'plan' in output['now'].lower()
    assert '01 Aug' in output['next']
    assert '1:00 pm' in output['next']
    verbose['action_explanations'] = [{'window_id': 'invented', 'reason': 'Unsafe'}]
    with pytest.raises(module.AISummaryError) as caught:
        module.prepare_model_output(verbose, context)
    assert caught.value.code == 'invalid_provider_response'


def test_concise_monitoring_and_missing_metrics_remain_explicit():
    module = _load_module()
    snapshot = _snapshot()
    snapshot['monitoring_mode'] = True
    snapshot.pop('currency')
    snapshot.pop('predicted_cost')
    snapshot.pop('predicted_savings')
    context = module.build_compact_context(snapshot)
    output = module.prepare_model_output(_model_output(), context)
    assert 'Monitoring only' in output['now']
    assert 'no commands' in output['now']
    assert output['expected_outcome'] == 'A cost comparison is unavailable for this plan.'
    assert '2.30' not in output['expected_outcome']


@pytest.mark.parametrize('provider', ['gemini', 'grok', 'openrouter', 'openai', 'chatgpt', 'local_openai_compatible'])
def test_every_provider_uses_shared_concise_policy_without_extra_generation(provider):
    module = _load_module()
    snapshot = _snapshot()
    class Adapter:
        calls = 0
        async def generate(self, **kwargs):
            self.calls += 1
            result = _model_output()
            result['now'] = 'The battery is currently performing self-consumption. ' * 6
            return result
    adapter = Adapter()
    module.PROVIDER_ADAPTERS[provider] = adapter
    service = module.AISummaryService(object(), lambda: snapshot)
    service._chatgpt_connected = lambda: True
    kwargs = dict(provider=provider, api_key='test', refresh=False,
                  local_model='local-test', endpoint='http://127.0.0.1:3000/v1',
                  openrouter_model='test/model')
    result = asyncio.run(service.generate(**kwargs))
    assert result['summary']['summary_source'] == 'plan'
    assert len(result['summary']['now']) <= 180
    assert adapter.calls == 1
    assert asyncio.run(service.generate(**kwargs))['cache_hit'] is True
    assert adapter.calls == 1


def test_concise_budget_covers_combined_words_and_legacy_timeline_limits():
    module = _load_module()
    context = module.build_compact_context(_snapshot())
    output = _model_output()
    for key in ('now', 'next', 'why_it_matters'):
        output[key] = 'A ' * 85
    assert module.prepare_model_output(output, context)['summary_source'] == 'plan'
    output = _model_output()
    output['action_explanations'][0]['reason'] = 'A' * 141
    assert module.prepare_model_output(output, context)['summary_source'] == 'plan'
    output = _model_output()
    output['caveats'] = ['One caveat.', 'Second caveat.', 'Third caveat.']
    assert module.prepare_model_output(output, context)['summary_source'] == 'plan'
    # Malformed or enormous content remains an error, never a summary source.
    output['now'] = 'A' * 401
    with pytest.raises(module.AISummaryError):
        module.prepare_model_output(output, context)


def test_concise_fallback_preserves_local_date_and_forecast_assumptions():
    module = _load_module()
    snapshot = _snapshot()
    snapshot['monitoring_mode'] = True
    snapshot['next_action_time'] = '2026-10-24T00:05:00+10:00'
    snapshot['next_action'] = 'charge'
    snapshot['forecast_summary'].update(away_mode=True, solar_next_24h_kwh=0)
    context = module.build_compact_context(snapshot)
    output = _model_output()
    output['why_it_matters'] = 'Lengthy explanation. ' * 20
    result = module.prepare_model_output(output, context)
    assert '24 Oct 2026 at 12:05 am' in result['next']
    assert 'Monitoring only' in result['now']
    assert 'no commands' in result['now']
    assert any('away mode and no solar generation' in text for text in result['caveats'])
    assert module._concise_enough(result)
