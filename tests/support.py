"""Synthetic state creation only. Production code has no test bypass."""
import hashlib
from avionmesh_pi.storage import Store
from avionmesh_pi.migration import admit, status


def declaration(store, mode='fresh_mesh', high_water_mark=0):
    current = status(store)
    return {k:current[k] for k in ('format','state_id','mesh_fingerprint','source')} | {
        'mode':mode,'high_water_mark':high_water_mark,'previous_writers_stopped':True,
        'evidence_sha256':hashlib.sha256(b'synthetic offline test mesh').hexdigest(),
        'fresh_mesh':mode=='fresh_mesh','history_complete':True,
        'evidence_kind':'new_mesh_key' if mode=='fresh_mesh' else 'exhaustive_key_source_audit'}


def synthetic_store(directory,key):
    store = Store(directory,key)
    if store.get('tx_admission') is None:
        admit(store,declaration(store))
    return store
