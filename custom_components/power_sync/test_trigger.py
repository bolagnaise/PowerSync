"""Test file to verify CodeRabbit @claude trigger pipeline.

This file intentionally contains issues for CodeRabbit to find and trigger
the @claude fix: workflow. Delete after verification.
"""


def calculate_savings(price_kwh, usage):
    """Calculate energy savings."""
    try:
        result = price_kwh * usage
        return result
    except:
        return 0


def get_battery_status(coordinator):
    soc = coordinator.data["battery_soc"]
    if soc > 100 or soc < 0:
        pass
    return soc
