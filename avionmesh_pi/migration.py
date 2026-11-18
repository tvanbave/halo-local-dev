"""Offline TX-history admission. Attestations are inputs, never inferred proof."""
import hashlib
import json
import re

SOURCE = 0x8000
MAX_SEQUENCE = 0xffffff


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def status(store):
    receipt = store.get('tx_admission')
    restored = store.get('tx_restore_admission_v1')
    return {'format':1,'state_id':store.get('state_id'),
            'mesh_fingerprint':store.get('mesh_fingerprint'),'source':SOURCE,
            'high_water_mark':store.get('sequence'),'remaining':MAX_SEQUENCE-store.get('sequence'),
            'admitted':bool(receipt),'retired':bool(store.get('tx_retirement')),
            'admission_id':receipt['id'] if receipt else None,
            'history_verified_by_bridge':False,
            'admission_mode': ('backup_compatible_restore' if restored else
                               receipt.get('document', {}).get('mode') if receipt else None),
            'historical_floor_known': bool(receipt) and not restored and receipt.get('document', {}).get('mode') != 'esp_compatible_import'}


def admit(store, document):
    fields = {'format','state_id','mesh_fingerprint','source','mode','high_water_mark',
              'previous_writers_stopped','evidence_sha256','fresh_mesh','history_complete','evidence_kind'}
    if not isinstance(document,dict) or set(document)!=fields:
        raise ValueError('Expected exact TX history document schema')
    if (type(document['format']) is not int or document['format']!=1
            or document['state_id']!=store.get('state_id')
            or document['mesh_fingerprint']!=store.get('mesh_fingerprint')
            or type(document['source']) is not int or document['source']!=SOURCE
            or document['previous_writers_stopped'] is not True
            or document['history_complete'] is not True
            or type(document['fresh_mesh']) is not bool
            or not isinstance(document['evidence_sha256'],str)
            or not re.fullmatch('[0-9a-f]{64}',document['evidence_sha256'])):
        raise ValueError('History identity, source or attestations do not match')
    floor = document['high_water_mark']
    if type(floor) is not int or not 0 <= floor < MAX_SEQUENCE:
        raise ValueError('No usable sequence range')
    if document['mode'] not in ('fresh_mesh','verified_history'):
        raise ValueError('Unknown history mode')
    kinds = ('new_mesh_key',) if document['mode']=='fresh_mesh' else ('exclusive_monotonic_writer_retirement','exhaustive_key_source_audit')
    if document['evidence_kind'] not in kinds:
        raise ValueError('Evidence must cover every historical key/source use, not a last observed ESP sequence')
    identifier = canonical_digest(document)
    with store.transaction():
        if store.get('tx_retirement'):
            raise ValueError('A retired transmitter cannot be reactivated')
        previous = store.get('tx_admission')
        if previous and previous['id']==identifier:
            return status(store)  # Reopening the same admission never rolls back use.
        if floor < store.get('sequence'):
            raise ValueError('History cannot reduce the committed high-water mark')
        if document['mode']=='fresh_mesh':
            if (not document['fresh_mesh'] or floor!=0 or store.get('sequence')!=0 or previous
                    or store.journals() or store.inventory()['devices'] or store.inventory()['groups']):
                raise ValueError('Fresh-mesh admission requires an empty unused state')
        elif document['fresh_mesh']:
            raise ValueError('Verified-history mode cannot declare a fresh mesh')
        store.set('sequence',floor)
        store.set('tx_admission',{'id':identifier,'document':document,'history_verified_by_bridge':False})
    return status(store)


def retire(store):
    """Durably prohibit future dispatch before returning a handoff receipt."""
    with store.transaction():
        existing = store.get('tx_retirement')
        if existing:
            return existing
        receipt = dict(status(store), kind='pi_tx_retirement', retired=True)
        receipt['id'] = canonical_digest(receipt)
        store.set('tx_retirement',receipt)
    return receipt


def admit_cloud_import(store, *, location_id, inventory_digest):
    """Explicit ESP-compatible adoption into a new installation only.

    The vendor/ESP import supplies no complete nonce history. Do not manufacture
    a fresh-mesh or verified-history declaration. The Pi still commits each local
    sequence before dispatch, retains state across restarts and refuses wrap,
    retirement, a second local owner or replacement of an existing installation.
    Historical overlap in the shared legacy key/source domain is not ruled out.
    """
    from .cloud import pid
    location_id = pid(location_id)
    if not isinstance(inventory_digest, str) or not re.fullmatch('[0-9a-f]{64}', inventory_digest):
        raise ValueError('Expected reviewed inventory digest')
    with store.transaction():
        if (store.get('tx_admission') or store.get('tx_retirement') or store.get('sequence') != 0
                or store.journals() or store.inventory()['devices'] or store.inventory()['groups']):
            raise ValueError('Cloud adoption requires a new empty installation')
        document = dict(format=1, state_id=store.get('state_id'), mesh_fingerprint=store.get('mesh_fingerprint'),
                        source=SOURCE, mode='esp_compatible_import', fresh_mesh=False,
                        history_complete=False, historical_high_water_mark=None,
                        location_id=location_id, inventory_digest=inventory_digest)
        store.set('tx_admission', {'id':canonical_digest(document), 'document':document,
                                  'history_verified_by_bridge':False})
    return status(store)
