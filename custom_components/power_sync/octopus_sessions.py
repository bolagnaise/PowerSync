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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp

_LOGGER = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"

# Token lifetime: Kraken tokens last ~60 minutes; refresh before expiry.
TOKEN_REFRESH_BUFFER = timedelta(minutes=5)


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

    async def _graphql_request(self, query: str, variables: dict | None = None) -> dict | None:
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
                    _LOGGER.error("GraphQL request failed: HTTP %s", resp.status)
                    return None

                data = await resp.json()
                errors = data.get("errors")
                if errors:
                    _LOGGER.error("GraphQL errors: %s", errors)
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
        query = """
        query SavingSessions($accountNumber: String!) {
            savingSessions(accountNumber: $accountNumber) {
                events {
                    code
                    startAt
                    endAt
                    octoPointsPerKwh
                    joinedEvents {
                        eventId
                    }
                }
            }
        }
        """
        variables = {"accountNumber": self._account_number}
        data = await self._graphql_request(query, variables)
        if not data:
            return []

        sessions_data = data.get("savingSessions", {})
        events = sessions_data.get("events", [])
        results: list[SavingSession] = []

        for ev in events:
            try:
                code = ev.get("code", "")
                start_str = ev.get("startAt", "")
                end_str = ev.get("endAt", "")
                if not start_str or not end_str:
                    continue

                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                octopoints = ev.get("octoPointsPerKwh", 800)
                joined_events = ev.get("joinedEvents") or []
                joined = len(joined_events) > 0

                results.append(SavingSession(
                    code=code,
                    start=start,
                    end=end,
                    octopoints_per_kwh=octopoints,
                    joined=joined,
                    session_type="saving",
                ))
            except (ValueError, KeyError) as err:
                _LOGGER.debug("Skipping malformed saving session event: %s", err)

        _LOGGER.debug("Fetched %d saving session events", len(results))
        return results

    async def _get_free_electricity_events(self) -> list[SavingSession]:
        """Fetch free electricity campaign events."""
        query = """
        query FreeElectricity($accountNumber: String!) {
            customerFlexibilityCampaignEvents(
                accountNumber: $accountNumber
                campaignSlug: "free_electricity"
            ) {
                edges {
                    node {
                        code
                        startAt
                        endAt
                        name
                    }
                }
            }
        }
        """
        variables = {"accountNumber": self._account_number}
        data = await self._graphql_request(query, variables)
        if not data:
            return []

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
                    joined=True,  # Free electricity events are auto-enrolled
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
        query = """
        mutation JoinSavingSession($input: JoinSavingSessionsEventInput!) {
            joinSavingSessionsEvent(input: $input) {
                joinedEvent {
                    eventId
                }
            }
        }
        """
        variables = {
            "input": {
                "accountNumber": self._account_number,
                "eventCode": event_code,
            }
        }

        data = await self._graphql_request(query, variables)
        if data and data.get("joinSavingSessionsEvent", {}).get("joinedEvent"):
            _LOGGER.info("Joined saving session: %s", event_code)
            return True

        _LOGGER.warning("Failed to join saving session: %s", event_code)
        return False
