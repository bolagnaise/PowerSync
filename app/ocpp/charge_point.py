# app/ocpp/charge_point.py
"""
OCPP 1.6J ChargePoint handler.

Handles OCPP protocol messages for a connected charge point, including:
- BootNotification, Heartbeat, StatusNotification
- StartTransaction, StopTransaction, MeterValues
- Remote commands: RemoteStartTransaction, RemoteStopTransaction, SetChargingProfile
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, TYPE_CHECKING

try:
    from ocpp.routing import on, after
    from ocpp.v16 import ChargePoint as OcppChargePoint
    from ocpp.v16 import call, call_result
    from ocpp.v16.enums import (
        Action,
        RegistrationStatus,
        AuthorizationStatus,
        ChargePointStatus,
        ChargePointErrorCode,
        RemoteStartStopStatus,
        ChargingProfilePurposeType,
        ChargingProfileKindType,
        ChargingRateUnitType,
        ResetType,
        ResetStatus,
        ClearChargingProfileStatus,
    )
    from ocpp.v16.datatypes import ChargingProfile, ChargingSchedule, ChargingSchedulePeriod
    OCPP_AVAILABLE = True
except ImportError:
    OCPP_AVAILABLE = False

if TYPE_CHECKING:
    from .server import OCPPServer

_LOGGER = logging.getLogger(__name__)


class ChargePointHandler:
    """
    Handler for a single connected OCPP charge point.

    Wraps the ocpp library's ChargePoint class and handles all OCPP messages.
    """

    def __init__(self, charger_id: str, websocket, server: 'OCPPServer'):
        """
        Initialize the charge point handler.

        Args:
            charger_id: Unique identifier for this charge point
            websocket: WebSocket connection
            server: Reference to the OCPP server
        """
        self.charger_id = charger_id
        self.websocket = websocket
        self.server = server

        # Create the OCPP ChargePoint instance
        if OCPP_AVAILABLE:
            self._cp = _OcppHandler(charger_id, websocket, self)
        else:
            self._cp = None

        # Track current state
        self.vendor: Optional[str] = None
        self.model: Optional[str] = None
        self.serial_number: Optional[str] = None
        self.firmware_version: Optional[str] = None
        self.status: str = 'Unavailable'
        self.error_code: str = 'NoError'
        self.current_transaction_id: Optional[int] = None

    async def start(self):
        """Start handling messages from this charge point."""
        if self._cp:
            await self._cp.start()

    # =========================================================================
    # Remote Commands (called from server)
    # =========================================================================

    async def remote_start_transaction(self, id_tag: str, connector_id: int = 1) -> bool:
        """Send RemoteStartTransaction command."""
        if not self._cp:
            return False

        try:
            request = call.RemoteStartTransaction(
                id_tag=id_tag,
                connector_id=connector_id,
            )
            response = await self._cp.call(request)
            return response.status == RemoteStartStopStatus.accepted
        except Exception as e:
            _LOGGER.error(f"RemoteStartTransaction error: {e}")
            return False

    async def remote_stop_transaction(self, transaction_id: int) -> bool:
        """Send RemoteStopTransaction command."""
        if not self._cp:
            return False

        try:
            request = call.RemoteStopTransaction(transaction_id=transaction_id)
            response = await self._cp.call(request)
            return response.status == RemoteStartStopStatus.accepted
        except Exception as e:
            _LOGGER.error(f"RemoteStopTransaction error: {e}")
            return False

    async def set_charging_profile(
        self,
        connector_id: int,
        limit_watts: int,
        duration_seconds: Optional[int] = None
    ) -> bool:
        """
        Set a charging power limit.

        Args:
            connector_id: Connector ID (usually 1)
            limit_watts: Power limit in watts
            duration_seconds: Optional duration for the limit
        """
        if not self._cp:
            return False

        try:
            # Create charging schedule period
            period = ChargingSchedulePeriod(
                start_period=0,
                limit=limit_watts / 1000.0,  # Convert to kW
            )

            # Create charging schedule
            schedule = ChargingSchedule(
                charging_rate_unit=ChargingRateUnitType.w,
                charging_schedule_period=[{
                    'startPeriod': 0,
                    'limit': float(limit_watts),
                }],
            )
            if duration_seconds:
                schedule.duration = duration_seconds

            # Create charging profile
            profile = ChargingProfile(
                charging_profile_id=1,
                stack_level=0,
                charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
                charging_profile_kind=ChargingProfileKindType.relative,
                charging_schedule={
                    'chargingRateUnit': 'W',
                    'chargingSchedulePeriod': [{'startPeriod': 0, 'limit': float(limit_watts)}],
                },
            )

            request = call.SetChargingProfile(
                connector_id=connector_id,
                cs_charging_profiles={
                    'chargingProfileId': 1,
                    'stackLevel': 0,
                    'chargingProfilePurpose': 'TxDefaultProfile',
                    'chargingProfileKind': 'Relative',
                    'chargingSchedule': {
                        'chargingRateUnit': 'W',
                        'chargingSchedulePeriod': [{'startPeriod': 0, 'limit': float(limit_watts)}],
                    },
                },
            )
            response = await self._cp.call(request)
            return response.status == 'Accepted'
        except Exception as e:
            _LOGGER.error(f"SetChargingProfile error: {e}")
            return False

    async def clear_charging_profile(self, connector_id: int = 1) -> bool:
        """Clear charging profiles on a connector."""
        if not self._cp:
            return False

        try:
            request = call.ClearChargingProfile(connector_id=connector_id)
            response = await self._cp.call(request)
            return response.status == ClearChargingProfileStatus.accepted
        except Exception as e:
            _LOGGER.error(f"ClearChargingProfile error: {e}")
            return False

    async def reset(self, hard: bool = False) -> bool:
        """Reset the charge point."""
        if not self._cp:
            return False

        try:
            reset_type = ResetType.hard if hard else ResetType.soft
            request = call.Reset(type=reset_type)
            response = await self._cp.call(request)
            return response.status == ResetStatus.accepted
        except Exception as e:
            _LOGGER.error(f"Reset error: {e}")
            return False

    # =========================================================================
    # Database Updates
    # =========================================================================

    def _update_charger_in_db(self, **kwargs):
        """Update charger record in database."""
        if not self.server.app:
            return

        try:
            with self.server.app.app_context():
                from app import db
                from app.models import OCPPCharger, User

                charger = OCPPCharger.query.filter_by(charger_id=self.charger_id).first()

                if not charger:
                    # Create new charger record - find first user for now
                    # In production, you'd want to associate with a specific user
                    user = User.query.first()
                    if not user:
                        _LOGGER.warning("No user found to associate charger with")
                        return

                    charger = OCPPCharger(
                        user_id=user.id,
                        charger_id=self.charger_id,
                    )
                    db.session.add(charger)

                # Update fields
                for key, value in kwargs.items():
                    if hasattr(charger, key):
                        setattr(charger, key, value)

                charger.last_seen = datetime.utcnow()
                db.session.commit()

        except Exception as e:
            _LOGGER.error(f"Database error: {e}")

    def _create_transaction_in_db(
        self,
        transaction_id: int,
        connector_id: int,
        id_tag: str,
        meter_start: int
    ):
        """Create a new transaction record."""
        if not self.server.app:
            return

        try:
            with self.server.app.app_context():
                from app import db
                from app.models import OCPPCharger, OCPPTransaction

                charger = OCPPCharger.query.filter_by(charger_id=self.charger_id).first()
                if not charger:
                    return

                transaction = OCPPTransaction(
                    charger_id=charger.id,
                    user_id=charger.user_id,
                    transaction_id=transaction_id,
                    connector_id=connector_id,
                    id_tag=id_tag,
                    start_time=datetime.utcnow(),
                    meter_start=meter_start / 1000.0,  # Convert Wh to kWh
                )
                db.session.add(transaction)

                # Update charger
                charger.current_transaction_id = transaction_id
                charger.current_energy_kwh = 0
                db.session.commit()

        except Exception as e:
            _LOGGER.error(f"Database error creating transaction: {e}")

    def _stop_transaction_in_db(
        self,
        transaction_id: int,
        meter_stop: int,
        reason: str
    ):
        """Update transaction record with stop data."""
        if not self.server.app:
            return

        try:
            with self.server.app.app_context():
                from app import db
                from app.models import OCPPCharger, OCPPTransaction

                charger = OCPPCharger.query.filter_by(charger_id=self.charger_id).first()
                if not charger:
                    return

                transaction = OCPPTransaction.query.filter_by(
                    charger_id=charger.id,
                    transaction_id=transaction_id
                ).first()

                if transaction:
                    transaction.stop_time = datetime.utcnow()
                    transaction.meter_stop = meter_stop / 1000.0  # Convert Wh to kWh
                    transaction.stop_reason = reason
                    transaction.energy_kwh = transaction.meter_stop - (transaction.meter_start or 0)

                # Update charger
                charger.current_transaction_id = None
                charger.current_energy_kwh = None
                charger.current_power_kw = None
                db.session.commit()

        except Exception as e:
            _LOGGER.error(f"Database error stopping transaction: {e}")


if OCPP_AVAILABLE:
    class _OcppHandler(OcppChargePoint):
        """Internal OCPP message handler using the ocpp library."""

        def __init__(self, charger_id: str, websocket, handler: ChargePointHandler):
            super().__init__(charger_id, websocket)
            self.handler = handler

        @on(Action.boot_notification)
        async def on_boot_notification(
            self,
            charge_point_vendor: str,
            charge_point_model: str,
            **kwargs
        ):
            """Handle BootNotification from charger."""
            _LOGGER.info(
                f"BootNotification from {self.id}: {charge_point_vendor} {charge_point_model}"
            )

            # Update handler state
            self.handler.vendor = charge_point_vendor
            self.handler.model = charge_point_model
            self.handler.serial_number = kwargs.get('charge_point_serial_number')
            self.handler.firmware_version = kwargs.get('firmware_version')

            # Update database
            self.handler._update_charger_in_db(
                vendor=charge_point_vendor,
                model=charge_point_model,
                serial_number=kwargs.get('charge_point_serial_number'),
                firmware_version=kwargs.get('firmware_version'),
                is_connected=True,
                last_boot=datetime.utcnow(),
            )

            # Emit event
            self.handler.server.emit_event('boot', self.id, {
                'vendor': charge_point_vendor,
                'model': charge_point_model,
            })

            return call_result.BootNotification(
                current_time=datetime.now(tz=timezone.utc).isoformat(),
                interval=60,  # Heartbeat interval in seconds
                status=RegistrationStatus.accepted,
            )

        @on(Action.heartbeat)
        async def on_heartbeat(self):
            """Handle Heartbeat from charger."""
            self.handler._update_charger_in_db(last_seen=datetime.utcnow())

            return call_result.Heartbeat(
                current_time=datetime.now(tz=timezone.utc).isoformat()
            )

        @on(Action.status_notification)
        async def on_status_notification(
            self,
            connector_id: int,
            error_code: str,
            status: str,
            **kwargs
        ):
            """Handle StatusNotification from charger."""
            _LOGGER.info(
                f"StatusNotification from {self.id}: connector={connector_id}, "
                f"status={status}, error={error_code}"
            )

            old_status = self.handler.status
            self.handler.status = status
            self.handler.error_code = error_code

            # Update database
            self.handler._update_charger_in_db(
                status=status,
                error_code=error_code,
            )

            # Emit events based on status changes
            if status == 'Charging' and old_status != 'Charging':
                self.handler.server.emit_event('charging_starts', self.id, {
                    'connector_id': connector_id,
                })
            elif old_status == 'Charging' and status != 'Charging':
                self.handler.server.emit_event('charging_stops', self.id, {
                    'connector_id': connector_id,
                    'new_status': status,
                })

            if status == 'Available' and old_status != 'Available':
                self.handler.server.emit_event('available', self.id, {
                    'connector_id': connector_id,
                })

            if status == 'Faulted':
                self.handler.server.emit_event('faulted', self.id, {
                    'connector_id': connector_id,
                    'error_code': error_code,
                })

            return call_result.StatusNotification()

        @on(Action.start_transaction)
        async def on_start_transaction(
            self,
            connector_id: int,
            id_tag: str,
            meter_start: int,
            timestamp: str,
            **kwargs
        ):
            """Handle StartTransaction from charger."""
            # Generate transaction ID
            import random
            transaction_id = random.randint(1, 999999)

            _LOGGER.info(
                f"StartTransaction from {self.id}: connector={connector_id}, "
                f"id_tag={id_tag}, transaction_id={transaction_id}"
            )

            self.handler.current_transaction_id = transaction_id

            # Create transaction in database
            self.handler._create_transaction_in_db(
                transaction_id=transaction_id,
                connector_id=connector_id,
                id_tag=id_tag,
                meter_start=meter_start,
            )

            # Emit event
            self.handler.server.emit_event('transaction_started', self.id, {
                'transaction_id': transaction_id,
                'connector_id': connector_id,
                'id_tag': id_tag,
            })

            return call_result.StartTransaction(
                transaction_id=transaction_id,
                id_tag_info={'status': AuthorizationStatus.accepted},
            )

        @on(Action.stop_transaction)
        async def on_stop_transaction(
            self,
            meter_stop: int,
            timestamp: str,
            transaction_id: int,
            **kwargs
        ):
            """Handle StopTransaction from charger."""
            reason = kwargs.get('reason', 'Local')

            _LOGGER.info(
                f"StopTransaction from {self.id}: transaction_id={transaction_id}, "
                f"reason={reason}, meter_stop={meter_stop}"
            )

            self.handler.current_transaction_id = None

            # Update transaction in database
            self.handler._stop_transaction_in_db(
                transaction_id=transaction_id,
                meter_stop=meter_stop,
                reason=reason,
            )

            # Emit event
            self.handler.server.emit_event('transaction_stopped', self.id, {
                'transaction_id': transaction_id,
                'reason': reason,
                'energy_wh': meter_stop,
            })

            return call_result.StopTransaction(
                id_tag_info={'status': AuthorizationStatus.accepted},
            )

        @on(Action.meter_values)
        async def on_meter_values(
            self,
            connector_id: int,
            meter_value: list,
            **kwargs
        ):
            """Handle MeterValues from charger."""
            # Parse meter values
            power_w = None
            energy_wh = None
            soc = None

            for mv in meter_value:
                for sv in mv.get('sampledValue', []):
                    measurand = sv.get('measurand', 'Energy.Active.Import.Register')
                    value = float(sv.get('value', 0))

                    if 'Power' in measurand:
                        power_w = value
                    elif 'Energy' in measurand:
                        energy_wh = value
                    elif 'SoC' in measurand:
                        soc = int(value)

            # Update charger state
            update_data = {}
            if power_w is not None:
                update_data['current_power_kw'] = power_w / 1000.0
            if energy_wh is not None:
                update_data['current_energy_kwh'] = energy_wh / 1000.0
                update_data['meter_value_kwh'] = energy_wh / 1000.0
            if soc is not None:
                update_data['current_soc'] = soc

            if update_data:
                self.handler._update_charger_in_db(**update_data)

            # Emit event for energy threshold triggers
            if energy_wh is not None:
                self.handler.server.emit_event('meter_values', self.id, {
                    'connector_id': connector_id,
                    'power_w': power_w,
                    'energy_wh': energy_wh,
                    'soc': soc,
                })

            return call_result.MeterValues()

        @on(Action.authorize)
        async def on_authorize(self, id_tag: str):
            """Handle Authorize request - accept all tags."""
            _LOGGER.info(f"Authorize request from {self.id}: id_tag={id_tag}")

            return call_result.Authorize(
                id_tag_info={'status': AuthorizationStatus.accepted}
            )

        @on(Action.data_transfer)
        async def on_data_transfer(self, vendor_id: str, **kwargs):
            """Handle DataTransfer - vendor-specific messages."""
            _LOGGER.info(f"DataTransfer from {self.id}: vendor={vendor_id}")

            return call_result.DataTransfer(status='Accepted')
