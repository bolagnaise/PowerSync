"""Australian retailer directory and AER Product Reference Data plan lookup.

The AER publishes every retailer's generally available electricity plans
through the Consumer Data Right Product Reference Data (PRD) API used by Energy
Made Easy and Victorian Energy Compare. Those endpoints are public and need no
customer consent. A customer's own tariff is not available without CDR
accreditation, so PowerSync only offers published plans as a starting point
that the owner reviews before saving.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

PRD_BASE_URL = "https://cdr.energymadeeasy.gov.au"
PRD_PAGE_SIZE = 1000
PRD_MAX_PAGES = 20
PRD_TIMEOUT_SECONDS = 15

# NEM regions PowerSync can price with a published retail plan. Evoenergy
# (ACT) is part of the NSW1 region.
PLAN_REGIONS: dict[str, str] = {
    "NSW1": "New South Wales / ACT",
    "QLD1": "Queensland",
    "SA1": "South Australia",
    "VIC1": "Victoria",
    "TAS1": "Tasmania",
}

ELECTRICITY_NETWORKS: dict[str, tuple[str, ...]] = {
    "NSW1": ("Ausgrid", "Endeavour Energy", "Essential Energy", "Evoenergy"),
    "QLD1": ("Energex", "Ergon"),
    "SA1": ("SA Power Networks",),
    "VIC1": ("AusNet", "CitiPower", "Jemena", "Powercor", "United Energy"),
    "TAS1": ("TasNetworks",),
}

PLAN_REGION_TIMEZONES: dict[str, str] = {
    "NSW1": "Australia/Sydney",
    "QLD1": "Australia/Brisbane",
    "SA1": "Australia/Adelaide",
    "VIC1": "Australia/Melbourne",
    "TAS1": "Australia/Hobart",
}

# Regions whose local clock moves for daylight saving. Plans that publish AEST
# times differ from the local clock there for part of the year.
DAYLIGHT_SAVING_REGIONS = frozenset({"NSW1", "SA1", "VIC1", "TAS1"})


@dataclass(frozen=True)
class RetailerEntry:
    """A retailer an owner can pick.

    ``prd`` is the verified PRD brand slug (``None`` when the retailer
    publishes no residential plans there). ``provider`` names the dedicated
    PowerSync electricity provider for retailers with live pricing or a
    reviewed plan model of their own.
    """

    id: str
    label: str
    prd: str | None
    provider: str | None = None


ELECTRICITY_PROVIDER_DIRECTORY: tuple[RetailerEntry, ...] = (
    RetailerEntry("agl", "AGL", "agl", "agl"),
    RetailerEntry("actewagl", "ActewAGL", "actewagl"),
    RetailerEntry("alinta", "Alinta Energy", "alinta"),
    RetailerEntry("amber", "Amber Electric", "amber", "amber"),
    RetailerEntry("arc", "Arc Energy", "arc-energy"),
    RetailerEntry("aurora", "Aurora Energy", "aurora"),
    RetailerEntry("blue_nrg", "Blue NRG", "blue-nrg"),
    RetailerEntry("covau", "CovaU", "covau", "covau"),
    RetailerEntry("diamond", "Diamond Energy", "diamond"),
    RetailerEntry("dodo", "Dodo", "dodo"),
    RetailerEntry("energy_locals", "Energy Locals / 1st Energy", "energy-locals"),
    RetailerEntry("energyaustralia", "EnergyAustralia", "energyaustralia"),
    RetailerEntry("engie", "ENGIE", "engie"),
    RetailerEntry("ergon", "Ergon Energy", "ergon"),
    RetailerEntry("first_energy", "1st Energy", "1st-energy"),
    RetailerEntry("flow_power", "Flow Power", "flow-power", "flow_power"),
    RetailerEntry("globird", "GloBird Energy", "globird", "globird"),
    RetailerEntry("io_energy", "iO Energy", "radian"),
    RetailerEntry("kogan", "Kogan Energy", "kogan"),
    RetailerEntry("localvolts", "Localvolts", None, "localvolts"),
    RetailerEntry("lumo", "Lumo Energy", "lumo"),
    RetailerEntry("momentum", "Momentum Energy", "momentum"),
    RetailerEntry("nectr", "Nectr", "nectr"),
    RetailerEntry("origin", "Origin Energy", "origin"),
    RetailerEntry("ovo", "OVO Energy", "ovo-energy"),
    RetailerEntry("powershop", "Powershop", "powershop"),
    RetailerEntry("red_energy", "Red Energy", "red-energy"),
    RetailerEntry("sumo", "Sumo", "sumo-power"),
    RetailerEntry("tango", "Tango Energy", "tango"),
    RetailerEntry("other", "Another retailer / outside Australia", None),
)

_PLAN_ID = re.compile(r"^[A-Z0-9_-]+@(EME|VEC)$", re.IGNORECASE)


def retailer_entry(retailer_id: str | None) -> RetailerEntry | None:
    """Return a directory entry by id."""
    return next(
        (entry for entry in ELECTRICITY_PROVIDER_DIRECTORY if entry.id == retailer_id),
        None,
    )


def network_option_value(region: str, network: str) -> str:
    """Encode a region/network pair for a single selector value."""
    return f"{region}|{network}"


def parse_network_option(value: Any) -> tuple[str, str] | None:
    """Decode and validate a region/network selector value."""
    if not isinstance(value, str) or "|" not in value:
        return None
    region, network = value.split("|", 1)
    if network not in ELECTRICITY_NETWORKS.get(region, ()):
        return None
    return region, network


def prd_network_matches(distributor: str, network: str) -> bool:
    """Match PRD distributor names, which vary ("Citipower", "AusNet Services")."""

    def key(value: str) -> str:
        words = re.sub(r"[^a-z ]", " ", str(value).lower()).split()
        return words[0] if words else ""

    return bool(key(network)) and key(distributor) == key(network)


def valid_prd_plan_id(plan_id: Any) -> bool:
    """Return whether a value looks like a PRD plan identifier."""
    return (
        isinstance(plan_id, str)
        and len(plan_id) <= 100
        and _PLAN_ID.match(plan_id) is not None
    )


def summarise_prd_plans(plans: list[Any]) -> list[dict[str, Any]]:
    """Keep residential electricity plans and the fields the picker needs."""
    result: list[dict[str, Any]] = []
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        if plan.get("customerType") != "RESIDENTIAL" or plan.get("fuelType") != "ELECTRICITY":
            continue
        geography = plan.get("geography")
        distributors = geography.get("distributors") if isinstance(geography, dict) else None
        result.append(
            {
                "planId": plan.get("planId"),
                "label": plan.get("displayName"),
                "effectiveFrom": plan.get("effectiveFrom"),
                "networks": distributors if isinstance(distributors, list) else [],
                **({"standing": True} if plan.get("type") == "STANDING" else {}),
            }
        )
    return result


def public_plans_for_network(
    plans: list[dict[str, Any]], network: str
) -> list[dict[str, Any]]:
    """Residential plans for one network: market offers first, newest first."""
    matching = []
    for plan in plans:
        networks = plan.get("networks")
        if not isinstance(networks, list) or not any(
            isinstance(name, str) and prd_network_matches(name, network)
            for name in networks
        ):
            continue
        plan_id = str(plan.get("planId"))
        if not valid_prd_plan_id(plan_id):
            continue
        effective = plan.get("effectiveFrom")
        matching.append(
            {
                "planId": plan_id,
                "label": str(plan.get("label") or plan_id),
                "effectiveFrom": effective[:10] if isinstance(effective, str) else None,
                "standing": plan.get("standing") is True,
            }
        )
    # Stable sorts, least significant key first.
    matching.sort(key=lambda plan: plan["effectiveFrom"] or "", reverse=True)
    matching.sort(key=lambda plan: plan["label"])
    matching.sort(key=lambda plan: plan["standing"])
    return matching


class PrdError(Exception):
    """A published-plan request failed or returned an unexpected body."""


async def async_fetch_prd_plans(session: Any, slug: str) -> list[dict[str, Any]]:
    """Fetch every residential electricity plan a retailer publishes."""
    import aiohttp

    plans: list[dict[str, Any]] = []
    total_pages = 1
    page = 1
    while page <= total_pages:
        url = (
            f"{PRD_BASE_URL}/{quote(slug, safe='-')}/cds-au/v1/energy/plans"
            f"?fuelType=ELECTRICITY&page-size={PRD_PAGE_SIZE}&page={page}"
        )
        try:
            async with session.get(
                url,
                headers={"x-v": "1", "Accept": "application/json"},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=PRD_TIMEOUT_SECONDS),
            ) as response:
                # Redirects are not followed; any non-2xx, 3xx included, fails.
                if not 200 <= response.status < 300:
                    raise PrdError(f"prd_http_{response.status}")
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise PrdError("prd_unavailable") from err
        data = body.get("data") if isinstance(body, dict) else None
        meta = body.get("meta") if isinstance(body, dict) else None
        page_plans = data.get("plans") if isinstance(data, dict) else None
        pages = meta.get("totalPages") if isinstance(meta, dict) else None
        if (
            not isinstance(page_plans, list)
            or not isinstance(pages, int)
            or isinstance(pages, bool)
            or not 0 <= pages <= PRD_MAX_PAGES
        ):
            raise PrdError("prd_response_invalid")
        total_pages = pages
        plans.extend(summarise_prd_plans(page_plans))
        page += 1
    return plans


async def async_fetch_prd_plan(session: Any, slug: str, plan_id: str) -> dict[str, Any]:
    """Fetch one published plan's full electricity contract."""
    import aiohttp

    if not valid_prd_plan_id(plan_id):
        raise PrdError("invalid_prd_plan")
    url = (
        f"{PRD_BASE_URL}/{quote(slug, safe='-')}/cds-au/v1/energy/plans/"
        f"{quote(plan_id, safe='')}"
    )
    try:
        async with session.get(
            url,
            headers={"x-v": "3", "Accept": "application/json"},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=PRD_TIMEOUT_SECONDS),
        ) as response:
            if not 200 <= response.status < 300:
                raise PrdError(f"prd_http_{response.status}")
            body = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise PrdError("prd_unavailable") from err
    plan = body.get("data") if isinstance(body, dict) else None
    if not isinstance(plan, dict) or plan.get("planId") != plan_id:
        raise PrdError("prd_plan_identity_mismatch")
    return plan
