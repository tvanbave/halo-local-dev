"""Offline interpretation of a saved /api/snapshot. Never connects or changes state.

Usage: python -m avionmesh_pi.ha_readiness saved-snapshot.json
Only the allowlisted summary is emitted; credentials and raw diagnostics are not.
"""
import argparse
import json
from .bridge import LIGHTS, CONTROLLERS


def assess(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('status'), dict):
        raise ValueError('Expected an API snapshot with a status object')
    status = snapshot['status']
    inventory = snapshot.get('inventory', {})
    if not isinstance(inventory, dict):
        raise ValueError('Expected an inventory object')
    devices = inventory.get('devices', [])
    if not isinstance(devices, list) or any(not isinstance(d, dict) for d in devices):
        raise ValueError('Expected device metadata rows')
    enabled = status.get('mqtt_enabled') is True
    broker = status.get('mqtt_ready') is True
    mesh = status.get('mesh_ready') is True
    pending = status.get('pending_transactions')
    recovery_clear = type(pending) is int and pending == 0
    exposed = [d for d in devices if d.get('exposed') is True and d.get('product_id') in {*LIGHTS, *CONTROLLERS}]
    steps = []
    if not enabled:
        steps.append('MQTT is disabled. Configure the intended broker only in an authorized live session.')
    elif not broker:
        steps.append('MQTT is disconnected. Check broker reachability, TLS trust, credentials and topic permissions; do not change mesh enrollment.')
    if not mesh:
        steps.append('The mesh is disconnected. Local control needs an available relay and powered lights.')
    if not recovery_clear:
        steps.append('Review pending recovery before testing affected devices. Do not retry uncertain writes or clear receipts.')
    if not exposed:
        steps.append('No supported devices are exposed. Exposure is an explicit metadata choice, not a pairing or physical setup action.')
    steps.append('Live acceptance still requires authorized broker/HA discovery, restart, availability and physical controller-button tests.')
    return {
        'assessment': 'saved_snapshot_only',
        'mqtt': 'disabled' if not enabled else 'connected' if broker else 'disconnected',
        'mesh': 'connected' if mesh else 'disconnected',
        'local_control': 'potentially_available' if mesh else 'unavailable',
        'local_control_note': 'Independent of MQTT; per-device quarantine, sequence admission and busy-operation guards still apply.',
        'recovery': 'clear' if recovery_clear else 'review_required',
        'supported_exposed_devices': len(exposed),
        'hidden_devices': sum(d.get('exposed') is not True for d in devices),
        'ha_acceptance': 'not_established_by_snapshot',
        'next_steps': steps,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', help='Previously saved API snapshot JSON; no URL or live connection')
    args = parser.parse_args()
    try:
        with open(args.snapshot, encoding='utf-8') as source:
            snapshot = json.load(source)
        print(json.dumps(assess(snapshot), indent=2))
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, 'Cannot assess snapshot: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
