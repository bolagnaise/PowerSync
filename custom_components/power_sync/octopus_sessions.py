"""Octopus Energy Saving Sessions GraphQL client.

Handles authentication, token refresh, session fetching, and auto-join
for Octopus Saving Sessions and Free Electricity events.

Auth flow:
  1. ObtainKrakenToken mutation with API key → 60-min JWT + refresh token
  2. Token auto-refreshes via refreshKrakenToken mutation (7-day refresh token)

Sessions:
  - Saving Sessions: demand response events paying ~800-1800 octopoints/kWh
  - Free Electricity: reverse events offering free grid power during renewable surplus
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

_LOGGER = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"

# Token lifetime: Kraken tokens last ~60 minutes; refresh before expiry.
TOKEN_REFRESH_BUFFER = timedelta(minutes=5)
SUPPLY_POINTS_PAGE_SIZE = 20
CAMPAIGN_EVENTS_PAGE_SIZE = 50
FREE_ELECTRICITY_CAMPAIGN_SLUG = "free_electricity"


@dataclass
class SavingSession:
    """Represents an Octopus Saving Session or Free Electricity event."""

    code: str
    start: datetime
    end: datetime
    octopoints_per_kwh: int  # Typically 800-1800 for saving sessions
    joined: bool
    session_type: str  # "saving" or "free_electricity"

    def is_active(self) -> bool:
        """Return True if the session is currently active."""
        now = datetime.now(timezone.utc)
        return self.start <= now < self.end

    def is_upcoming(self) -> bool:
        """Return True if the session hasn't started yet."""
        return datetime.now(timezone.utc) < self.start

    @property
    def duration_minutes(self) -> int:
        """Return the session duration in minutes."""
        return int((self.end - self.start).total_seconds() / 60)

    @property
    def rate_pence_per_kwh(self) -> float:
        """Return the effective rate in pence/kWh (octopoints / 8)."""
        return self.octopoints_per_kwh / 8.0


class OctopusSavingSessionsClient:
    """GraphQL client for Octopus Saving Sessions API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        account_number: str,
    ) -> None:
        """Initialize the client.

        Args:
            session: aiohttp client session
            api_key: Octopus API key (from developer dashboard)
            account_number: Octopus account number (e.g. "A-12345678")
        """
        self._session = session
        self._api_key = api_key
        self._account_number = account_number
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_expiry: datetime | None = None
        self._supply_point_identifiers: list[str] | None = None
        self._free_electricity_identifier: str | None = None
        self._saving_sessions_unsupported_logged = False
        self._join_unsupported_logged = False
        self._free_electricity_lookup_failed_logged = False

    async def authenticate(self) -> bool:
        """Authenticate with Octopus GraphQL API using API key.

        Returns:
            True if authentication succeeded.
        """
        query = """
        mutation ObtainKrakenToken($input: ObtainJSONWebTokenInput!) {
            obtainKrakenToken(input: $input) {
                token
                refreshToken
            }
        }
        """
        variables = {"input": {"APIKey": self._api_key}}

        try:
            async with self._session.post(
                GRAPHQL_URL,
                json={"query": query, "variables": variables},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error(
                        "Octopus GraphQL auth failed: HTTP %s", resp.status
                    )
                    return False

                data = await resp.json()
                errors = data.get("errors")
                if errors:
                    _LOGGER.error("Octopus GraphQL auth error: %s", errors)
                    return False

                token_data = data.get("data", {}).get("obtainKrakenToken", {})
                self._token = token_data.get("token")
                self._refresh_token = token_data.get("refreshToken")

                if not self._token:
                    _LOGGER.error("Octopus GraphQL auth: no token in response")
                    return False

                # Kraken tokens last ~60 minutes
                self._token_expiry = datetime.now(timezone.utc) + timedelta(minutes=55)
                _LOGGER.info("Octopus GraphQL authenticated successfully")
                return True

        except Exception as err:
            _LOGGER.error("Octopus GraphQL auth exception: %s", err)
            return False

    async def _ensure_token(self) -> bool:
        """Ensure we have a valid token, refreshing if needed.

        Returns:
            True if we have a valid token.
        """
        if self._token and self._token_expiry:
            if datetime.now(timezone.utc) < self._token_expiry - TOKEN_REFRESH_BUFFER:
                return True

        # Try refresh first if we have a refresh token
        if self._refresh_token:
            if await self._refresh():
                return True

        # Fall back to full re-auth
        return await self.authenticate()

    async def _refresh(self) -> bool:
        """Refresh the authentication token.

        Returns:
            True if refresh succeeded.
        """
        query = """
        mutation RefreshKrakenToken($input: ObtainJSONWebTokenInput!) {
            obtainKrakenToken(input: $input) {
                token
                refreshToken
            }
        }
        """
        variables = {"input": {"refreshToken": self._refresh_token}}

        try:
            async with self._session.post(
                GRAPHQL_URL,
                json={"query": query, "variables": variables},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Token refresh HTTP %s — will re-auth", resp.status)
                    return False

                data = await resp.json()
                if data.get("errors"):
                    _LOGGER.debug("Token refresh error — will re-auth")
                    return False

                token_data = data.get("data", {}).get("obtainKrakenToken", {})
                new_token = token_data.get("token")
                if not new_token:
                    return False

                self._token = new_token
                self._refresh_token = token_data.get("refreshToken") or self._refresh_token
                self._token_expiry = datetime.now(timezone.utc) + timedelta(minutes=55)
                _LOGGER.debug("Octopus token refreshed successfully")
                return True

        except Exception as err:
            _LOGGER.debug("Token refresh exception: %s", err)
            return False

    async def _graphql_request(
        self,
        query: str,
        variables: dict | None = None,
        *,
        log_errors: bool = True,
    ) -> dict | None:
        """Make an authenticated GraphQL request.

        Returns:
            The 'data' portion of the response, or None on failure.
        """
        if not await self._ensure_token():
            _LOGGER.error("Cannot make GraphQL request: no valid token")
            return None

        headers = {"Authorization": self._token}

        try:
            payload = {"query": query}
            if variables:
                payload["variables"] = variables

            async with self._session.post(
                GRAPHQL_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text()).strip()
                    if len(body) > 500:
                        body = f"{body[:500]}..."
                    log_method = _LOGGER.error if log_errors else _LOGGER.debug
                    log_method(
                        "GraphQL request failed: HTTP %s: %s",
                        resp.status,
                        body or "<empty body>",
                    )
                    return None

                data = await resp.json()
                errors = data.get("errors")
                if errors:
                    log_method = _LOGGER.error if log_errors else _LOGGER.debug
                    log_method("GraphQL errors: %s", errors)
                    return None

                return data.get("data")

        except Exception as err:
            _LOGGER.error("GraphQL request exception: %s", err)
            return None

    async def get_sessions(self) -> list[SavingSession]:
        """Fetch all saving sessions and free electricity events.

        Returns:
            List of SavingSession objects (both types combined).
        """
        sessions: list[SavingSession] = []

        # Fetch saving sessions
        saving = await self._get_saving_sessions()
        sessions.extend(saving)

        # Fetch free electricity events
        free = await self._get_free_electricity_events()
        sessions.extend(free)

        return sessions

    async def _get_saving_sessions(self) -> list[SavingSession]:
        """Fetch saving session events."""
        if not self._saving_sessions_unsupported_logged:
            _LOGGER.warning(
                "Octopus no longer exposes Saving Sessions through the Kraken "
                "GraphQL API. Direct mode will continue to fetch Free Electricity "
                "events only; use the octopus_energy/Bottlecap Dave source for "
                "Saving Sessions."
            )
            self._saving_sessions_unsupported_logged = True
        return []

    async def _get_supply_point_identifiers(self) -> list[str]:
        """Resolve candidate electricity supply point identifiers for the account."""
        if self._supply_point_identifiers is not None:
            return self._supply_point_identifiers

        query = """
        query SupplyPoints($accountNumber: String!, $first: Int!) {
            supplyPoints(accountNumber: $accountNumber, first: $first) {
                edges {
                    node {
                        id
                        externalIdentifier
                        marketName
                        meterPoint {
                            __typename
                            ... on ElectricityMeterPointType {
                                mpan
                            }
                        }
                    }
                }
            }
        }
        """
        variables = {
            "accountNumber": self._account_number,
            "first": SUPPLY_POINTS_PAGE_SIZE,
        }
        data = await self._graphql_request(query, variables)
        if not data:
            self._supply_point_identifiers = []
            return []

        supply_points = data.get("supplyPoints", {})
        identifiers: list[str] = []

        for edge in supply_points.get("edges", []):
            node = edge.get("node") or {}
            meter_point = node.get("meterPoint") or {}
            market_name = str(node.get("marketName") or "").upper()
            is_electricity = (
                meter_point.get("__typename") == "ElectricityMeterPointType"
                or "ELECTRIC" in market_name
            )
            if not is_electricity:
                continue

            for identifier in (
                node.get("externalIdentifier"),
                meter_point.get("mpan"),
                node.get("id"),
            ):
                if identifier:
                    identifier = str(identifier)
                    if identifier not in identifiers:
                        identifiers.append(identifier)

        if not identifiers:
            _LOGGER.debug(
                "No electricity supply point identifiers found for Octopus account %s",
                self._account_number,
            )

        self._supply_point_identifiers = identifiers
        return identifiers

    async def _get_free_electricity_events(self) -> list[SavingSession]:
        """Fetch free electricity campaign events."""
        query = """
        query FreeElectricity(
            $accountNumber: String!
            $supplyPointIdentifier: String!
            $campaignSlug: String!
            $first: Int!
        ) {
            customerFlexibilityCampaignEvents(
                accountNumber: $accountNumber
                supplyPointIdentifier: $supplyPointIdentifier
                campaignSlug: $campaignSlug
                first: $first
            ) {
                edges {
                    node {
                        code
                        startAt
                        endAt
                        name
                        isEventParticipant
                    }
                }
            }
        }
        """

        identifiers = (
            [self._free_electricity_identifier]
            if self._free_electricity_identifier
            else await self._get_supply_point_identifiers()
        )
        if not identifiers:
            if not self._free_electricity_lookup_failed_logged:
                _LOGGER.warning(
                    "Could not resolve an Octopus electricity supply point for "
                    "Free Electricity events on account %s",
                    self._account_number,
                )
                self._free_electricity_lookup_failed_logged = True
            return []

        data = None
        for identifier in identifiers:
            variables = {
                "accountNumber": self._account_number,
                "supplyPointIdentifier": identifier,
                "campaignSlug": FREE_ELECTRICITY_CAMPAIGN_SLUG,
                "first": CAMPAIGN_EVENTS_PAGE_SIZE,
            }
            data = await self._graphql_request(query, variables, log_errors=False)
            if data:
                self._free_electricity_identifier = identifier
                break

        if not data:
            if not self._free_electricity_lookup_failed_logged:
                _LOGGER.warning(
                    "Could not fetch Octopus Free Electricity events for account %s "
                    "using resolved electricity supply point identifiers",
                    self._account_number,
                )
                self._free_electricity_lookup_failed_logged = True
            return []

        self._free_electricity_lookup_failed_logged = False
        campaign_data = data.get("customerFlexibilityCampaignEvents", {})
        edges = campaign_data.get("edges", [])
        results: list[SavingSession] = []

        for edge in edges:
            node = edge.get("node", {})
            try:
                code = node.get("code", "")
                start_str = node.get("startAt", "")
                end_str = node.get("endAt", "")
                if not start_str or not end_str:
                    continue

                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

                results.append(SavingSession(
                    code=code,
                    start=start,
                    end=end,
                    octopoints_per_kwh=0,  # Free electricity has no octopoints
                    joined=bool(node.get("isEventParticipant")),
                    session_type="free_electricity",
                ))
            except (ValueError, KeyError) as err:
                _LOGGER.debug("Skipping malformed free electricity event: %s", err)

        _LOGGER.debug("Fetched %d free electricity events", len(results))
        return results

    async def join_session(self, event_code: str) -> bool:
        """Join a saving session event.

        Args:
            event_code: The session event code to join.

        Returns:
            True if join succeeded.
        """
        if not self._join_unsupported_logged:
            _LOGGER.warning(
                "Octopus no longer exposes Saving Sessions join support through "
                "the Kraken GraphQL API. Configure the octopus_energy/Bottlecap "
                "Dave source if auto-join is required."
            )
            self._join_unsupported_logged = True
        return False
