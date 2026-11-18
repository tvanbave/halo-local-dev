"""Cloud inventory origin, never a claim receipt or a physical UUID proof."""
import copy
from .migration import canonical_digest


def retain_cloud_devices(store, devices, location_id):
    from .cloud import pid
    location_id = pid(location_id)
    admission = store.get('tx_admission')
    if not admission or store.get('tx_retirement'):
        raise ValueError('Cloud provenance requires active transmit admission')
    sources = store.get('cloud_device_sources', {})
    for device in devices:
        target = device['device_id']
        # Existing local claims never become imported authority.
        if any(j['kind'] == 'claim' and j['target'] == target for j in store.journals()):
            continue
        if target in store.get('retired_addresses', []):
            continue
        sources.setdefault(str(target), {'source':'cloud_import', 'authority_type':'reviewed_cloud_inventory', 'location_id':location_id,
            'state_id':store.get('state_id'), 'mesh_fingerprint':store.get('mesh_fingerprint'),
            'admission_id':admission['id'], 'device_id':target, 'product_id':device['product_id']})
    store.set('cloud_device_sources', sources)


def device_source(store, target):
    admission = store.get('tx_admission') or {}
    document = admission.get('document') or {}
    sources = store.get('cloud_device_sources', {})
    source = sources.get(str(target))
    if source is None:
        # dev59 and earlier retained no per-device origin. Treat this as the
        # owner's selected current address within an admitted imported mesh,
        # never proof of the original cloud record or a prior full UUID.
        location = store.get('cloud_location') or {}
        if (document.get('mode') != 'esp_compatible_import'
                or location.get('id') != document.get('location_id')):
            raise ValueError('No retained cloud identity for this device')
        device = next((d for d in store.inventory()['devices'] if d['device_id']==target), None)
        if device is None:
            raise ValueError('Imported device is absent')
        source = {'source':'cloud_import','authority_type':'legacy_imported_inventory_owner_selected',
            'location_id':document['location_id'],
            'state_id':document.get('state_id'),'mesh_fingerprint':document.get('mesh_fingerprint'),
            'admission_id':admission.get('id'),'device_id':target,'product_id':device['product_id']}
    if (source.get('source') != 'cloud_import' or source.get('device_id') != target
            or source.get('state_id') != store.get('state_id')
            or source.get('mesh_fingerprint') != store.get('mesh_fingerprint')
            or source.get('admission_id') != admission.get('id')
            or admission.get('id') != canonical_digest(document)
            or document.get('state_id') != store.get('state_id')
            or document.get('mesh_fingerprint') != store.get('mesh_fingerprint')
            or store.get('tx_retirement')):
        raise ValueError('Retained cloud authority changed')
    return copy.deepcopy(source)
