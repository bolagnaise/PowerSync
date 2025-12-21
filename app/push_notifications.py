# app/push_notifications.py
"""
Push notification service for Tesla Sync mobile app.
Supports iOS APNs notifications.
"""

import os
import json
import jwt
import time
import httpx
import logging
from datetime import datetime

logger = logging.getLogger('app.push_notifications')

# APNs configuration
APNS_KEY_ID = os.environ.get('APNS_KEY_ID')
APNS_TEAM_ID = os.environ.get('APNS_TEAM_ID')
APNS_AUTH_KEY_PATH = os.environ.get('APNS_AUTH_KEY_PATH')
APNS_BUNDLE_ID = 'com.teslasync.health'

# Use sandbox for development, production for release
APNS_USE_SANDBOX = os.environ.get('APNS_USE_SANDBOX', 'true').lower() == 'true'
APNS_HOST = 'api.sandbox.push.apple.com' if APNS_USE_SANDBOX else 'api.push.apple.com'


def get_apns_auth_token():
    """Generate JWT token for APNs authentication."""
    if not APNS_KEY_ID or not APNS_TEAM_ID or not APNS_AUTH_KEY_PATH:
        logger.warning("APNs not configured - missing APNS_KEY_ID, APNS_TEAM_ID, or APNS_AUTH_KEY_PATH")
        return None

    try:
        with open(APNS_AUTH_KEY_PATH, 'r') as f:
            auth_key = f.read()

        token = jwt.encode(
            {
                'iss': APNS_TEAM_ID,
                'iat': int(time.time())
            },
            auth_key,
            algorithm='ES256',
            headers={
                'kid': APNS_KEY_ID
            }
        )
        return token
    except Exception as e:
        logger.error(f"Failed to generate APNs auth token: {e}")
        return None


def send_push_notification(device_token: str, title: str, body: str, data: dict = None) -> bool:
    """
    Send a push notification to an iOS device.

    Args:
        device_token: The APNs device token
        title: Notification title
        body: Notification body text
        data: Optional custom data payload

    Returns:
        True if notification was sent successfully, False otherwise
    """
    auth_token = get_apns_auth_token()
    if not auth_token:
        logger.warning("Cannot send push notification - APNs not configured")
        return False

    if not device_token:
        logger.warning("Cannot send push notification - no device token")
        return False

    # Build APNs payload
    payload = {
        'aps': {
            'alert': {
                'title': title,
                'body': body
            },
            'sound': 'default',
            'badge': 1
        }
    }

    # Add custom data if provided
    if data:
        payload['data'] = data

    headers = {
        'authorization': f'bearer {auth_token}',
        'apns-topic': APNS_BUNDLE_ID,
        'apns-push-type': 'alert',
        'apns-priority': '10',
        'apns-expiration': '0'
    }

    url = f'https://{APNS_HOST}/3/device/{device_token}'

    try:
        with httpx.Client(http2=True) as client:
            response = client.post(
                url,
                headers=headers,
                json=payload,
                timeout=30.0
            )

            if response.status_code == 200:
                logger.info(f"Push notification sent successfully to {device_token[:20]}...")
                return True
            else:
                logger.error(f"APNs error {response.status_code}: {response.text}")
                return False

    except Exception as e:
        logger.error(f"Failed to send push notification: {e}")
        return False


def send_firmware_update_notification(user, old_version: str, new_version: str) -> bool:
    """
    Send a notification about a firmware update.

    Args:
        user: User model instance
        old_version: Previous firmware version
        new_version: New firmware version

    Returns:
        True if notification was sent successfully
    """
    if not user.push_notifications_enabled:
        logger.debug(f"Push notifications disabled for {user.email}")
        return False

    if not user.notify_firmware_updates:
        logger.debug(f"Firmware update notifications disabled for {user.email}")
        return False

    if not user.apns_device_token:
        logger.debug(f"No device token for {user.email}")
        return False

    title = "Powerwall Firmware Updated"
    body = f"Your Powerwall firmware has been updated from {old_version} to {new_version}"

    data = {
        'type': 'firmware_update',
        'old_version': old_version,
        'new_version': new_version,
        'timestamp': datetime.utcnow().isoformat()
    }

    logger.info(f"Sending firmware update notification to {user.email}: {old_version} -> {new_version}")
    return send_push_notification(user.apns_device_token, title, body, data)


def check_and_notify_firmware_change(user, current_version: str) -> bool:
    """
    Check if firmware version changed and send notification if so.

    Args:
        user: User model instance
        current_version: Current firmware version from API

    Returns:
        True if firmware changed, False otherwise
    """
    from app import db

    if not current_version:
        return False

    stored_version = user.powerwall_firmware_version

    # First time seeing firmware - just store it
    if not stored_version:
        user.powerwall_firmware_version = current_version
        user.powerwall_firmware_updated = datetime.utcnow()
        db.session.commit()
        logger.info(f"Stored initial firmware version for {user.email}: {current_version}")
        return False

    # Check if version changed
    if stored_version != current_version:
        logger.info(f"Firmware changed for {user.email}: {stored_version} -> {current_version}")

        # Send notification
        send_firmware_update_notification(user, stored_version, current_version)

        # Update stored version
        user.powerwall_firmware_version = current_version
        user.powerwall_firmware_updated = datetime.utcnow()
        db.session.commit()

        return True

    return False
