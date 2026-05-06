#!/usr/bin/env python3
"""Open the persistent Home Assistant dashboard from local .env settings.

Expected .env keys:
  HA_URL=https://homeassistant.local:8123
  HA_LONG_LIVED_TOKEN=...
  HA_DASHBOARD_URL=https://homeassistant.local:8123/power-sync/energy

Optional:
  HA_API_URL=http://homeassistant.local:8123
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _get_config() -> tuple[str, str, str, str]:
    env_file = _load_env(ENV_PATH)
    ha_url = os.environ.get("HA_URL") or env_file.get("HA_URL") or ""
    ha_api_url = os.environ.get("HA_API_URL") or env_file.get("HA_API_URL") or ha_url
    token = os.environ.get("HA_LONG_LIVED_TOKEN") or env_file.get("HA_LONG_LIVED_TOKEN") or ""
    dashboard_url = os.environ.get("HA_DASHBOARD_URL") or env_file.get("HA_DASHBOARD_URL") or ""

    missing = [
        name
        for name, value in (
            ("HA_URL", ha_url),
            ("HA_LONG_LIVED_TOKEN", token),
            ("HA_DASHBOARD_URL", dashboard_url),
        )
        if not value
    ]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"Missing {joined} in {ENV_PATH}")

    return ha_url.rstrip("/") + "/", ha_api_url.rstrip("/") + "/", token, dashboard_url


def _api_base_url(ha_url: str) -> str:
    parsed = urlparse(ha_url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit("HA_URL must include scheme and host, for example https://homeassistant.local:8123")
    return urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))


def _check_api(ha_url: str, token: str) -> None:
    request = Request(
        urljoin(_api_base_url(ha_url), "api/"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Language": "en-AU,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            if response.status < 200 or response.status >= 300:
                raise SystemExit(f"Home Assistant API returned HTTP {response.status}")
    except HTTPError as err:
        body = err.read(1000).decode("utf-8", "replace")
        if "cloudflare" in body.lower() or "error 1010" in body.lower():
            raise SystemExit(
                "Cloudflare blocked the API check before it reached Home Assistant. "
                "Set HA_API_URL in .env to a local/direct HA URL, or use --skip-check."
            ) from err
        if err.code in (401, 403):
            raise SystemExit("Home Assistant token was rejected") from err
        raise SystemExit(f"Home Assistant API returned HTTP {err.code}") from err
    except URLError as err:
        raise SystemExit(f"Could not reach Home Assistant API: {err.reason}") from err


def _open_url(url: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", url], check=True)
        return
    if sys.platform.startswith("linux"):
        subprocess.run(["xdg-open", url], check=True)
        return
    if sys.platform.startswith("win"):
        os.startfile(url)  # type: ignore[attr-defined]
        return
    raise SystemExit(f"Unsupported platform for opening URLs: {sys.platform}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check and open the configured Home Assistant dashboard.")
    parser.add_argument("--check-only", action="store_true", help="Validate HA_URL/token but do not open the dashboard")
    parser.add_argument("--skip-check", action="store_true", help="Open the dashboard without checking the HA API first")
    args = parser.parse_args()

    _ha_url, ha_api_url, token, dashboard_url = _get_config()

    if not args.skip_check:
        _check_api(ha_api_url, token)
        print("Home Assistant API check passed.")

    if not args.check_only:
        _open_url(dashboard_url)
        print("Opened Home Assistant dashboard.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
