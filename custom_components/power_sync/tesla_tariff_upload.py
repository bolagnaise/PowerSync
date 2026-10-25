"""Reserve a site's tariff handoff without cancelling an in-flight upload."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class TariffUploadReservationError(RuntimeError):
    """Native capture could not safely wait for the preceding upload."""


@asynccontextmanager
async def reserve_tariff_upload(domain_data, key, *, timeout):
    """Drain the prior upload before capturing a native restore baseline.

    New non-owner uploads defer while reserved. The prior upload retains its
    own timeout and readback; cancelling this waiter cannot cancel that write.
    """
    state = domain_data.setdefault('_tesla_tou_uploads', {}).setdefault(
        key, {'busy': False, 'retry_at': 0.0})
    if state.get('reservation') is not None:
        raise TariffUploadReservationError('Tesla tariff handoff is already reserved; Max was not started')
    token = object()
    state['reservation'] = token
    try:
        try:
            async with asyncio.timeout(max(0, timeout)):
                while state['busy']:
                    await asyncio.sleep(.05)
        except TimeoutError as error:
            raise TariffUploadReservationError('Timed out waiting for Tesla tariff handoff; Max was not started') from error
        yield token
    finally:
        if state.get('reservation') is token:
            state.pop('reservation', None)
