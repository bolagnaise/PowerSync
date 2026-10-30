"""Cross-layer account recovery with real manager/view and card controls."""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

pytest.importorskip("jwt")  # via test_chatgpt_auth; PyJWT ships with HA core

from test_chatgpt_auth import Response, Session, Store, callback, token
from test_chatgpt_frontend import _run_component_runtime
from test_chatgpt_view import Request, body
import test_chatgpt_view as view_test_support


integration_view = view_test_support.fixture


def test_consumed_backend_session_restarts_and_connects_through_card(integration_view):
    """A consumed real grant cannot masquerade as a reusable frontend retry."""
    view = integration_view
    store, session, clock = Store(), Session(), [time.time()]
    manager = view.auth.ChatGPTAuthManager(session, store, lambda: clock[0])
    view.monkeypatch.setattr(view.auth, "get_chatgpt_auth", lambda *_: manager)
    view.entry.options[view.const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER] = "chatgpt"

    async def prepare_api_responses():
        initial = body(await view.view.get(Request(user=view.admin)))
        first = body(await view.view.post(Request({"action": "start"}, view.admin)))
        session.tokens.append(aiohttp.ClientConnectionError("synthetic outage"))
        failure = await view.view.post(Request({
            "action": "complete", "login_id": first["login_id"],
            "callback_url": callback(first),
        }, view.admin))
        assert failure.status == 503
        second = body(await view.view.post(Request({"action": "start"}, view.admin)))
        nonce = parse_qs(urlsplit(second["auth_url"]).query)["nonce"][0]
        session.tokens.append(token(clock[0], nonce))
        completion = await view.view.post(Request({
            "action": "complete", "login_id": second["login_id"],
            "callback_url": callback(second),
        }, view.admin))
        assert completion.status == 200
        assert body(completion)["connected"]
        return {
            "initial": initial, "first": first, "failure": body(failure),
            "second": second, "completion": body(completion),
            "first_callback": callback(first), "second_callback": callback(second),
        }

    responses = asyncio.run(prepare_api_responses())
    _run_component_runtime("const actual = " + json.dumps(responses) + r""";
      let starts = 0, completions = 0;
      const { card } = await setup(async (method, route, payload) => {
        if (route === 'power_sync/optimization') return completions === 2 ? ready : notConfigured;
        if (method === 'GET') return completions === 2 ? actual.completion : actual.initial;
        if (payload.action === 'start') return ++starts === 1 ? actual.first : actual.second;
        if (payload.action === 'complete') {
          const expected = completions++ === 0 ? actual.first : actual.second;
          expect(payload.login_id === expected.login_id, 'card submitted a consumed login identity');
          if (completions === 1) {
            const error = new Error('HTTP 503'); error.body = actual.failure; throw error;
          }
          return actual.completion;
        }
        throw new Error('Unexpected account request');
      });
      click(card, 'Continue with ChatGPT'); await settle();
      input(card, 'chatgpt-callback', actual.first_callback); submitCallback(card); await settle();
      expect(button(card, 'Continue with ChatGPT'), 'real consumed API response traps the card');
      expect(!card.shadowRoot.getElementById('chatgpt-callback'), 'real failure retained a consumed callback');
      click(card, 'Continue with ChatGPT'); await settle();
      input(card, 'chatgpt-callback', actual.second_callback); submitCallback(card); await settle();
      expect(starts === 2 && completions === 2, 'card did not complete a genuinely new sign-in');
      expect(button(card, 'Generate explanation'), 'successful new account did not enable generation');
      expect(card.shadowRoot.getElementById('connection-details').hidden, 'successful account did not collapse settings');
      expect(!text(card).includes('SECRET'), 'callback or credentials escaped into visible recovery content');
      card.disconnectedCallback();
    """)


def test_real_invalidated_account_can_be_reset_and_replaced_through_card(integration_view):
    """Replacing an invalidated identity requires an explicit visible reset."""
    view = integration_view
    store, session, clock = Store(), Session(), [time.time()]
    manager = view.auth.ChatGPTAuthManager(session, store, lambda: clock[0])
    view.monkeypatch.setattr(view.auth, "get_chatgpt_auth", lambda *_: manager)
    view.entry.options[view.const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER] = "chatgpt"

    async def start_and_complete(claims=None):
        start = body(await view.view.post(Request({"action": "start"}, view.admin)))
        nonce = parse_qs(urlsplit(start["auth_url"]).query)["nonce"][0]
        session.tokens.append(token(clock[0], nonce, claims=claims))
        response = await view.view.post(Request({
            "action": "complete", "login_id": start["login_id"],
            "callback_url": callback(start),
        }, view.admin))
        return start, response

    async def prepare_api_responses():
        _, original = await start_and_complete()
        assert original.status == 200
        clock[0] += 3600
        session.tokens.append(Response({"error": "invalid_grant"}, 400))
        expired = await view.view.post(Request({"action": "models"}, view.admin))
        assert body(expired)["code"] == "invalid_grant"
        invalidated = body(await view.view.get(Request(user=view.admin)))
        assert invalidated["status"] == "reconnect_required"
        reset = await view.view.post(Request({"action": "logout"}, view.admin))
        assert reset.status == 200
        start, replacement = await start_and_complete({
            "sub": "replacement-subject", "email": "replacement@example.test",
        })
        assert replacement.status == 200
        assert body(replacement)["account"]["email"] == "replacement@example.test"
        return {
            "invalidated": invalidated, "reset": body(reset), "start": start,
            "replacement": body(replacement), "callback": callback(start),
        }

    responses = asyncio.run(prepare_api_responses())
    _run_component_runtime("const actual = " + json.dumps(responses) + r""";
      let resets = 0, starts = 0, completed = false;
      const { card } = await setup(async (method, route, payload) => {
        if (route === 'power_sync/optimization') return completed ? ready : notConfigured;
        if (method === 'GET') return completed ? actual.replacement : actual.invalidated;
        if (payload.action === 'logout') { resets++; return actual.reset; }
        if (payload.action === 'start') {
          expect(resets === 1, 'identity replacement skipped explicit reset'); starts++; return actual.start;
        }
        if (payload.action === 'complete') { completed = true; return actual.replacement; }
        throw new Error('Unexpected account request');
      });
      expect(resets === 0 && starts === 0, 'card changed an invalidated identity automatically');
      click(card, 'Reset ChatGPT account'); await settle();
      click(card, 'Continue with ChatGPT'); await settle();
      input(card, 'chatgpt-callback', actual.callback); submitCallback(card); await settle();
      expect(completed && button(card, 'Generate explanation'), 'explicit replacement did not restore generation');
      openSettings(card);
      expect(text(card).includes('replacement@example.test'), 'old account identity survived replacement');
      expect(!text(card).includes('SECRET'), 'replacement exposed credentials or callback');
      card.disconnectedCallback();
    """)


def test_timed_out_logout_recovers_real_remote_warning_through_card(integration_view):
    """A lost logout reply cannot hide the real server cleanup notice."""
    view = integration_view
    store, session, clock = Store(), Session(), [time.time()]
    manager = view.auth.ChatGPTAuthManager(session, store, lambda: clock[0])
    view.monkeypatch.setattr(view.auth, "get_chatgpt_auth", lambda *_: manager)
    view.entry.options[view.const.CONF_OPTIMIZATION_AI_SUMMARY_PROVIDER] = "chatgpt"

    async def prepare_api_responses():
        start = body(await view.view.post(Request({"action": "start"}, view.admin)))
        nonce = parse_qs(urlsplit(start["auth_url"]).query)["nonce"][0]
        session.tokens.append(token(clock[0], nonce))
        completed = await view.view.post(Request({
            "action": "complete", "login_id": start["login_id"],
            "callback_url": callback(start),
        }, view.admin))
        assert completed.status == 200
        initial = body(await view.view.get(Request(user=view.admin)))
        session.revoke_failure = True
        result = await view.view.post(Request({"action": "logout"}, view.admin))
        assert result.status == 200 and "warning" in body(result)
        recovered = body(await view.view.get(Request(user=view.admin)))
        assert recovered["warning"] == body(result)["warning"]
        assert not recovered["connected"]
        assert "SECRET" not in json.dumps(recovered)
        return {"initial": initial, "recovered": recovered}

    responses = asyncio.run(prepare_api_responses())
    _run_component_runtime("const actual = " + json.dumps(responses) + r""";
      let timedOut = false, logouts = 0;
      const { card } = await setup(async (method, route, payload) => {
        if (route === 'power_sync/optimization') return timedOut ? notConfigured : ready;
        if (method === 'GET') return timedOut ? actual.recovered : actual.initial;
        if (payload.action === 'logout') {
          logouts++; timedOut = true;
          const error = new Error('Synthetic lost logout reply'); error.code = 'client_timeout'; throw error;
        }
        throw new Error('Unexpected account request');
      });
      openSettings(card); click(card, 'Log out of ChatGPT'); await settle();
      expect(logouts === 1, 'timeout caused another logout instead of status reconciliation');
      expect(text(card).includes(actual.recovered.warning), 'lost POST reply hid the real server cleanup warning');
      expect(!button(card, 'Generate explanation'), 'disconnected real API status still enabled generation');
      click(card, 'Dismiss disconnection notice'); await settle();
      expect(!text(card).includes(actual.recovered.warning), 'explicit notice dismissal did not work');
      card.disconnectedCallback(); card.connectedCallback(); await settle();
      expect(!text(card).includes(actual.recovered.warning), 'fresh real API status resurrected an acknowledged notice');
      expect(!text(card).includes('SECRET'), 'recovered logout status exposed account credentials');
      card.disconnectedCallback();
    """)
