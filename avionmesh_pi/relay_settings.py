"""Relay preferences and the last connection scan. Reading settings never starts a scan."""
from uuid import uuid4

from .bootstrap import AutoBlueZTransport, relay_lights

PREFERENCE = 'bluetooth_relay_preference'


def _saved(store):
    return store.get(PREFERENCE, {'revision': 'initial', 'preferred': None})


def _match(lights, saved):
    if isinstance(saved, dict):
        address = saved.get('address')
        light = lights.get(address) if isinstance(address, str) else None
        if light and light['device_id'] == saved.get('device_id'):
            return address
    return None


def scan_policy(store):
    lights = relay_lights(store)
    return set(lights), _match(lights, _saved(store)['preferred'])


def relay_status(runtime):
    transport, store = runtime.transport, runtime.store
    automatic = isinstance(transport, AutoBlueZTransport)
    current = getattr(transport, 'address', None) if transport.ready else None
    lights = relay_lights(store)
    saved = _saved(store)
    preferred = _match(lights, saved['preferred'])
    observations = transport.observations if automatic else {}
    addresses = set(lights) | observations.keys()
    if current:
        addresses.add(current)
    candidates = []
    for address in sorted(addresses):
        signal, seen = observations.get(address, (None, None))
        candidates.append(dict(address=address, **lights.get(address, {'device_id': None, 'name': None}),
                               rssi=signal, seen_at=seen,
                               latest=automatic and address in transport.latest_candidates))
    return {'configurable': automatic, 'revision': saved['revision'], 'preferred': preferred,
            'current': current, 'scanned_at': transport.scanned_at if automatic else None,
            'fallback': bool(automatic and current and preferred and current != preferred
                             and transport.attempted_preference == preferred),
            'candidates': candidates}


class RelayConflict(ValueError):
    pass


def save_preference(runtime, data):
    store = runtime.store
    if not isinstance(runtime.transport, AutoBlueZTransport):
        raise RelayConflict('This installation has a fixed relay configuration.')
    if not isinstance(data, dict) or set(data) != {'revision', 'preferred'}:
        raise ValueError('Expected a revision and a preferred light or null')
    selection = data['preferred']
    with store.transaction():
        if data['revision'] != _saved(store)['revision']:
            raise RelayConflict('The saved relay preference changed. Refresh and review your selection.')
        if selection is not None:
            if (not isinstance(selection, dict) or set(selection) != {'address', 'device_id'}
                    or type(selection['device_id']) is not int or not _match(relay_lights(store), selection)):
                raise RelayConflict('This light can no longer be identified. Refresh and choose another light.')
        store.set(PREFERENCE, {'revision': uuid4().hex, 'preferred': selection})
    return relay_status(runtime)
