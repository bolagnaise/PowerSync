"""Regression test for OB-9: Modbus brand restore branches in
``handle_restore_normal`` must re-check ``_restore_superseded(...)`` after
their ``coord.restore_normal()`` await, exactly like the Tesla branch does.

Without the re-check, a force command that interleaves during the brand's
``await coord.restore_normal()`` call can be clobbered: the resuming restore
unconditionally clears ``force_charge_state`` / ``force_discharge_state``
(and, for Sigenergy, the saved-tariff/reserve fields too), wiping out the
freshly-set ``active`` flag from the newer force command. The new force
command then runs with no software auto-restore armed.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _find_function(tree: ast.AST, function_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return node
    raise AssertionError(f"{function_name} not found")


def _find_try_containing(source: str, function: ast.AST, marker: str) -> str:
    """Return the source of the single ``try:`` block inside ``function``
    whose body contains ``marker`` (e.g. a brand-specific coordinator call).
    """
    matches = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Try):
            continue
        try_source = ast.get_source_segment(source, node)
        if try_source and marker in try_source:
            matches.append(try_source)
    assert len(matches) == 1, (
        f"expected exactly one try-block containing {marker!r}, found {len(matches)}"
    )
    return matches[0]


# Each Modbus/local brand branch named in OB-9: the marker that pins down its
# try-block, the await call that hits hardware, the expected
# ``_restore_superseded`` stage label the branch must use, and the
# force-state clear line that must come *after* the guard.
BRAND_CASES = [
    (
        "Sigenergy",
        "await controller.restore_normal(",
        "await controller.disconnect()",
        '_restore_superseded("Sigenergy restore")',
        'force_discharge_state["active"] = False',
    ),
    (
        "FoxESS",
        "await foxess_coord.restore_normal()",
        "await foxess_coord.restore_normal()",
        '_restore_superseded("FoxESS restore")',
        'force_charge_state["active"] = False',
    ),
    (
        "GoodWe",
        "await goodwe_coord.restore_normal()",
        "await goodwe_coord.restore_normal()",
        '_restore_superseded("GoodWe restore")',
        'force_charge_state["active"] = False',
    ),
    (
        "AlphaESS",
        "await alphaess_coord.restore_normal()",
        "await alphaess_coord.restore_normal()",
        '_restore_superseded("AlphaESS restore")',
        'force_charge_state["active"] = False',
    ),
    (
        "ESY Sunhome",
        "await esy_coord.restore_normal()",
        "await esy_coord.restore_normal()",
        '_restore_superseded("ESY Sunhome restore")',
        'force_charge_state["active"] = False',
    ),
    (
        "SolarEdge",
        "await solaredge_coord.restore_normal()",
        "await solaredge_coord.restore_normal()",
        '_restore_superseded("SolarEdge restore")',
        'force_charge_state["active"] = False',
    ),
    (
        "Anker Solix",
        "await anker_coord.restore_normal()",
        "await anker_coord.restore_normal()",
        '_restore_superseded("Anker Solix restore")',
        'force_charge_state["active"] = False',
    ),
    (
        "Sungrow",
        "await sungrow_coord.restore_normal()",
        "await sungrow_coord.restore_normal()",
        '_restore_superseded("Sungrow restore")',
        'force_charge_state["active"] = False',
    ),
    (
        "Solax",
        "await solax_coord.restore_normal()",
        "await solax_coord.restore_normal()",
        '_restore_superseded("Solax restore")',
        'force_charge_state["active"] = False',
    ),
    (
        "SAJ H2",
        "await saj_coord.restore_normal()",
        "await saj_coord.restore_normal()",
        '_restore_superseded("SAJ H2 restore")',
        'force_charge_state["active"] = False',
    ),
    (
        "Fronius Reserva",
        "await fronius_coord.restore_normal()",
        "await fronius_coord.restore_normal()",
        '_restore_superseded("Fronius Reserva restore")',
        'force_charge_state["active"] = False',
    ),
    (
        "Neovolt",
        "await neovolt_coord.restore_normal()",
        "await neovolt_coord.restore_normal()",
        '_restore_superseded("Neovolt restore")',
        'force_charge_state["active"] = False',
    ),
]


def test_tesla_branch_still_has_restore_superseded_baseline():
    """Sanity: confirm the Tesla branch's existing guard pattern (the one the
    Modbus branches must mirror) is present, so this test's premise holds.
    """
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    assert "def _restore_superseded(stage: str) -> bool:" in function_source
    assert '_command_generation[0] += 1' in function_source
    assert '_restore_generation = _command_generation[0]' in function_source
    assert 'if _restore_superseded("initial mode handoff"):' in function_source
    assert 'if _restore_superseded("tariff restore"):' in function_source
    assert 'if _restore_superseded("mode/reserve restore"):' in function_source
    assert 'if _restore_superseded("grid charging restore"):' in function_source


def test_modbus_brand_branches_recheck_restore_superseded_before_clearing_state():
    """Each of the 12 Modbus/local brand branches must call
    ``_restore_superseded(...)`` after their hardware-restore await and
    before unconditionally clearing force_*_state, mirroring the Tesla
    branch. This is the actual OB-9 regression check.

    The original OB-9 registry undercounted the branch set at 8; there are
    12 (Solax, SAJ H2, Fronius Reserva and Neovolt share the identical
    unguarded-clear shape). Guard the count so a future branch added to
    handle_restore_normal without a matching BRAND_CASES entry is caught.
    """
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")

    assert len(BRAND_CASES) == 12, (
        "expected 12 Modbus/local brand restore branches in handle_restore_normal"
    )

    for brand, call_marker, await_marker, guard, clear_marker in BRAND_CASES:
        try_source = _find_try_containing(source, function, call_marker)

        assert guard in try_source, (
            f"{brand} restore branch is missing the _restore_superseded "
            f"re-check ({guard!r}) between its hardware restore await and "
            f"its force_*_state clear"
        )
        assert clear_marker in try_source, (
            f"{brand} restore branch no longer clears {clear_marker!r} — "
            "test fixture assumption is stale"
        )

        await_index = try_source.index(await_marker)
        guard_index = try_source.index(guard)
        clear_index = try_source.index(clear_marker)

        assert await_index < guard_index, (
            f"{brand}: _restore_superseded re-check must come after the "
            "hardware restore await"
        )
        assert guard_index < clear_index, (
            f"{brand}: _restore_superseded re-check must come before the "
            "unconditional force_*_state clear, otherwise an interleaved "
            "newer force command gets clobbered"
        )


def test_modbus_brand_branch_guards_use_the_shared_restore_superseded_helper():
    """The brand branches must reuse the *same* closure-scoped helper the
    Tesla branch uses (same generation snapshot captured once at the top of
    handle_restore_normal), not a fresh/parallel implementation.
    """
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")
    function_source = ast.get_source_segment(source, function)

    assert function_source is not None
    helper_def_index = function_source.index("def _restore_superseded(stage: str) -> bool:")

    for brand, call_marker, _await_marker, guard, _clear_marker in BRAND_CASES:
        # Every brand-branch call to _restore_superseded must appear textually
        # after the single helper definition (i.e. it's the same closure, not
        # a locally redefined helper).
        guard_index = function_source.index(guard)
        assert helper_def_index < guard_index, (
            f"{brand}: _restore_superseded call must reference the helper "
            "defined once near the top of handle_restore_normal"
        )
        # There must be exactly one occurrence of this brand's guard string —
        # a duplicate would indicate a stray/duplicated re-check.
        assert function_source.count(guard) == 1, (
            f"{brand}: expected exactly one _restore_superseded re-check, "
            f"found {function_source.count(guard)}"
        )


def _enclosing_try(function: ast.AST, target_node: ast.AST) -> ast.Try:
    """Return the innermost ``ast.Try`` whose body/handlers contain
    ``target_node``.
    """
    best: ast.Try | None = None
    best_span = None
    for node in ast.walk(function):
        if not isinstance(node, ast.Try):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", None)
        if end is None:
            continue
        if start <= target_node.lineno <= end:
            span = end - start
            if best_span is None or span < best_span:
                best = node
                best_span = span
    assert best is not None, "no enclosing try-block found"
    return best


def test_every_restore_normal_await_branch_is_guarded_structurally():
    """Structural guard against the original registry undercount: walk the AST
    of ``handle_restore_normal``, find EVERY ``await <coord>.restore_normal()``
    (the shape shared by all brand branches), and assert each one's enclosing
    ``try`` block contains a ``_restore_superseded`` re-check that precedes the
    force-state clear.

    Unlike the marker-based cases above, this discovers branches automatically,
    so a 13th brand added later without a guard fails here even if nobody
    remembers to extend ``BRAND_CASES``.
    """
    source = INIT_PATH.read_text()
    tree = ast.parse(source)
    function = _find_function(tree, "handle_restore_normal")

    restore_awaits = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "restore_normal"
    ]

    # 12 brand branches: Sigenergy, FoxESS, GoodWe, AlphaESS, ESY Sunhome,
    # Solax, SAJ H2, Fronius Reserva, Neovolt, SolarEdge, Anker Solix, Sungrow.
    assert len(restore_awaits) == len(BRAND_CASES) == 12, (
        f"expected 12 restore_normal awaits (one per brand branch), found "
        f"{len(restore_awaits)}; if a brand branch was added/removed, update "
        "BRAND_CASES and add its _restore_superseded guard"
    )

    for await_node in restore_awaits:
        obj = ast.get_source_segment(source, await_node.value.func.value)
        try_block = _enclosing_try(function, await_node)
        try_source = ast.get_source_segment(source, try_block)
        assert try_source is not None

        assert "_restore_superseded(" in try_source, (
            f"restore_normal branch (await {obj}.restore_normal()) is missing a "
            "_restore_superseded re-check in its try-block — OB-9 gap"
        )
        guard_index = try_source.index("_restore_superseded(")
        clear_index = try_source.index('_state["active"] = False')
        assert guard_index < clear_index, (
            f"restore_normal branch (await {obj}.restore_normal()) clears force "
            "state before its _restore_superseded re-check — OB-9 gap"
        )
