#!/usr/bin/env python3
"""Test Sigenergy credentials and list stations.

This script helps validate your Sigenergy credentials before configuring
them in PowerSync. It tests authentication and retrieves your stations.

Usage:
    python sigenergy_test_connection.py <username> <device_id> --password <password>
    python sigenergy_test_connection.py <username> <device_id> --pass-enc <encoded_password>

Where:
    username   - Your Sigenergy account email
    device_id  - 13-digit device identifier (captured from browser dev tools)
    --password - Your plain Sigenergy account password (recommended)
    --pass-enc - Pre-encoded password (advanced, for backwards compatibility)

How to find device_id:
1. Open your browser's Developer Tools (F12)
2. Go to the Network tab
3. Log in to https://app-aus.sigencloud.com/
4. Look for the POST request to /auth/oauth/token
5. In the request payload, find 'userDeviceId'

Example (with plain password):
    python sigenergy_test_connection.py user@email.com 1756353655250 --password "MySecretPass123"

Example (with encoded password):
    python sigenergy_test_connection.py user@email.com 1756353655250 --pass-enc "aZ9ejFf8Ya3lUFlQL9sprw=="
"""

import argparse
import base64
import sys
import requests
from datetime import datetime, timedelta

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False


# API Configuration
BASE_URL = "https://api-aus.sigencloud.com"
AUTH_ENDPOINT = "/auth/oauth/token"
STATIONS_ENDPOINT = "/device/station/list"
BASIC_AUTH = "Basic c2lnZW46c2lnZW4="  # base64 of "sigen:sigen"

# Sigenergy password encryption constants
_SIGENERGY_AES_KEY = b"sigensigensigenp"  # 16 bytes for AES-128
_SIGENERGY_AES_IV = b"sigensigensigenp"  # Same as key


def encode_sigenergy_password(plain_password: str) -> str:
    """Encode a plain password to Sigenergy's encrypted format.

    Sigenergy uses AES-128-CBC with PKCS7 padding, then Base64 encodes the result.
    Key and IV are both "sigensigensigenp".

    Args:
        plain_password: The plain text password

    Returns:
        Base64-encoded encrypted password (pass_enc format)
    """
    if not HAS_CRYPTOGRAPHY:
        raise ImportError(
            "The 'cryptography' package is required for plain password encoding.\n"
            "Install it with: pip install cryptography\n"
            "Or use --pass-enc with a pre-encoded password instead."
        )

    # PKCS7 padding to 16-byte block size
    padder = padding.PKCS7(128).padder()
    padded_data = padder.update(plain_password.encode("utf-8")) + padder.finalize()

    # AES-128-CBC encryption
    cipher = Cipher(algorithms.AES(_SIGENERGY_AES_KEY), modes.CBC(_SIGENERGY_AES_IV))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded_data) + encryptor.finalize()

    # Base64 encode
    return base64.b64encode(encrypted).decode("utf-8")


def test_authentication(username: str, pass_enc: str, device_id: str) -> dict:
    """Test authentication with Sigenergy API.

    Returns:
        dict with 'success' and token info or 'error' message
    """
    url = f"{BASE_URL}{AUTH_ENDPOINT}"

    headers = {
        "Authorization": BASIC_AUTH,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {
        "username": username,
        "password": pass_enc,
        "scope": "server",
        "grant_type": "password",
        "userDeviceId": device_id,
    }

    try:
        print(f"\n[*] Authenticating as: {username}")
        print(f"[*] Device ID: {device_id}")
        print(f"[*] Pass_enc length: {len(pass_enc)} characters")
        print(f"[*] Connecting to: {url}")

        response = requests.post(url, headers=headers, data=data, timeout=30)

        if response.status_code != 200:
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:200]}"
            }

        result = response.json()
        token_data = result.get("data", result)

        if "access_token" not in token_data:
            return {
                "success": False,
                "error": f"No access_token in response: {result}"
            }

        expires_in = token_data.get("expires_in", 3600)
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

        return {
            "success": True,
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "expires_in": expires_in,
            "expires_at": expires_at.isoformat(),
        }

    except requests.exceptions.Timeout:
        return {"success": False, "error": "Connection timeout"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"Request error: {e}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {e}"}


def get_stations(access_token: str) -> dict:
    """Get list of stations using access token.

    Returns:
        dict with 'success' and stations list or 'error' message
    """
    url = f"{BASE_URL}{STATIONS_ENDPOINT}"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        print(f"\n[*] Fetching stations from: {url}")

        response = requests.get(url, headers=headers, timeout=30)

        if response.status_code != 200:
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:200]}"
            }

        result = response.json()

        # Sigenergy wraps data in various formats
        stations = result.get("data", [])
        if isinstance(stations, dict):
            stations = stations.get("records", stations.get("list", []))

        return {
            "success": True,
            "stations": stations,
        }

    except Exception as e:
        return {"success": False, "error": f"Error fetching stations: {e}"}


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Test Sigenergy credentials and list stations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  With plain password (recommended):
    %(prog)s user@email.com 1756353655250 --password "MySecretPass123"

  With pre-encoded password (advanced):
    %(prog)s user@email.com 1756353655250 --pass-enc "aZ9ejFf8Ya3lUFlQL9sprw=="
"""
    )
    parser.add_argument("username", help="Your Sigenergy account email")
    parser.add_argument("device_id", help="13-digit device identifier")

    # Password options (mutually exclusive)
    pwd_group = parser.add_mutually_exclusive_group(required=True)
    pwd_group.add_argument(
        "--password", "-p",
        help="Your plain Sigenergy account password (recommended)"
    )
    pwd_group.add_argument(
        "--pass-enc", "-e",
        help="Pre-encoded password (advanced, for backwards compatibility)"
    )

    args = parser.parse_args()

    username = args.username
    device_id = args.device_id

    # Determine the encoded password
    if args.pass_enc:
        pass_enc = args.pass_enc
        print("[*] Using pre-encoded password")
    else:
        try:
            pass_enc = encode_sigenergy_password(args.password)
            print(f"[*] Encoded plain password to: {pass_enc}")
        except ImportError as e:
            print(f"\n[X] Error: {e}")
            sys.exit(1)

    print("=" * 60)
    print("Sigenergy Connection Test")
    print("=" * 60)

    # Test authentication
    auth_result = test_authentication(username, pass_enc, device_id)

    if not auth_result.get("success"):
        print(f"\n[X] Authentication FAILED")
        print(f"    Error: {auth_result.get('error')}")
        print("\n[!] Common issues:")
        print("    - Incorrect password or credentials")
        print("    - Incorrect device_id")
        print("    - Account may be locked (try logging in via browser)")
        sys.exit(1)

    print(f"\n[+] Authentication SUCCESS!")
    print(f"    Token expires in: {auth_result['expires_in']} seconds")
    print(f"    Token expires at: {auth_result['expires_at']}")

    # Get stations
    stations_result = get_stations(auth_result["access_token"])

    if not stations_result.get("success"):
        print(f"\n[X] Failed to fetch stations")
        print(f"    Error: {stations_result.get('error')}")
        sys.exit(1)

    stations = stations_result.get("stations", [])

    if not stations:
        print(f"\n[!] No stations found")
        print("    Your account may not have any stations configured yet.")
    else:
        print(f"\n[+] Found {len(stations)} station(s):")
        print("-" * 40)

        for station in stations:
            station_id = station.get("stationId") or station.get("id") or station.get("sn")
            station_name = station.get("stationName") or station.get("name") or "Unnamed"

            print(f"\n    Station ID: {station_id}")
            print(f"    Name: {station_name}")

            # Print additional info if available
            if station.get("address"):
                print(f"    Address: {station.get('address')}")
            if station.get("capacity"):
                print(f"    Capacity: {station.get('capacity')} kWh")

    print("\n" + "=" * 60)
    print("Configuration for PowerSync:")
    print("=" * 60)
    print(f"\nFor Home Assistant (custom_components/power_sync):")
    print(f"  Username: {username}")
    print(f"  Password: (use your plain password in the UI)")
    print(f"  Device ID: {device_id}")
    if stations:
        station_id = stations[0].get("stationId") or stations[0].get("id") or stations[0].get("sn")
        print(f"  Station ID: {station_id}")

    print("\n[+] Test completed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
