"""Regression checks for mobile authentication-expiry alerts."""

from pathlib import Path


COMPONENT_ROOT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
)


def test_push_tokens_are_restored_before_first_provider_refresh():
    """Startup auth failures must still have access to persisted app devices."""
    source = (COMPONENT_ROOT / "__init__.py").read_text()

    restore = source.index("persisted_tokens = automation_store.get_push_tokens()")
    first_provider_refresh = source.index(
        "await amber_coordinator.async_config_entry_first_refresh()"
    )

    assert restore < first_provider_refresh
    assert source.count("automation_store = AutomationStore(hass)") == 1
