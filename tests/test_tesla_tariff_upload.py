"""A failed native handoff must leave an earlier remote upload untouched."""
import asyncio
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('tariff_reservation',
    Path(__file__).parents[1]/'custom_components/power_sync/tesla_tariff_upload.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_failed_wait_releases_reservation_without_releasing_inflight_write(cancel):
    key = ('provider', 'base', 'site')
    state = {'busy': True, 'retry_at': 123.0}
    domain = {'_tesla_tou_uploads': {key: state}}
    async def wait():
        async with module.reserve_tariff_upload(domain, key, timeout=10 if cancel else .001):
            pytest.fail('Capture must not run while the preceding upload is in flight')
    task = asyncio.create_task(wait())
    await asyncio.sleep(0)
    assert state.get('reservation') is not None
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else module.TariffUploadReservationError):
        await task
    assert state == {'busy': True, 'retry_at': 123.0}
    state['busy'] = False
    async with module.reserve_tariff_upload(domain, key, timeout=1):
        assert state.get('reservation') is not None
    assert state == {'busy': False, 'retry_at': 123.0}


@pytest.mark.asyncio
@pytest.mark.parametrize('other', [('other', 'base', 'site'), ('provider', 'region', 'site'), ('provider', 'base', 'other')])
async def test_reservation_uses_provider_base_and_site_and_preserves_existing_owner(other):
    domain = {}
    key = ('provider', 'base', 'site')
    async with module.reserve_tariff_upload(domain, key, timeout=1) as owner:
        async with module.reserve_tariff_upload(domain, other, timeout=1):
            pass
        with pytest.raises(module.TariffUploadReservationError):
            async with module.reserve_tariff_upload(domain, key, timeout=1):
                pytest.fail('Another native owner cannot replace the reservation')
        assert domain['_tesla_tou_uploads'][key]['reservation'] is owner
