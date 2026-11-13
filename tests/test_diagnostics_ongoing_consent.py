"""Single owner confirmation covers the exact reviewed diagnostic/test selection."""
import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
import time
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = '_ongoing_consent_test'
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT / 'custom_components/power_sync/diagnostics_access')]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(PACKAGE + '.runtime', Path(package.__path__[0]) / 'runtime.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
wire = module.wire


class Store:
    def __init__(self):
        self.value = None
        self.fail = False

    async def async_load(self):
        return copy.deepcopy(self.value)

    async def async_save(self, value):
        if self.fail:
            raise OSError('storage unavailable')
        self.value = copy.deepcopy(value)


class Runtime(module.AccessRuntime):
    def start(self):
        self.started = True


async def setup():
    runtime = Runtime(None, Store(), None, None)
    runtime.started = False
    identity = await runtime.setup_identity()
    seed, public = wire.keypair()
    _, client = wire.keypair()
    descriptors = {}
    codes = []
    for index, kind in enumerate(('ha_read', 'optimizer_contribution'), 1):
        grant = str(index) * 32
        registration = dict(root=identity['root'], grant=grant, actor='operator', profile='codex', public_key=client, processor='OpenAI Codex')
        descriptors[grant] = dict(registration=registration, expires_at=time.time() + 300,
            disclosure='Support and private offline testing; replay then delete.',
            ceiling=dict(kind=kind, actor='operator', client='codex:' + wire.key_id(client), processor='OpenAI Codex',
                         policy_digest='digest-' + kind, audience=kind, operations=[], data_policy='replay_then_delete' if index == 2 else 'ephemeral_diagnostics'))
        codes.append('.'.join((identity['root'], grant, 'invitation', 'challenge')))

    async def post(route, data):
        assert route == '/descriptor'
        return wire.packet(seed, 'authority', identity['root'], data['grant'], {'descriptor': descriptors[data['grant']]})

    runtime.post = post
    code = 'psa1.' + wire.b64(wire.json_bytes({'invitations': codes}))
    return runtime, code, public, descriptors, codes


@pytest.mark.asyncio
async def test_one_confirmation_persists_both_purposes_and_group_survives_restart():
    runtime, code, public, _, _ = await setup()
    review = await runtime.review('owner', code, public)
    assert review['combined'] and len(review['descriptors']) == 2
    assert not runtime.grants and not runtime.started
    result = await runtime.confirm('owner', review['challenge'])
    assert len(result['grants']) == 2 and runtime.started
    grants = list(runtime.grants.values())
    assert {item['ceiling']['kind'] for item in grants} == {'ha_read', 'optimizer_contribution'}
    assert grants[0]['approval_set'] == grants[1]['approval_set'] == grants[0]['attestation']
    restored = Runtime(None, runtime.store, None, None)
    await restored.load()
    assert len(restored.grants) == 2 and all(not item['active'] for item in restored.grants.values())
    assert {item['approval_set'] for item in restored.grants.values()} == {grants[0]['approval_set']}
    await restored.revoke(result['grants'][0])
    assert all(item['revoking'] and not item['active'] for item in restored.grants.values())
    assert all('approval' not in item for item in restored.grants.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['root', 'actor', 'profile', 'public_key', 'processor'])
async def test_combined_review_refuses_different_owner_operator_or_application(field):
    runtime, code, public, descriptors, _ = await setup()
    descriptors['2' * 32]['registration'][field] = 'different'
    with pytest.raises(wire.WireError):
        await runtime.review('owner', code, public)
    assert not runtime.grants


@pytest.mark.asyncio
async def test_existing_read_approval_is_never_silently_expanded():
    runtime, _, public, _, codes = await setup()
    review = await runtime.review('owner', codes[0], public)
    assert not review['combined']
    await runtime.confirm('owner', review['challenge'])
    assert len(runtime.grants) == 1
    assert next(iter(runtime.grants.values()))['ceiling']['kind'] == 'ha_read'
    assert 'approval_set' not in next(iter(runtime.grants.values()))


@pytest.mark.asyncio
async def test_wrong_administrator_expired_review_and_replaced_identity_cannot_confirm():
    for variant in ('wrong_owner', 'expired', 'identity'):
        runtime, code, public, _, _ = await setup()
        review = await runtime.review('owner', code, public)
        if variant == 'expired':
            runtime.reviews[review['challenge']]['expires'] = 0
        if variant == 'identity':
            runtime.identity = None
            await runtime.setup_identity()
        with pytest.raises(wire.WireError):
            await runtime.confirm('other' if variant == 'wrong_owner' else 'owner', review['challenge'])
        assert not runtime.grants


@pytest.mark.asyncio
async def test_atomic_local_save_failure_does_not_start_partial_approval():
    runtime, code, public, _, _ = await setup()
    review = await runtime.review('owner', code, public)
    runtime.store.fail = True
    with pytest.raises(OSError):
        await runtime.confirm('owner', review['challenge'])
    assert not runtime.grants and not runtime.started
    assert not runtime.store.value['grants']


@pytest.mark.asyncio
async def test_duplicate_wrong_purpose_or_invalid_signed_descriptor_cannot_expand_access():
    for variant in ('duplicate', 'purpose', 'signature'):
        runtime, code, public, descriptors, codes = await setup()
        if variant == 'duplicate':
            code = 'psa1.' + wire.b64(wire.json_bytes({'invitations': [codes[0], codes[0]]}))
        if variant == 'purpose':
            descriptors['2' * 32]['ceiling']['kind'] = 'ha_read'
        if variant == 'signature':
            _, public = wire.keypair()
        with pytest.raises(wire.WireError):
            await runtime.review('owner', code, public)
        assert not runtime.grants


@pytest.mark.asyncio
async def test_root_revocation_during_descriptor_fetch_prevents_review():
    runtime, code, public, _, _ = await setup()
    post = runtime.post
    async def revoking_post(route, data):
        response = await post(route, data)
        runtime.root_pending = True
        return response
    runtime.post = revoking_post
    with pytest.raises(wire.WireError):
        await runtime.review('owner', code, public)
    assert not runtime.reviews and not runtime.grants

class _FragmentedContent:
    def __init__(self, parts, failure=None):
        self.parts = list(parts)
        self.failure = failure
        self.parts_started = 0

    async def read(self, maximum):
        # aiohttp read(n) returns available data, not necessarily a whole body.
        return self.parts.pop(0)[:maximum] if self.parts else b''

    async def iter_chunked(self, maximum):
        for part in self.parts:
            self.parts_started += 1
            for offset in range(0, len(part), maximum):
                yield part[offset:offset + maximum]
                await asyncio.sleep(0)
        if self.failure:
            raise self.failure


class _StreamResponse:
    def __init__(self, content, status=200):
        self.content = content
        self.status = status
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True


class _StreamSession:
    def __init__(self, response):
        self.response = response

    def post(self, url, **kwargs):
        assert url == wire.ORIGIN + '/descriptor'
        assert kwargs['allow_redirects'] is False
        assert kwargs['headers']['User-Agent'].startswith('PowerSync-HA-Diagnostics/')
        return self.response


def _stream_runtime(parts, status=200, failure=None):
    response = _StreamResponse(_FragmentedContent(parts, failure), status)
    return module.AccessRuntime(None, None, _StreamSession(response), None), response


@pytest.mark.asyncio
async def test_http_transport_consumes_fragmented_json_through_end_of_body():
    raw = wire.json_bytes({'payload': 'fragmented production response', 'metadata': {'version': 1}})
    runtime, response = _stream_runtime([raw[:5], raw[5:19], raw[19:]])
    assert await runtime.post('/descriptor', {}) == wire.loads(raw)
    assert response.closed


@pytest.mark.asyncio
async def test_http_transport_accepts_a_complete_frame_at_the_exact_size_limit():
    runtime, response = _stream_runtime([b'{', b'}' + b' ' * (420 * 1024 - 2)])
    assert await runtime.post('/descriptor', {}) == {}
    assert response.closed


@pytest.mark.asyncio
async def test_http_transport_refuses_extra_bytes_after_an_apparently_valid_prefix():
    runtime, response = _stream_runtime([b'{}' + b' ' * (420 * 1024 - 2), b' ', b'never consume this'])
    with pytest.raises(wire.WireError, match='payload_limit'):
        await runtime.post('/descriptor', {})
    assert response.closed and response.content.parts_started == 2


@pytest.mark.asyncio
async def test_http_transport_preserves_remote_denial_after_fragmented_error_body():
    runtime, response = _stream_runtime([b'{"error":', b'"denied"}'], status=403)
    with pytest.raises(wire.WireError, match='remote_request_denied'):
        await runtime.post('/descriptor', {})
    assert response.closed


@pytest.mark.asyncio
async def test_http_transport_never_returns_a_frame_after_stream_interruption():
    runtime, response = _stream_runtime([b'{"payload":'], failure=OSError('connection closed'))
    with pytest.raises(OSError, match='connection closed'):
        await runtime.post('/descriptor', {})
    assert response.closed
