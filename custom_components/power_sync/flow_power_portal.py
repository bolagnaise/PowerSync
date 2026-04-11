"""Flow Power portal client for authenticated account data.

Authenticates via Azure AD B2C (email + password + SMS MFA) to the
Flow Power kWatch portal and fetches actual account pricing data
(PEA, LWAP, TWAP, DLF, etc.) from the billing system.
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import re
import time as time_mod
from datetime import datetime
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Flow Power Portal API URLs
FLOWPOWER_BASE_URL = "https://flowpower.kwatch.com.au"
FLOWPOWER_B2C_TENANT = "flowpowerb2c"
FLOWPOWER_B2C_POLICY = "B2C_1A_SignUp_SignIn"


class FlowPowerPortalClient:
    """Client for Flow Power kWatch portal.

    Authenticates via Azure AD B2C (email + password + SMS MFA)
    and fetches actual account data (PEA, LWAP, TWAP, etc.)
    directly from Flow Power's portal.
    """

    B2C_BASE = (
        f"https://{FLOWPOWER_B2C_TENANT}.b2clogin.com"
        f"/{FLOWPOWER_B2C_TENANT}.onmicrosoft.com"
        f"/{FLOWPOWER_B2C_POLICY}"
    )

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        if session is None:
            jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(cookie_jar=jar)
            self._owns_session = True
        else:
            self._session = session
            self._owns_session = False
        self._authenticated = False
        self._last_keepalive: float = 0
        self._csrf_token: str | None = None
        self._tx: str | None = None
        self._api_url: str | None = None
        self._cookies: dict[str, str] = {}
        self._home_report_guid: str | None = None
        self._home_report_properties: str | None = None

    async def close(self) -> None:
        """Close the underlying HTTP session if we own it."""
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    def _b2c_cookie_header(self) -> str:
        """Build a Cookie header string from stored cookies."""
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    def _capture_cookies(self, resp: aiohttp.ClientResponse) -> None:
        """Capture Set-Cookie headers from a response."""
        for header_val in resp.headers.getall("Set-Cookie", []):
            parts = header_val.split(";")[0]
            if "=" in parts:
                name, _, value = parts.partition("=")
                self._cookies[name.strip()] = value.strip()

    def _extract_b2c_settings(
        self, html: str, url: str
    ) -> tuple[str | None, str | None]:
        """Extract CSRF token and transId from a B2C page."""
        csrf = None
        tx = None

        settings_match = re.search(
            r"var\s+SETTINGS\s*=\s*(\{.*?\})\s*;", html, re.DOTALL
        )
        if settings_match:
            try:
                settings = json.loads(settings_match.group(1))
                csrf = settings.get("csrf")
                tx = settings.get("transId")
            except json.JSONDecodeError:
                pass

        if not csrf:
            m = re.search(r'"csrf"\s*:\s*"([^"]+)"', html)
            if m:
                csrf = m.group(1)
        if not tx:
            m = re.search(r'"transId"\s*:\s*"([^"]+)"', html)
            if m:
                tx = m.group(1)
        if not tx:
            m = re.search(r"[?&]tx=(StateProperties=[A-Za-z0-9%+=/_-]+)", url)
            if m:
                tx = m.group(1)
        if not csrf:
            m = re.search(r'name="csrf"\s+content="([^"]+)"', html)
            if m:
                csrf = m.group(1)

        return csrf, tx

    async def authenticate(self, email: str, password: str) -> dict[str, Any]:
        """Submit credentials to B2C and request SMS MFA.

        Returns {"status": "mfa_required"} if SMS was sent.
        Raises Exception on invalid credentials or network error.
        """
        _LOGGER.debug("Flow Power: Loading portal to trigger B2C redirect")
        async with self._session.get(
            f"{FLOWPOWER_BASE_URL}/Home/Index",
            timeout=aiohttp.ClientTimeout(total=30),
            allow_redirects=True,
        ) as resp:
            page_html = await resp.text()
            page_url = str(resp.url)

        for c in self._session.cookie_jar:
            self._cookies[c.key] = c.value

        csrf, tx = self._extract_b2c_settings(page_html, page_url)
        if not csrf or not tx:
            raise ValueError("Could not extract B2C auth tokens from login page")

        self._csrf_token = csrf
        self._tx = tx
        self._login_page_url = page_url
        self._b2c_base = (
            page_url.split("/oauth2/")[0] if "/oauth2/" in page_url else self.B2C_BASE
        )

        # Submit email + password via SelfAsserted
        tx_param = tx if tx.startswith("StateProperties=") else f"StateProperties={tx}"
        self_asserted_url = (
            f"{self._b2c_base}/SelfAsserted?tx={tx_param}&p={FLOWPOWER_B2C_POLICY}"
        )

        async with aiohttp.ClientSession() as clean_session:
            async with clean_session.post(
                self_asserted_url,
                data={
                    "request_type": "RESPONSE",
                    "email": email,
                    "password": password,
                },
                headers={
                    "X-CSRF-TOKEN": self._csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Origin": f"https://{FLOWPOWER_B2C_TENANT}.b2clogin.com",
                    "Referer": self._login_page_url,
                    "Cookie": self._b2c_cookie_header(),
                },
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
            ) as resp:
                self._capture_cookies(resp)
                status = resp.status
                body = await resp.text()

        if status != 200:
            raise ValueError(f"Login failed with status {status}")
        if '"status":"400"' in body or "INCORRECT_PASSWORD" in body:
            raise ValueError("Invalid email or password")

        # Confirm sign-in (triggers MFA page)
        confirmed_url = (
            f"{self._b2c_base}/api/CombinedSigninAndSignup/confirmed"
            f"?rememberMe=true"
            f"&csrf_token={self._csrf_token}"
            f"&tx={tx_param}"
            f"&p={FLOWPOWER_B2C_POLICY}"
        )

        async with aiohttp.ClientSession() as cs:
            async with cs.get(
                confirmed_url,
                headers={"Cookie": self._b2c_cookie_header()},
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=True,
            ) as resp:
                self._capture_cookies(resp)
                mfa_html = await resp.text()
                mfa_url = str(resp.url)

        new_csrf, new_tx = self._extract_b2c_settings(mfa_html, mfa_url)
        if new_csrf:
            self._csrf_token = new_csrf
        if new_tx:
            self._tx = new_tx

        # Request SMS MFA
        tx_param = (
            self._tx
            if self._tx.startswith("StateProperties=")
            else f"StateProperties={self._tx}"
        )
        mfa_request_url = (
            f"{self._b2c_base}/Phonefactor/verify"
            f"?tx={tx_param}"
            f"&p={FLOWPOWER_B2C_POLICY}"
        )

        async with aiohttp.ClientSession() as cs:
            async with cs.post(
                mfa_request_url,
                data={
                    "request_type": "VERIFICATION_REQUEST",
                    "auth_type": "onewaysms",
                    "id": "1",
                },
                headers={
                    "X-CSRF-TOKEN": self._csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Cookie": self._b2c_cookie_header(),
                },
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
            ) as resp:
                self._capture_cookies(resp)

        _LOGGER.info("Flow Power: SMS MFA code requested")
        return {"status": "mfa_required"}

    async def verify_mfa(self, code: str) -> bool:
        """Verify the SMS MFA code and establish portal session."""
        if not self._csrf_token or not self._tx:
            raise ValueError("authenticate() must be called first")

        tx_param = (
            self._tx
            if self._tx.startswith("StateProperties=")
            else f"StateProperties={self._tx}"
        )

        # Submit verification code
        verify_url = (
            f"{self._b2c_base}/Phonefactor/verify"
            f"?tx={tx_param}"
            f"&p={FLOWPOWER_B2C_POLICY}"
        )

        async with aiohttp.ClientSession() as cs:
            async with cs.post(
                verify_url,
                data={
                    "request_type": "VALIDATION_REQUEST",
                    "verification_code": code,
                },
                headers={
                    "X-CSRF-TOKEN": self._csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Cookie": self._b2c_cookie_header(),
                },
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
            ) as resp:
                self._capture_cookies(resp)
                body = await resp.text()

        if '"status":"400"' in body or "INCORRECT" in body.upper():
            return False

        # Confirm MFA
        confirmed_url = (
            f"{self._b2c_base}/api/Phonefactor/confirmed"
            f"?csrf_token={self._csrf_token}"
            f"&tx={tx_param}"
            f"&p={FLOWPOWER_B2C_POLICY}"
        )

        async with aiohttp.ClientSession() as cs:
            async with cs.get(
                confirmed_url,
                headers={"Cookie": self._b2c_cookie_header()},
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
            ) as resp:
                self._capture_cookies(resp)
                if resp.status in (200, 302):
                    redirect_html = await resp.text()
                else:
                    return False

        # Extract code and id_token from the redirect form
        code_match = re.search(
            r"name=['\"]code['\"]\s+(?:id=['\"]code['\"]\s+)?value=['\"]([^'\"]+)['\"]",
            redirect_html,
        )
        id_token_match = re.search(
            r"name=['\"]id_token['\"]\s+(?:id=['\"]id_token['\"]\s+)?value=['\"]([^'\"]+)['\"]",
            redirect_html,
        )
        state_match = re.search(
            r"name=['\"]state['\"]\s+(?:id=['\"]state['\"]\s+)?value=['\"]([^'\"]+)['\"]",
            redirect_html,
        )

        if code_match and id_token_match:
            callback_data = {
                "code": code_match.group(1),
                "id_token": id_token_match.group(1),
            }
            if state_match:
                callback_data["state"] = state_match.group(1)

            async with self._session.post(
                f"{FLOWPOWER_BASE_URL}/Home/Index",
                data=callback_data,
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=True,
            ) as resp:
                await resp.text()
                self._authenticated = resp.status == 200
        else:
            async with self._session.get(
                f"{FLOWPOWER_BASE_URL}/Home/Index",
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=True,
            ) as resp:
                body = await resp.text()
                self._authenticated = "allmenu" in body or "kWFormBase" in body

        if self._authenticated:
            self._last_keepalive = time_mod.time()
            _LOGGER.info("Flow Power: Portal authentication successful")
            await self._fetch_menu_guids()

        return self._authenticated

    async def _fetch_menu_guids(self) -> None:
        """Fetch report GUIDs from the portal menu."""
        try:
            async with self._session.get(
                f"{FLOWPOWER_BASE_URL}/menu/allmenu",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return
                menu = await resp.json()

            for menu_item in menu.get("MenuItems", []):
                for sub in menu_item.get("SubMenuItems", []):
                    if sub.get("IsDefaultReport") or sub.get("Name") == "Home":
                        self._home_report_guid = sub.get("Link")
                        self._home_report_properties = sub.get("reportPropertiesGuid")
                        return

            default_guid = menu.get("DefaultReportId")
            default_props = menu.get("DefaultReportPropertiesGuid")
            if default_guid:
                self._home_report_guid = default_guid
                self._home_report_properties = default_props

        except Exception as e:
            _LOGGER.error("Flow Power: Error fetching menu: %s", e)

    async def get_account_data(self) -> dict[str, Any] | None:
        """Fetch account data (PEA, LWAP, TWAP, DLF, etc.) from the portal."""
        if not self._authenticated:
            return None

        if not self._home_report_guid:
            await self._fetch_menu_guids()
            if not self._home_report_guid:
                _LOGGER.error("Flow Power: No home report GUID available")
                return None

        await self._keep_alive()

        try:
            request_body = {
                "reportId": self._home_report_guid,
                "reportName": "Home",
                "reportProperties": self._home_report_properties,
                "reportSettings": None,
                "applicationSettings": json.dumps(
                    {
                        "applicationState": {},
                        "formBaseState": None,
                        "clientInfo": {
                            "loadTime": datetime.utcnow().strftime(
                                "%Y-%m-%dT%H:%M:%S.000Z"
                            ),
                            "timeZone": 600,
                            "currentTime": datetime.utcnow().strftime(
                                "%Y-%m-%dT%H:%M:%S.000Z"
                            ),
                        },
                    }
                ),
            }

            async with self._session.post(
                f"{FLOWPOWER_BASE_URL}/report/get?",
                json=request_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    if resp.status in (302, 401):
                        self._authenticated = False
                        _LOGGER.warning(
                            "Flow Power: Session expired (status %s)", resp.status
                        )
                    return None
                html_response = await resp.text()

            return self._parse_user_object(html_response)

        except Exception as e:
            _LOGGER.error("Flow Power: Error fetching account data: %s", e)
            return None

    def _parse_user_object(self, html: str) -> dict[str, Any] | None:
        """Extract the userObject from portal HTML response."""
        match = re.search(r'data-userobject="([^"]+)"', html)
        if not match:
            match = re.search(r'"userObject"\s*:\s*(\{[^}]+\})', html)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass
            return None

        decoded = html_mod.unescape(match.group(1))
        try:
            user_obj = json.loads(decoded)
        except json.JSONDecodeError as e:
            _LOGGER.error("Flow Power: Failed to parse userObject JSON: %s", e)
            return None

        return {
            "lwap": user_obj.get("LWAP"),
            "lwap_import": user_obj.get("LWAPImp"),
            "lwap_actual": user_obj.get("LWAPActual"),
            "lwap_import_actual": user_obj.get("LWAPImpActual"),
            "twap": user_obj.get("TWAP"),
            "twap_import": user_obj.get("TWAPImp"),
            "avg_rrp": user_obj.get("AvgRRP"),
            "avg_usage_kw": user_obj.get("AvgUsage"),
            "avg_import_usage_kw": user_obj.get("AvgImpUsage"),
            "max_usage_kw": user_obj.get("MaxUsage"),
            "total_intervals": user_obj.get("TotalInterval"),
            "pea_30_days": user_obj.get("PEA30Days"),
            "pea_30_import": user_obj.get("PEA30ImportDays"),
            "pea_actual": user_obj.get("PEAActual"),
            "pea_target": user_obj.get("PEATarget"),
            "pea_actual_import": user_obj.get("PEAActualImport"),
            "pea_target_import": user_obj.get("PEATargetImport"),
            "bpea": user_obj.get("PEATarget"),
            "bpea_import": user_obj.get("PEATargetImport"),
            "cpea": (user_obj.get("LWAP") or 0) - (user_obj.get("TWAP") or 0),
            "cpea_import": (user_obj.get("LWAPImp") or 0)
            - (user_obj.get("TWAPImp") or 0),
            "site_losses_dlf": user_obj.get("SiteLosses"),
            "gst_multiplier": user_obj.get("GST"),
        }

    async def _keep_alive(self) -> bool:
        """Send keepalive to maintain session (every 5 minutes).

        Returns True if keepalive succeeded (cookies may have been refreshed
        and should be persisted). False if throttled, failed, or expired.
        """
        now = time_mod.time()
        if now - self._last_keepalive < 290:
            return False
        try:
            async with self._session.post(
                f"{FLOWPOWER_BASE_URL}/Account/KeepAlive",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                body = await resp.text()
                if "Success" in body:
                    self._last_keepalive = now
                    return True
                else:
                    _LOGGER.warning("Flow Power: KeepAlive returned: %s", body)
                    self._authenticated = False
                    return False
        except Exception as e:
            _LOGGER.error("Flow Power: KeepAlive failed: %s", e)
            return False

    def export_session_cookies(self) -> list[dict[str, str]]:
        """Export session cookies for persistent storage."""
        cookies = []
        for cookie in self._session.cookie_jar:
            cookies.append(
                {
                    "name": cookie.key,
                    "value": cookie.value,
                    "domain": cookie["domain"] or "",
                    "path": cookie["path"] or "/",
                    "secure": cookie["secure"] or "",
                    "httponly": cookie["httponly"] or "",
                }
            )
        _LOGGER.info(
            "Flow Power: Exported %d session cookies: %s",
            len(cookies),
            ", ".join(f"{c['name']}@{c['domain']}" for c in cookies),
        )
        return cookies

    def import_session_cookies(self, cookies: list[dict[str, str]]) -> None:
        """Import previously saved session cookies into the cookie jar."""
        from http.cookies import SimpleCookie
        from yarl import URL

        for c in cookies:
            morsel = SimpleCookie()
            morsel[c["name"]] = c["value"]
            morsel[c["name"]]["domain"] = c.get("domain", "")
            morsel[c["name"]]["path"] = c.get("path", "/")
            if c.get("secure"):
                morsel[c["name"]]["secure"] = True
            if c.get("httponly"):
                morsel[c["name"]]["httponly"] = True
            domain = c.get("domain", "").lstrip(".")
            if not domain:
                domain = "flowpower.kwatch.com.au"
            self._session.cookie_jar.update_cookies(morsel, URL(f"https://{domain}/"))
        _LOGGER.info(
            "Flow Power: Imported %d session cookies: %s",
            len(cookies),
            ", ".join(f"{c.get('name', '?')}@{c.get('domain', '?')}" for c in cookies),
        )

    async def restore_session(self) -> bool:
        """Try to restore a session from imported cookies."""
        try:
            async with self._session.post(
                f"{FLOWPOWER_BASE_URL}/Account/KeepAlive",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.text()
                if "Success" in body:
                    self._authenticated = True
                    self._last_keepalive = time_mod.time()
                    _LOGGER.info("Flow Power: Session restored via KeepAlive")
                    await self._fetch_menu_guids()
                    return True

            async with self._session.get(
                f"{FLOWPOWER_BASE_URL}/Home/Index",
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as resp:
                page = await resp.text()
                if "allmenu" in page or "kWFormBase" in page:
                    self._authenticated = True
                    self._last_keepalive = time_mod.time()
                    _LOGGER.info("Flow Power: Session restored via home page check")
                    await self._fetch_menu_guids()
                    return True

            _LOGGER.warning("Flow Power: Stored session cookies expired")
            return False

        except Exception as e:
            _LOGGER.error("Flow Power: Error restoring session: %r", e)
            return False

    @property
    def is_authenticated(self) -> bool:
        """Return whether the portal session is active."""
        return self._authenticated
