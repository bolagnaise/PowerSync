"""Use the actual protobuf FileStore transport for strict native-limit CAS."""
import json

import pytest
from test_powerwall_v1r_teslemetry_parity import _transport_without_key, transport_mod as m


def transport(source):
    client = _transport_without_key()
    read = m.transport_pb2.MessageEnvelope()
    read.filestore.read_file_response.file.blob = source.encode()
    read.filestore.read_file_response.hash = b'x' * 32
    written = []

    async def post(payload, din, **kwargs):
        request = m.transport_pb2.MessageEnvelope()
        request.ParseFromString(payload)
        if request.filestore.WhichOneof('message') == 'read_file_request':
            return m.TEDAPIResponse(True, read.SerializeToString())
        if kwargs.get('before_submit'):
            await kwargs['before_submit'](12345)
        if kwargs.get('before_send'):
            kwargs['before_send']()
        written.append(request)
        response = m.transport_pb2.MessageEnvelope()
        response.filestore.update_file_response.SetInParent()
        return m.TEDAPIResponse(True, response.SerializeToString())

    client.post_v1r = post
    return client, written


@pytest.mark.asyncio
async def test_actual_write_checks_original_document_hash_and_preserves_other_fields():
    original = {'site_info': {'min_site_meter_power_ac': -5, 'on_grid_solar_curtailment_enabled': False}, 'other': [None, True, 5.0, {'value': 'keep'}]}
    client, written = transport(json.dumps(original))
    events = []

    def validate(config, native_hash):
        assert config == original and native_hash == b'x' * 32
        events.append('validated')
        return True

    async def stage(expires):
        assert expires == 12345
        events.append('durable-intent')

    assert await client.write_config('DIN', {'site_info.min_site_meter_power_ac': -1.005},
        strict_json=True, validate_document=validate, before_submit=stage,
        before_write=lambda: events.append('final-guard'))
    assert events == ['validated', 'durable-intent', 'final-guard']
    request = written[0].filestore.update_file_request
    assert request.hash == b'x' * 32
    result = json.loads(request.file.blob)
    assert result['site_info']['min_site_meter_power_ac'] == -1.005
    assert result['site_info']['on_grid_solar_curtailment_enabled'] is False
    assert result['other'] == original['other'] and type(result['other'][2]) is float


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['{"site_info":{},"n":9007199254740993}', '{"site_info":{},"a":1,"a":2}'])
async def test_actual_strict_transport_rejects_lossy_or_duplicate_json_before_send(source):
    client, written = transport(source)
    assert await client.read_config('DIN', with_hash=True, strict_json=True) is None
    assert await client.write_config('DIN', {'site_info.min_site_meter_power_ac': -1}, strict_json=True) is False
    assert not written


@pytest.mark.asyncio
async def test_actual_transport_refuses_stale_cas_without_applying_updates():
    client, written = transport('{"site_info":{"min_site_meter_power_ac":-5}}')
    assert await client.write_config('DIN', {'site_info.min_site_meter_power_ac': -1}, strict_json=True,
                                     validate_document=lambda config, native_hash: False) is False
    assert not written
    assert await client.read_config('DIN', with_hash=True, strict_json=True) == {
        'config': {'site_info': {'min_site_meter_power_ac': -5}}, 'hash': (b'x' * 32).hex()}
