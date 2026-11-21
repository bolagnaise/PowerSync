"""Retail plan selector and allowance steps for the setup and options flows.

Setup: retailer -> state and network -> plan source -> (published AER plan |
PowerSync preset | manual) -> custom tariff -> period table -> allowance.
Options: the same, after a first choice to keep and edit the current rates.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .aer_plans import (
    PrdError,
    async_fetch_prd_plan,
    async_fetch_prd_plans,
    parse_network_option,
    public_plans_for_network,
    retailer_entry,
)
from .aer_tariff_import import (
    PrdImportError,
    import_prd_electricity_plan,
    schedule_to_tariff_draft,
)
from .retail_plan_flow import (
    MANUAL_PLAN_CHOICE,
    SOURCE_CHOOSE,
    SOURCE_EDIT,
    SOURCE_PRESET,
    SOURCE_PUBLISHED,
    apply_draft,
    build_import_quota,
    clear_draft,
    dedicated_provider_hint,
    half_hour_options,
    network_options,
    preset_options,
    quota_effective_price,
    quota_window,
    render_period_overview,
    retailer_options,
    source_options,
    template_to_draft,
    warnings_text,
)

_LOGGER = logging.getLogger(__name__)

CONF_RETAIL_PLAN_RETAILER = "retail_plan_retailer"
CONF_RETAIL_PLAN_NETWORK = "retail_plan_network"
CONF_RETAIL_PLAN_SOURCE = "retail_plan_source"
CONF_RETAIL_PLAN_ID = "retail_plan_id"
CONF_RETAIL_PLAN_PRESET = "retail_plan_preset"
CONF_ALLOWANCE_KWH = "allowance_kwh"
CONF_ALLOWANCE_START = "allowance_start"
CONF_ALLOWANCE_END = "allowance_end"
CONF_ALLOWANCE_PRICE = "allowance_price"
MAX_PLAN_OPTIONS = 300


def _select(options: list[tuple[str, str]], *, dropdown: bool = True) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=value, label=label) for value, label in options],
            mode=SelectSelectorMode.DROPDOWN if dropdown else SelectSelectorMode.LIST,
        )
    )


class _RetailPlanSteps:
    """Shared step bodies. Subclasses set the step suffix and next steps."""

    _retail_plan_suffix = ""

    def _rp_step_id(self, name: str) -> str:
        return f"{name}{self._retail_plan_suffix}"

    async def _rp_next(self, name: str) -> Any:
        return await getattr(self, f"async_step_{self._rp_step_id(name)}")()

    async def _rp_continue_to_tariff(self) -> Any:
        raise NotImplementedError

    def _rp_unit(self, kind: str = "minor_rate") -> str:
        selector_unit = getattr(self, "_selector_unit", None)
        return selector_unit(kind) if callable(selector_unit) else "c/kWh"

    async def _rp_retailer(self, user_input: dict[str, Any] | None) -> Any:
        if user_input is not None:
            entry = retailer_entry(user_input.get(CONF_RETAIL_PLAN_RETAILER))
            self._retail_plan_retailer = entry.id if entry else "other"
            if entry is None or entry.id == "other":
                clear_draft(self)
                return await self._rp_continue_to_tariff()
            return await self._rp_next("retail_plan_network")
        return self.async_show_form(
            step_id=self._rp_step_id("retail_plan_retailer"),
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_RETAIL_PLAN_RETAILER,
                        default=getattr(self, "_retail_plan_retailer", None) or "agl",
                    ): _select(retailer_options()),
                }
            ),
        )

    async def _rp_network(self, user_input: dict[str, Any] | None) -> Any:
        errors: dict[str, str] = {}
        if user_input is not None:
            parsed = parse_network_option(user_input.get(CONF_RETAIL_PLAN_NETWORK))
            if parsed is None:
                errors["base"] = "network_required"
            else:
                self._retail_plan_region, self._retail_plan_network = parsed
                return await self._rp_next("retail_plan_source")
        default = None
        if getattr(self, "_retail_plan_region", None) and getattr(self, "_retail_plan_network", None):
            default = f"{self._retail_plan_region}|{self._retail_plan_network}"
        field = (
            vol.Required(CONF_RETAIL_PLAN_NETWORK, default=default)
            if default
            else vol.Required(CONF_RETAIL_PLAN_NETWORK)
        )
        return self.async_show_form(
            step_id=self._rp_step_id("retail_plan_network"),
            data_schema=vol.Schema({field: _select(network_options())}),
            errors=errors,
        )

    async def _rp_source(self, user_input: dict[str, Any] | None) -> Any:
        entry = retailer_entry(getattr(self, "_retail_plan_retailer", None))
        presets = preset_options(entry.label if entry else None)
        options = source_options(entry, bool(presets))
        if user_input is not None:
            source = user_input.get(CONF_RETAIL_PLAN_SOURCE)
            if source == SOURCE_PUBLISHED and entry is not None and entry.prd:
                return await self._rp_next("retail_plan_published")
            if source == SOURCE_PRESET and presets:
                return await self._rp_next("retail_plan_preset")
            clear_draft(self)
            self._tariff_utility = entry.label if entry and entry.id != "other" else None
            return await self._rp_continue_to_tariff()
        return self.async_show_form(
            step_id=self._rp_step_id("retail_plan_source"),
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_RETAIL_PLAN_SOURCE, default=options[0][0]): _select(
                        options, dropdown=False
                    )
                }
            ),
            description_placeholders={
                "retailer": entry.label if entry else "Your retailer",
                "network": getattr(self, "_retail_plan_network", "") or "",
                "provider_hint": dedicated_provider_hint(entry),
            },
        )

    async def _rp_published(self, user_input: dict[str, Any] | None) -> Any:
        errors: dict[str, str] = {}
        entry = retailer_entry(getattr(self, "_retail_plan_retailer", None))
        network = getattr(self, "_retail_plan_network", None)
        if entry is None or not entry.prd or not network:
            return await self._rp_next("retail_plan_source")
        session = async_get_clientsession(self.hass)
        cache_key = (entry.prd, network)
        cache = getattr(self, "_retail_plan_cache", None) or {}
        plans = cache.get(cache_key)

        if user_input is not None:
            choice = user_input.get(CONF_RETAIL_PLAN_ID)
            if choice == MANUAL_PLAN_CHOICE or not choice:
                clear_draft(self)
                self._tariff_utility = entry.label
                return await self._rp_continue_to_tariff()
            allowed = {plan["planId"] for plan in plans or []}
            if choice not in allowed:
                errors["base"] = "prd_plan_unknown"
            else:
                try:
                    detail = await async_fetch_prd_plan(session, entry.prd, choice)
                    imported = import_prd_electricity_plan(detail)
                    draft = schedule_to_tariff_draft(
                        imported,
                        utility=entry.label,
                        region=getattr(self, "_retail_plan_region", None),
                        network=network,
                        retailer=entry.id,
                    )
                except PrdError as err:
                    _LOGGER.warning("Published plan %s unavailable: %s", choice, err)
                    errors["base"] = "prd_unavailable"
                except PrdImportError as err:
                    _LOGGER.warning("Published plan %s could not be imported: %s", choice, err)
                    errors["base"] = "prd_import_failed"
                else:
                    apply_draft(self, draft)
                    return await self._rp_continue_to_tariff()

        if plans is None:
            try:
                plans = public_plans_for_network(await async_fetch_prd_plans(session, entry.prd), network)
            except PrdError as err:
                _LOGGER.warning("Published plans for %s unavailable: %s", entry.prd, err)
                errors["base"] = "prd_unavailable"
                plans = []
            else:
                cache[cache_key] = plans
                self._retail_plan_cache = cache
        if not plans and not errors:
            errors["base"] = "prd_no_plans"
        options = [
            (
                plan["planId"],
                plan["label"]
                + (f" (from {plan['effectiveFrom']})" if plan.get("effectiveFrom") else "")
                + (" — standing offer" if plan.get("standing") else ""),
            )
            for plan in plans[:MAX_PLAN_OPTIONS]
        ]
        options.append((MANUAL_PLAN_CHOICE, "None of these — enter rates manually"))
        return self.async_show_form(
            step_id=self._rp_step_id("retail_plan_published"),
            data_schema=vol.Schema(
                {vol.Required(CONF_RETAIL_PLAN_ID, default=options[0][0]): _select(options)}
            ),
            errors=errors,
            description_placeholders={
                "retailer": entry.label,
                "network": network,
                "count": str(len(plans)),
            },
        )

    async def _rp_preset(self, user_input: dict[str, Any] | None) -> Any:
        from .tariff_templates import get_template

        entry = retailer_entry(getattr(self, "_retail_plan_retailer", None))
        options = preset_options(entry.label if entry else None)
        if not options:
            return await self._rp_next("retail_plan_source")
        if user_input is not None:
            template = get_template(str(user_input.get(CONF_RETAIL_PLAN_PRESET)))
            if template is not None:
                draft = template_to_draft(
                    template,
                    utility=entry.label if entry and entry.id != "other" else None,
                )
                draft["source"].update(
                    {
                        key: value
                        for key, value in (
                            ("retailer", entry.id if entry else None),
                            ("region", getattr(self, "_retail_plan_region", None)),
                            ("network", getattr(self, "_retail_plan_network", None)),
                        )
                        if value
                    }
                )
                apply_draft(self, draft)
                return await self._rp_continue_to_tariff()
        return self.async_show_form(
            step_id=self._rp_step_id("retail_plan_preset"),
            data_schema=vol.Schema(
                {vol.Required(CONF_RETAIL_PLAN_PRESET, default=options[0][0]): _select(options, dropdown=False)}
            ),
        )

    # --- allowance --------------------------------------------------------

    def _rp_allowance_schema(self) -> vol.Schema:
        quota = getattr(self, "_tariff_import_quota", None)
        window = quota_window(quota) or ("11:00", "14:00")
        limit = float(quota.get("daily_cap_kwh", 0)) if isinstance(quota, dict) else 0.0
        price = quota_effective_price(quota) or 0.0
        return vol.Schema(
            {
                vol.Optional(CONF_ALLOWANCE_KWH, default=limit): NumberSelector(
                    NumberSelectorConfig(min=0, max=1000, step=0.1, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_ALLOWANCE_START, default=window[0]): _select(half_hour_options()),
                vol.Optional(CONF_ALLOWANCE_END, default=window[1]): _select(half_hour_options(end=True)),
                vol.Optional(CONF_ALLOWANCE_PRICE, default=round(price, 4)): NumberSelector(
                    NumberSelectorConfig(min=0, max=200, step=0.01, unit_of_measurement=self._rp_unit(), mode=NumberSelectorMode.BOX)
                ),
            }
        )

    def _rp_allowance_quota(self, user_input: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        existing = getattr(self, "_tariff_import_quota", None)
        existing = existing if isinstance(existing, dict) else {}
        return build_import_quota(
            limit_kwh=user_input.get(CONF_ALLOWANCE_KWH, 0),
            start=user_input.get(CONF_ALLOWANCE_START, "11:00"),
            end=user_input.get(CONF_ALLOWANCE_END, "14:00"),
            price_minor=user_input.get(CONF_ALLOWANCE_PRICE, 0),
            periods=list(getattr(self, "_tariff_periods", None) or []),
            standard_import=float(getattr(self, "_tariff_offpeak_rate", 0.0) or 0.0),
            name=existing.get("name"),
            timezone_token=existing.get("timezone_token"),
        )

    def _rp_allowance_placeholders(self) -> dict[str, str]:
        return {
            "period_info": render_period_overview(
                getattr(self, "_tariff_periods", None) or [],
                getattr(self, "_tariff_seasons", None),
                standard_import=float(getattr(self, "_tariff_offpeak_rate", 0.0) or 0.0),
                standard_export=float(getattr(self, "_tariff_fit_rate", 0.0) or 0.0),
                unit=self._rp_unit(),
                quota=getattr(self, "_tariff_import_quota", None),
            ),
            "plan_info": warnings_text(getattr(self, "_tariff_draft", None)),
        }


class RetailPlanSetupMixin(_RetailPlanSteps):
    """Setup flow: retailer first, before the custom tariff editor."""

    async def _rp_continue_to_tariff(self) -> Any:
        return await self.async_step_custom_tariff()

    async def async_step_retail_plan_retailer(self, user_input: dict[str, Any] | None = None) -> Any:
        """Choose the electricity retailer."""
        return await self._rp_retailer(user_input)

    async def async_step_retail_plan_network(self, user_input: dict[str, Any] | None = None) -> Any:
        """Choose the state and distribution network."""
        return await self._rp_network(user_input)

    async def async_step_retail_plan_source(self, user_input: dict[str, Any] | None = None) -> Any:
        """Choose a published plan, a preset or manual entry."""
        return await self._rp_source(user_input)

    async def async_step_retail_plan_published(self, user_input: dict[str, Any] | None = None) -> Any:
        """Import a published AER plan for the chosen network."""
        return await self._rp_published(user_input)

    async def async_step_retail_plan_preset(self, user_input: dict[str, Any] | None = None) -> Any:
        """Start from a PowerSync preset."""
        return await self._rp_preset(user_input)

    async def async_step_tariff_allowance(self, user_input: dict[str, Any] | None = None) -> Any:
        """Confirm the daily free-import allowance, then continue setup."""
        errors: dict[str, str] = {}
        if user_input is not None:
            quota, error = self._rp_allowance_quota(user_input)
            if error:
                errors["base"] = error
            else:
                self._tariff_import_quota = quota
                tariff = getattr(self, "_custom_tariff_data", None)
                if isinstance(tariff, dict) and tariff:
                    if quota:
                        tariff["import_quota"] = quota
                    else:
                        tariff.pop("import_quota", None)
                return await self.async_step_battery_system()
        return self.async_show_form(
            step_id="tariff_allowance",
            data_schema=self._rp_allowance_schema(),
            errors=errors,
            description_placeholders=self._rp_allowance_placeholders(),
        )


class RetailPlanOptionsMixin(_RetailPlanSteps):
    """Options flow: keep and edit current rates, or choose a plan."""

    _retail_plan_suffix = "_options"

    async def _rp_continue_to_tariff(self) -> Any:
        return await self.async_step_custom_tariff_options()

    async def async_step_retail_plan_options(self, user_input: dict[str, Any] | None = None) -> Any:
        """Keep the current rates or choose a new plan."""
        if user_input is not None:
            if user_input.get(CONF_RETAIL_PLAN_SOURCE) == SOURCE_EDIT:
                clear_draft(self)
                return await self.async_step_custom_tariff_options()
            return await self.async_step_retail_plan_retailer_options()
        return self.async_show_form(
            step_id="retail_plan_options",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_RETAIL_PLAN_SOURCE, default=SOURCE_EDIT): _select(
                        [
                            (SOURCE_EDIT, "Keep my current rates and edit them"),
                            (SOURCE_CHOOSE, "Choose my retailer and a new plan"),
                        ],
                        dropdown=False,
                    )
                }
            ),
        )

    async def async_step_retail_plan_retailer_options(self, user_input: dict[str, Any] | None = None) -> Any:
        """Choose the electricity retailer."""
        return await self._rp_retailer(user_input)

    async def async_step_retail_plan_network_options(self, user_input: dict[str, Any] | None = None) -> Any:
        """Choose the state and distribution network."""
        return await self._rp_network(user_input)

    async def async_step_retail_plan_source_options(self, user_input: dict[str, Any] | None = None) -> Any:
        """Choose a published plan, a preset or manual entry."""
        return await self._rp_source(user_input)

    async def async_step_retail_plan_published_options(self, user_input: dict[str, Any] | None = None) -> Any:
        """Import a published AER plan for the chosen network."""
        return await self._rp_published(user_input)

    async def async_step_retail_plan_preset_options(self, user_input: dict[str, Any] | None = None) -> Any:
        """Start from a PowerSync preset."""
        return await self._rp_preset(user_input)

    async def async_step_tariff_allowance_options(self, user_input: dict[str, Any] | None = None) -> Any:
        """Confirm the daily free-import allowance and save the tariff."""
        errors: dict[str, str] = {}
        if user_input is not None:
            quota, error = self._rp_allowance_quota(user_input)
            if error:
                errors["base"] = error
            else:
                self._tariff_import_quota = quota
                tariff = getattr(self, "_pending_custom_tariff", None)
                if isinstance(tariff, dict) and tariff:
                    if quota:
                        tariff["import_quota"] = quota
                    else:
                        tariff.pop("import_quota", None)
                    await self._save_custom_tariff(tariff)
                return await self.async_step_demand_charge_options()
        return self.async_show_form(
            step_id="tariff_allowance_options",
            data_schema=self._rp_allowance_schema(),
            errors=errors,
            description_placeholders=self._rp_allowance_placeholders(),
        )
