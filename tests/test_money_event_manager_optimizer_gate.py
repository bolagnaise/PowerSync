"""Regression coverage for OB-35: the computed ``ml_optimization_enabled`` guard
in ``async_setup_entry`` must actually gate money-event manager creation.

Follows the AST source-extraction pattern from
``tests/test_sungrow_curtailment_runtime.py``: rather than hardcoding absolute
line numbers (which drift as `__init__.py` churns), the guarded ``if``
statements are located dynamically inside ``async_setup_entry`` by walking the
AST for the manager-creation call each one guards, then re-embedded verbatim
(original indentation restored from ``col_offset``) inside a stub
``async def _run(): ...`` and exec'd against a controlled namespace with the
four manager classes replaced by lightweight recorders.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _source_with_indent(source: str, node: ast.AST) -> str:
    """Verbatim source for `node`, with its first line's leading indentation
    restored (``ast.get_source_segment`` strips it since the segment starts
    at ``col_offset``); continuation lines already carry their original
    absolute indentation, so no dedent/re-indent step is needed."""
    segment = ast.get_source_segment(source, node)
    assert segment is not None, "empty source segment"
    return " " * node.col_offset + segment


def _test_references_name(test_node: ast.AST, name: str) -> bool:
    return any(
        isinstance(n, ast.Name) and n.id == name for n in ast.walk(test_node)
    )


def _body_calls(stmts, class_name: str) -> bool:
    for stmt in stmts:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == class_name:
                return True
    return False


def _find_async_setup_entry(module: ast.Module) -> ast.AsyncFunctionDef:
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            return node
    raise AssertionError("async_setup_entry not found")


def _find_guard_if(scope: ast.AST, class_name: str) -> ast.If:
    """Locate the single `if ... ml_optimization_enabled ...:` block whose
    body creates `class_name` — the guard site under test."""
    candidates = [
        n
        for n in ast.walk(scope)
        if isinstance(n, ast.If)
        and _test_references_name(n.test, "ml_optimization_enabled")
        and _body_calls(n.body, class_name)
    ]
    assert len(candidates) == 1, (
        f"expected exactly one ml_optimization_enabled-guarded if-block "
        f"creating {class_name}, found {len(candidates)}"
    )
    return candidates[0]


def _find_ml_optimization_enabled_assign(scope: ast.AST) -> ast.Assign:
    candidates = [
        n
        for n in ast.walk(scope)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "ml_optimization_enabled"
            for t in n.targets
        )
    ]
    assert len(candidates) == 1, (
        f"expected exactly one ml_optimization_enabled assignment, "
        f"found {len(candidates)}"
    )
    return candidates[0]


def _locate():
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    entry_fn = _find_async_setup_entry(module)

    assign_node = _find_ml_optimization_enabled_assign(entry_fn)
    tesla_aemo_if = _find_guard_if(entry_fn, "AEMOSpikeManager")
    generic_aemo_if = _find_guard_if(entry_fn, "GenericAEMOSpikeManager")
    tesla_saving_if = _find_guard_if(entry_fn, "SavingSessionTariffManager")

    return source, assign_node, tesla_aemo_if, generic_aemo_if, tesla_saving_if


(
    _SOURCE,
    _ASSIGN_NODE,
    _TESLA_AEMO_IF,
    _GENERIC_AEMO_IF,
    _TESLA_SAVING_IF,
) = _locate()

# ml_optimization_enabled computation + both AEMO spike manager creation
# sites (Tesla, then generic), located dynamically above.
AEMO_BLOCK = "\n".join(
    [
        _source_with_indent(_SOURCE, _ASSIGN_NODE),
        _source_with_indent(_SOURCE, _TESLA_AEMO_IF),
        _source_with_indent(_SOURCE, _GENERIC_AEMO_IF),
    ]
)
_AEMO_BASE_INDENT = " " * _ASSIGN_NODE.col_offset

# The Tesla saving-session `if` and its `elif` (non-Tesla generic manager)
# sibling are a single AST node (elif == If in `orelse`), so one source
# segment captures both branches.
SAVING_SESSION_BLOCK = _source_with_indent(_SOURCE, _TESLA_SAVING_IF)
_SAVING_SESSION_BASE_INDENT = " " * _TESLA_SAVING_IF.col_offset


CONF_OPTIMIZATION_PROVIDER = "optimization_provider"
CONF_OPTIMIZATION_ENABLED = "optimization_enabled"
CONF_AEMO_REGION = "aemo_region"
CONF_AEMO_SPIKE_THRESHOLD = "aemo_spike_threshold"
CONF_TESLA_ENERGY_SITE_ID = "tesla_energy_site_id"
OPT_PROVIDER_POWERSYNC = "powersync"
OPT_PROVIDER_NATIVE = "native"


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _make_recorder_class():
    """A fresh manager stand-in class that records every instantiation."""

    class _Recorder:
        created: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            type(self).created.append(self)

        async def restore_or_exit(self):
            """OB-41: async_setup_entry calls this right after construction
            to restore-or-exit a persisted event found on a reload; this
            stand-in has no real store, so it's a no-op."""
            pass

    return _Recorder


def _base_namespace(**overrides) -> dict:
    ns = dict(
        CONF_OPTIMIZATION_PROVIDER=CONF_OPTIMIZATION_PROVIDER,
        CONF_OPTIMIZATION_ENABLED=CONF_OPTIMIZATION_ENABLED,
        CONF_AEMO_REGION=CONF_AEMO_REGION,
        CONF_AEMO_SPIKE_THRESHOLD=CONF_AEMO_SPIKE_THRESHOLD,
        CONF_TESLA_ENERGY_SITE_ID=CONF_TESLA_ENERGY_SITE_ID,
        OPT_PROVIDER_POWERSYNC=OPT_PROVIDER_POWERSYNC,
        OPT_PROVIDER_NATIVE=OPT_PROVIDER_NATIVE,
        hass=object(),
        tesla_api_token="tok",
        tesla_api_provider="fleet",
        token_getter=lambda: "tok",
        _LOGGER=_Logger(),
        aemo_spike_enabled=True,
        has_tesla_site=False,
        is_sigenergy=False,
        is_sungrow=False,
        is_foxess=False,
        is_goodwe=False,
        is_alphaess=False,
        is_esy_sunhome=False,
        is_solax=False,
        is_saj_h2=False,
        is_fronius_reserva=False,
        is_neovolt=False,
        is_solaredge=False,
        is_anker_solix=False,
        entry=SimpleNamespace(
            options={CONF_AEMO_REGION: "NSW1", CONF_AEMO_SPIKE_THRESHOLD: 3000.0},
            data={CONF_TESLA_ENERGY_SITE_ID: "site-1"},
        ),
    )
    ns.update(overrides)
    return ns


def _with_optimizer(ns: dict, enabled: bool) -> dict:
    ns = dict(ns)
    options = dict(ns["entry"].options)
    if enabled:
        options[CONF_OPTIMIZATION_PROVIDER] = OPT_PROVIDER_POWERSYNC
        options[CONF_OPTIMIZATION_ENABLED] = True
    else:
        options[CONF_OPTIMIZATION_PROVIDER] = OPT_PROVIDER_NATIVE
    ns["entry"] = SimpleNamespace(options=options, data=dict(ns["entry"].data))
    return ns


def _run_aemo_block(ns: dict):
    aemo_spike_manager_cls = _make_recorder_class()
    generic_aemo_spike_manager_cls = _make_recorder_class()
    ns = dict(ns)
    ns["AEMOSpikeManager"] = aemo_spike_manager_cls
    ns["GenericAEMOSpikeManager"] = generic_aemo_spike_manager_cls

    src = (
        "async def _run():\n"
        + AEMO_BLOCK
        + "\n"
        + _AEMO_BASE_INDENT
        + "return locals()\n"
    )
    exec_ns: dict = {}
    exec(src, ns, exec_ns)
    asyncio.run(exec_ns["_run"]())
    return aemo_spike_manager_cls, generic_aemo_spike_manager_cls


def _run_saving_session_block(ns: dict):
    saving_session_tariff_manager_cls = _make_recorder_class()
    generic_saving_session_manager_cls = _make_recorder_class()
    ns = dict(ns)
    ns["SavingSessionTariffManager"] = saving_session_tariff_manager_cls
    ns["GenericSavingSessionManager"] = generic_saving_session_manager_cls
    ns["saving_session_coordinator"] = object()
    ns.setdefault("ss_octopoints_per_penny", 0)

    src = (
        "async def _run():\n"
        + SAVING_SESSION_BLOCK
        + "\n"
        + _SAVING_SESSION_BASE_INDENT
        + "return locals()\n"
    )
    exec_ns: dict = {}
    exec(src, ns, exec_ns)
    asyncio.run(exec_ns["_run"]())
    return saving_session_tariff_manager_cls, generic_saving_session_manager_cls


def test_tesla_aemo_spike_manager_gated_when_optimizer_active():
    ns = _base_namespace(has_tesla_site=True)

    tesla_cls, _ = _run_aemo_block(_with_optimizer(ns, enabled=False))
    assert len(tesla_cls.created) == 1, "manager should be created when optimizer is off"

    tesla_cls, _ = _run_aemo_block(_with_optimizer(ns, enabled=True))
    assert len(tesla_cls.created) == 0, (
        "Tesla AEMOSpikeManager must not double-control the battery "
        "alongside the LP optimizer"
    )


def test_generic_aemo_spike_manager_gated_when_optimizer_active():
    ns = _base_namespace(is_sungrow=True)

    _, generic_cls = _run_aemo_block(_with_optimizer(ns, enabled=False))
    assert len(generic_cls.created) == 1, "manager should be created when optimizer is off"

    _, generic_cls = _run_aemo_block(_with_optimizer(ns, enabled=True))
    assert len(generic_cls.created) == 0, (
        "GenericAEMOSpikeManager must not double-control the battery "
        "alongside the LP optimizer"
    )


def test_tesla_saving_session_manager_gated_when_optimizer_active():
    ns = _base_namespace(has_tesla_site=True)

    tesla_cls, _ = _run_saving_session_block(dict(ns, ml_optimization_enabled=False))
    assert len(tesla_cls.created) == 1, "manager should be created when optimizer is off"

    tesla_cls, _ = _run_saving_session_block(dict(ns, ml_optimization_enabled=True))
    assert len(tesla_cls.created) == 0, (
        "Tesla SavingSessionTariffManager must not double-control the battery "
        "alongside the LP optimizer"
    )


def test_generic_saving_session_manager_gated_when_optimizer_active():
    ns = _base_namespace(is_sungrow=True)

    _, generic_cls = _run_saving_session_block(dict(ns, ml_optimization_enabled=False))
    assert len(generic_cls.created) == 1, "manager should be created when optimizer is off"

    _, generic_cls = _run_saving_session_block(dict(ns, ml_optimization_enabled=True))
    assert len(generic_cls.created) == 0, (
        "GenericSavingSessionManager must not double-control the battery "
        "alongside the LP optimizer"
    )
