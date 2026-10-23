"""Plain-language access guidance for the Home Assistant options dialog."""
from __future__ import annotations

from datetime import datetime, timezone


def optimizer_access_description(status: dict, pairing_code: str | None = None) -> dict[str, str]:
    """Describe verified access separately from pairing and optimiser operation."""
    state = status.get("access_state")
    summaries = {
        "paid": "Active HA optimiser subscription.",
        "sponsor": "Access through GitHub sponsorship.",
        "trial": "Seven-day HA subscription trial.",
        "billing_pending": "First subscription payment is being verified. Access is temporarily available.",
        "payment_grace": "Subscription renewal needs attention. Access is temporarily available.",
        "support": "Access provided by support.",
        "checking": "Access has not yet been verified.",
        "unpaired": "This HA server has not been paired with a PowerSync account.",
        "expired": "The last verified access has expired.",
        "subscription_required": "An HA optimiser subscription or qualifying GitHub sponsorship is required.",
        "installation_revoked": "This HA server's pairing has been revoked.",
    }
    summary = summaries.get(state, "Access could not be confirmed.")
    if state == "transition":
        deadline = status.get("legacy_access_until")
        until = (
            " until " + datetime.fromtimestamp(deadline, timezone.utc).strftime("%d %B %Y at %H:%M UTC")
            if deadline else ""
        )
        summary = (
            f"Free existing-user transition period{until}. "
            "This is separate from the seven-day trial offered with direct subscription signup."
        )

    paired = bool(status.get("paired"))
    account_url = status["account_url"]
    if pairing_code:
        next_step = (
            f"Sign in at {account_url} and approve this code: **{pairing_code}**. "
            "Then choose **Finish pairing** below and submit. The code is valid for ten minutes."
        )
    elif not paired:
        next_step = (
            "To link this HA server, choose **Start pairing** below and submit. "
            f"Approve the code at {account_url}, then return here and choose **Finish pairing**. "
            "Linking GitHub or Discord on the website does not complete HA server pairing."
        )
    elif status.get("recovery_pending"):
        next_step = (
            "Recovery is still pending. Choose **Refresh access** to check again. "
            "Resume will be available after recovery completes and access is available."
        )
    elif status.get("requires_resume"):
        if status.get("would_allow") or status.get("enforcement_mode") == "shadow":
            next_step = "Choose **Resume optimisation** below and submit to start a fresh schedule for this entry."
        else:
            next_step = (
                f"Check HA optimiser access at {account_url}, then choose **Refresh access**. "
                "Once access is restored, choose **Resume optimisation** to start a fresh schedule."
            )
    elif status.get("can_optimize"):
        next_step = (
            "Access is available. No Resume action is needed. You can close this window. "
            "Smart Optimization and Monitoring Mode settings still determine whether automatic control runs."
        )
    else:
        next_step = (
            f"Check HA optimiser access at {account_url}, then choose **Refresh access** below."
        )

    return {
        "access_summary": summary,
        "pairing_status": "Paired with your PowerSync account." if paired else "Not paired with a PowerSync account.",
        "next_step": next_step,
    }


def optimizer_settings_description(
    status: dict | None, *, enabled: bool, running: bool
) -> str:
    """Distinguish the saved enable setting from live optimiser admission."""
    if not enabled:
        return "Smart Optimization is off in saved settings."
    if status is None:
        return "Saved setting is on. Check the Enable Smart Optimization switch for live operation."
    if status.get("recovery_pending"):
        return (
            "Saved setting is on, but optimisation is paused while owned controls are "
            "being restored. Open HA optimiser access to check recovery before resuming."
        )
    if status.get("requires_resume"):
        return (
            "Saved setting is on, but optimisation is paused after an access interruption. "
            "Open HA optimiser access, choose Resume optimisation, and submit to start a fresh schedule."
        )
    if not status.get("can_optimize"):
        return (
            "Saved setting is on, but HA optimiser access is unavailable. "
            "Open HA optimiser access to check pairing and access."
        )
    if running:
        return "Smart Optimization is running."
    return (
        "Saved setting is on, but the optimiser is not running yet. "
        "Check the Enable Smart Optimization switch and HA optimiser access."
    )
