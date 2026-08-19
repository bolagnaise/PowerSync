"""Regression tests for unfinished Tesla restores surviving a restart.

When a restore_normal's Tesla writes fail, the handler schedules an in-process
retry 60 seconds out. That timer dies with the process, and its callback also
self-skips once force state has been cleared — so on a restart-triggered
restore the retry could never run and the failure was abandoned silently,
leaving the hardware half-restored with nothing tracking it.

Observed live on 2026-08-19: a HACS update restarted HA mid force-charge, the
grid-charging restore failed its readback, "retry 1 scheduled in 60 seconds"
was logged, and nothing ever completed it.

Follows the AST/source-extraction pattern used by tests/test_force_mode_controls.py
and tests/test_hold_soc_persistence.py.
"""

from __future__ import annotations

import ast
import asyncio
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _find_function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _function_source(name: str) -> str:
    source = INIT_PATH.read_text()
    node = _find_function(ast.parse(source), name)
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    return segment


class _Store:
    """Minimal Store double recording what was written."""

    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})
        self.saves = 0

    async def async_load(self):
        return dict(self.data)

    async def async_save(self, data):
        self.data = dict(data)
        self.saves += 1


def _build_persist_helper(store: _Store, entry_data: dict):
    """exec persist_pending_tesla_restore with its free variables injected."""
    namespace: dict = {
        "store": store,
        "hass": SimpleNamespace(data={"power_sync": {"entry-1": entry_data}}),
        "entry": SimpleNamespace(entry_id="entry-1"),
        "DOMAIN": "power_sync",
        "dt_util": SimpleNamespace(
            utcnow=lambda: datetime(2026, 8, 19, 2, 28, tzinfo=timezone.utc)
        ),
        "_LOGGER": SimpleNamespace(
            warning=lambda *a, **k: None,
            debug=lambda *a, **k: None,
        ),
    }
    exec(textwrap.dedent(_function_source("persist_pending_tesla_restore")), namespace)
    return namespace["persist_pending_tesla_restore"]


# ---------------------------------------------------------------------------
# Persisting and clearing the marker
# ---------------------------------------------------------------------------


def test_failed_restore_is_recorded_for_the_next_startup():
    store = _Store()
    entry_data: dict = {}
    persist = _build_persist_helper(store, entry_data)

    asyncio.run(persist("one or more Tesla restore writes failed"))

    marker = store.data["pending_tesla_restore"]
    assert marker["reason"] == "one or more Tesla restore writes failed"
    assert marker["recorded_at"].startswith("2026-08-19")
    # Also mirrored into hass.data so a same-process reload sees it.
    assert entry_data["pending_tesla_restore"] == marker


def test_marker_carries_the_backup_reserve_skip_flag():
    store = _Store()
    persist = _build_persist_helper(store, {})

    asyncio.run(persist("reserve write failed", skip_backup_reserve_restore=True))

    assert store.data["pending_tesla_restore"]["_skip_backup_reserve_restore"] is True


def test_successful_restore_clears_the_marker():
    store = _Store({"pending_tesla_restore": {"reason": "stale"}})
    entry_data = {"pending_tesla_restore": {"reason": "stale"}}
    persist = _build_persist_helper(store, entry_data)

    asyncio.run(persist(None))

    assert store.data["pending_tesla_restore"] is None
    assert entry_data["pending_tesla_restore"] is None


# ---------------------------------------------------------------------------
# Structural guarantees in the handler and the startup path
# ---------------------------------------------------------------------------


def test_restore_failure_persists_before_relying_on_the_retry_timer():
    """The marker must not be conditional on the in-process retry surviving."""
    source = _function_source("handle_restore_normal")

    failure_branch = source[source.index("if tesla_restore_failed:"):]
    persist_at = failure_branch.index("persist_pending_tesla_restore")
    retry_at = failure_branch.index("_schedule_tesla_restore_retry")
    # Persisted first, and outside the `if _schedule...` conditional.
    assert persist_at < retry_at


def test_completed_restore_clears_the_marker():
    source = _function_source("handle_restore_normal")

    # The Tesla success line carries no brand prefix, unlike the per-brand
    # paths above it (Sigenergy, FoxESS, ...) which never set this marker.
    tesla_success = '_LOGGER.info("NORMAL OPERATION RESTORED")'
    assert "await persist_pending_tesla_restore(None)" in source
    assert source.index("await persist_pending_tesla_restore(None)") < source.index(
        tesla_success
    )


def test_only_the_tesla_path_owns_the_marker():
    """Per-brand restores never set the marker, so they must not clear it."""
    source = _function_source("handle_restore_normal")

    assert source.count("persist_pending_tesla_restore") == 2  # one set, one clear


def test_startup_finishes_an_unfinished_restore_without_force_state():
    """The hole: force_mode_state null but a restore still owed."""
    source = _function_source("restore_force_mode_from_persistence")

    guard = source[: source.index("return")]
    assert "if not persisted_force_state:" in guard
    assert "persisted_pending_restore" in guard
    assert "SERVICE_RESTORE_NORMAL" in source
    assert "startup_pending_restore" in source


def test_startup_marker_is_loaded_from_storage():
    source = INIT_PATH.read_text()

    assert 'stored_data.get("pending_tesla_restore")' in source
    assert '"pending_tesla_restore": pending_tesla_restore,' in source
    assert 'hass.data[DOMAIN][entry.entry_id].get("pending_tesla_restore")' in source
