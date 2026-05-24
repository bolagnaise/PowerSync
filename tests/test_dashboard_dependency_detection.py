"""Regression tests for the generated Home Assistant dashboard."""

from __future__ import annotations

from pathlib import Path


STRATEGY_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "frontend"
    / "power-sync-strategy.js"
)
INIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)


def test_button_card_resource_fallback_accepts_dashed_hacs_url():
    """button-card HACS URLs include dashes and must not be normalized away."""
    source = STRATEGY_PATH.read_text()

    assert "c.element," in source
    assert "c.hacs," in source
    assert "c.element.replace(/-/g, '')" in source
    assert "c.hacs?.replace(/-/g, '')" in source
    assert "url.includes(name)" in source


def test_optimizer_windows_use_combined_visual_card():
    """Charge and discharge windows should render as one dashboard schedule card."""
    source = STRATEGY_PATH.read_text()

    assert "optimization_force_charge_windows" in source
    assert "optimization_force_discharge_windows" in source
    assert "Planned Battery Windows" in source
    assert "ps-window-row" in source
    assert "Future Force Charge" not in source


def test_dashboard_setup_preserves_user_managed_lovelace_layout():
    """Reloads must not overwrite a dashboard the user has edited manually."""
    source = INIT_PATH.read_text()

    assert "def _is_empty_lovelace_dashboard_config" in source
    assert "Initializing empty PowerSync dashboard with strategy mode" in source
    assert "already has a custom Lovelace layout; " in source
    assert "leaving it unchanged" in source
    assert "Migrating PowerSync dashboard to strategy mode" not in source


def test_dashboard_layout_storage_reconciles_card_changes():
    """Saved tile order should survive card additions/removals where possible."""
    source = STRATEGY_PATH.read_text()

    assert "_legacyCardKey(cardConfig, index)" in source
    assert "_cardKey(cardConfig, occurrence)" in source
    assert "legacyToCurrent" in source
    assert "missingItems" in source
    assert "layouts[layoutKey] = normalized" in source
    assert "return `${index}:${parts.join(':')}`" not in source


def test_dashboard_layout_drag_starts_from_handle_only():
    """Mobile scroll gestures should not be captured by the whole card."""
    source = STRATEGY_PATH.read_text()

    assert "const dragSurface = document.createElement('button');" in source
    assert "dragSurface.setAttribute('aria-label', 'Drag dashboard card')" in source
    assert "item.addEventListener('pointerdown'" not in source
    assert "item.addEventListener('pointermove'" not in source
    assert ".item.customizing {\n        cursor: default;" in source
    assert ".drag-surface {\n        display: none;" in source
    drag_surface_css = source[
        source.index(".drag-surface {"):source.index(".item.customizing .drag-surface")
    ]
    assert "touch-action: none;" in drag_surface_css
    assert "appearance: none;" in drag_surface_css
