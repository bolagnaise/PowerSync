"""Tests for the Octopus Saving Sessions GraphQL client."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules.setdefault("power_sync", _ps)

octopus_sessions = importlib.import_module("power_sync.octopus_sessions")


def _free_electricity_response(code: str = "free-1") -> dict:
    return {
        "customerFlexibilityCampaignEvents": {
            "edges": [
                {
                    "node": {
                        "code": code,
                        "startAt": "2026-05-05T10:00:00Z",
                        "endAt": "2026-05-05T11:00:00Z",
                        "name": "Free Electricity",
                        "isEventParticipant": True,
                    }
                }
            ]
        }
    }


def _supply_points_response() -> dict:
    return {
        "supplyPoints": {
            "edges": [
                {
                    "node": {
                        "id": "supply-point-1",
                        "externalIdentifier": "mpan-1",
                        "marketName": "GB_ELECTRICITY",
                        "meterPoint": {
                            "__typename": "ElectricityMeterPointType",
                            "mpan": "mpan-1",
                        },
                    }
                }
            ]
        }
    }


def test_get_sessions_fetches_free_electricity_without_removed_saving_sessions_query():
    client = octopus_sessions.OctopusSavingSessionsClient(
        object(), "api-key", "A-12345678"
    )
    calls = []

    async def fake_graphql_request(query, variables=None, *, log_errors=True):
        calls.append((query, variables, log_errors))
        assert "savingSessions" not in query
        if "supplyPoints" in query:
            return _supply_points_response()
        if "customerFlexibilityCampaignEvents" in query:
            assert variables["supplyPointIdentifier"] == "mpan-1"
            assert variables["campaignSlug"] == "free_electricity"
            assert variables["first"] == 50
            assert log_errors is False
            return _free_electricity_response()
        raise AssertionError(f"Unexpected GraphQL query: {query}")

    client._graphql_request = fake_graphql_request

    sessions = asyncio.run(client.get_sessions())

    assert len(sessions) == 1
    session = sessions[0]
    assert session.code == "free-1"
    assert session.session_type == "free_electricity"
    assert session.octopoints_per_kwh == 0
    assert session.joined is True
    assert len(calls) == 2


def test_free_electricity_falls_back_to_internal_supply_point_id():
    client = octopus_sessions.OctopusSavingSessionsClient(
        object(), "api-key", "A-12345678"
    )
    tried_identifiers = []

    async def fake_graphql_request(query, variables=None, *, log_errors=True):
        if "supplyPoints" in query:
            return _supply_points_response()
        if "customerFlexibilityCampaignEvents" in query:
            tried_identifiers.append(variables["supplyPointIdentifier"])
            assert log_errors is False
            if variables["supplyPointIdentifier"] == "mpan-1":
                return None
            return _free_electricity_response(code="free-2")
        raise AssertionError(f"Unexpected GraphQL query: {query}")

    client._graphql_request = fake_graphql_request

    sessions = asyncio.run(client.get_sessions())

    assert tried_identifiers == ["mpan-1", "supply-point-1"]
    assert [session.code for session in sessions] == ["free-2"]


def test_join_session_returns_false_without_removed_mutation_call():
    client = octopus_sessions.OctopusSavingSessionsClient(
        object(), "api-key", "A-12345678"
    )

    async def fail_graphql_request(*args, **kwargs):
        raise AssertionError("join_session should not call GraphQL")

    client._graphql_request = fail_graphql_request

    assert asyncio.run(client.join_session("event-1")) is False
