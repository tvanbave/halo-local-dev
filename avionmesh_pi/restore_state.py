"""Offline database preparation for an explicitly reviewed full-backup restore.

Only private copies of supplied bytes are opened. No Store, original installation,
network or radio is touched. A compatibility receipt records the limits of an old
backup without inventing transmitter history or changing persistent scene owners.
"""
import copy
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from . import backup
from .bridge import load_devices
from .migration import canonical_digest, MAX_SEQUENCE, SOURCE

RESTORE_ADMISSION = 'tx_restore_admission_v1'
RESTORE_HISTORY = 'tx_restore_history_v1'
SUPERSEDED = 'installation_superseded_v1'
SCENES = 'native_scenes_v1'
SCENE_PROOFS = 'native_scene_activation_proofs_v1'


class RestoreStateError(ValueError):
    """Fixed diagnostics only; never include raw SQLite records or credentials."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _fail(code, message):
    raise RestoreStateError(code, message)


def _copy_input(value):
    if (not isinstance(value, bytes) or not 100 <= len(value) <= backup.MAX_DATABASE_BYTES
            or value[:16] != b'SQLite format 3\x00' or value[18:20] != b'\x01\x01'):
        _fail('invalid_database', 'The backup does not contain a supported complete database.')
    return value


def _inventory(value):
    if not isinstance(value, dict) or set(value) != {'devices', 'groups'}:
        _fail('invalid_inventory', 'The saved device and group inventory is invalid.')
    try:
        devices = load_devices(value['devices'])
        ids = {d.device_id for d in devices}
        groups = value['groups']
        if not isinstance(groups, list) or len(groups) > 128:
            raise ValueError
        seen = set()
        for g in groups:
            if (not isinstance(g, dict) or set(g) != {'group_id', 'name', 'members', 'exposed', 'protected'}
                    or type(g['group_id']) is not int or not 256 <= g['group_id'] <= 24575
                    or g['group_id'] in seen or not isinstance(g['name'], str) or not 1 <= len(g['name']) <= 128
                    or type(g['exposed']) is not bool or type(g['protected']) is not bool
                    or not isinstance(g['members'], list)
                    or any(type(t) is not int or t not in ids for t in g['members'])
                    or len(set(g['members'])) != len(g['members'])
                    or g['protected'] and (g['members'] or g['exposed'])):
                raise ValueError
            seen.add(g['group_id'])
    except (ValueError, TypeError, KeyError):
        _fail('invalid_inventory', 'The saved device and group inventory is invalid.')
    return value


def _admission(metadata):
    if 'tx_retirement' in metadata:
        _fail('retired_transmitter', 'This installation was retired. Its backup cannot reactivate it.')
    if SUPERSEDED in metadata:
        _fail('superseded_installation', 'This installation was replaced. Use the active installation or its latest backup.')
    if metadata['sequence'] >= MAX_SEQUENCE:
        _fail('sequence_exhausted', 'The saved radio counter is exhausted; restore cannot reset it.')
    receipt = metadata.get('tx_admission')
    if (not isinstance(receipt, dict) or set(receipt) != {'id', 'document', 'history_verified_by_bridge'}
            or not isinstance(receipt.get('document'), dict)):
        _fail('invalid_admission', 'The saved bridge has no valid transmitter admission.')
    doc = receipt['document']
    if (receipt.get('id') != canonical_digest(doc) or receipt.get('history_verified_by_bridge') is not False
            or doc.get('state_id') != metadata['state_id'] or doc.get('mesh_fingerprint') != metadata['mesh_fingerprint']
            or type(doc.get('source')) is not int or doc['source'] != SOURCE
            or type(doc.get('format')) is not int or doc['format'] != 1):
        _fail('invalid_admission', 'The saved transmitter admission does not match this database.')
    mode = doc.get('mode')
    if mode in ('fresh_mesh', 'verified_history'):
        expected = {'format', 'state_id', 'mesh_fingerprint', 'source', 'mode', 'high_water_mark',
                    'previous_writers_stopped', 'evidence_sha256', 'fresh_mesh', 'history_complete', 'evidence_kind'}
        if set(doc) != expected:
            _fail('invalid_admission', 'The saved transmitter admission is invalid.')
        floor = doc.get('high_water_mark')
        if (type(floor) is not int or not 0 <= floor <= metadata['sequence']
                or doc.get('history_complete') is not True or doc.get('previous_writers_stopped') is not True
                or not isinstance(doc.get('evidence_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', doc['evidence_sha256'])
                or mode == 'fresh_mesh' and (doc.get('fresh_mesh') is not True or floor != 0 or doc.get('evidence_kind') != 'new_mesh_key')
                or mode == 'verified_history' and (doc.get('fresh_mesh') is not False or doc.get('evidence_kind') not in ('exclusive_monotonic_writer_retirement', 'exhaustive_key_source_audit'))):
            _fail('invalid_admission', 'The saved transmitter admission is invalid.')
    elif mode == 'esp_compatible_import':
        expected = {'format', 'state_id', 'mesh_fingerprint', 'source', 'mode', 'fresh_mesh', 'history_complete',
                    'historical_high_water_mark', 'location_id', 'inventory_digest'}
        if (set(doc) != expected or not isinstance(doc.get('location_id'), str)
                or not re.fullmatch('[A-Za-z0-9_-]{1,128}', doc['location_id'])
                or not isinstance(doc.get('inventory_digest'), str)
                or not re.fullmatch('[0-9a-f]{64}', doc['inventory_digest'])):
            _fail('invalid_admission', 'The saved cloud-import admission is invalid.')
        if doc.get('history_complete') is not False or doc.get('fresh_mesh') is not False or doc.get('historical_high_water_mark', 1) is not None:
            _fail('invalid_admission', 'The saved cloud-import admission is invalid.')
    else:
        _fail('unsupported_admission', 'The saved transmitter admission requires a newer compatible bridge.')
    history = metadata.get(RESTORE_HISTORY, [])
    if not isinstance(history, list) or len(history) > 1024:
        _fail('invalid_restore_history', 'The saved restore history is invalid or full.')
    seen = set()
    for item in history:
        if (not isinstance(item, dict) or set(item) != {'id', 'document', 'history_verified_by_bridge'}
                or not isinstance(item['document'], dict) or item['history_verified_by_bridge'] is not False
                or item['id'] != canonical_digest(item['document']) or item['id'] in seen):
            _fail('invalid_restore_history', 'The saved restore history is invalid.')
        d = item['document']
        expected = {'format', 'mode', 'state_id', 'mesh_fingerprint', 'source', 'high_water_mark',
                    'original_admission_id', 'backup_sha256', 'backup_created_at', 'fresh_mesh', 'history_complete',
                    'previous_writers_stopped', 'previous_writers_verified_by_bridge', 'current_state_preserved'}
        if (set(d) != expected or type(d.get('format')) is not int or d['format'] != 1
                or type(d.get('source')) is not int or d['source'] != SOURCE
                or d.get('previous_writers_stopped') is not True or d.get('previous_writers_verified_by_bridge') is not False
                or type(d.get('current_state_preserved')) is not bool
                or not isinstance(d.get('backup_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', d['backup_sha256'])
                or not isinstance(d.get('backup_created_at'), str) or not 1 <= len(d['backup_created_at']) <= 64
                or d.get('mode') != 'backup_compatible_restore' or d.get('history_complete') is not False
                or d.get('fresh_mesh') is not False or d.get('original_admission_id') != receipt['id']
                or d.get('state_id') != metadata['state_id'] or d.get('mesh_fingerprint') != metadata['mesh_fingerprint']
                or type(d.get('high_water_mark')) is not int or not 0 <= d['high_water_mark'] <= metadata['sequence']):
            _fail('invalid_restore_history', 'The saved restore history does not match this database.')
        seen.add(item['id'])
    current = metadata.get(RESTORE_ADMISSION)
    if RESTORE_ADMISSION in metadata and (not history or current != history[-1]):
        _fail('invalid_restore_history', 'The saved restore admission is inconsistent.')
    if history and current != history[-1]:
        _fail('invalid_restore_history', 'The saved restore admission is missing.')


def _read_database(data):
    _copy_input(data)
    try:
        with tempfile.TemporaryDirectory(prefix='halo-restore-check-') as folder:
            path = Path(folder) / 'state.sqlite3'
            path.write_bytes(data); path.chmod(0o600)
            db = backup._connect(path)
            try:
                summary = backup._summary(db)
                metadata = {k: backup._json(v) for k, v in db.execute('SELECT key,value FROM metadata')}
                journal_rows = db.execute('SELECT id,kind,target,status,detail FROM journals ORDER BY rowid').fetchall()
                binding_rows = db.execute('SELECT target,snapshot,verified FROM bindings ORDER BY target').fetchall()
            finally:
                db.close()
        journals = {r[0]: dict(id=r[0], kind=r[1], target=r[2], status=r[3], detail=backup._json(r[4])) for r in journal_rows}
        bindings = {r[0]: dict(snapshot=backup._json(r[1]), verified=r[2]) for r in binding_rows}
        if len(journals) != len(journal_rows) or len(bindings) != len(binding_rows):
            _fail('invalid_state', 'The saved database contains duplicate identities.')
        _admission(metadata)
        inventory = _inventory(metadata.get('inventory', {'devices': [], 'groups': []}))
        ids = {d['device_id'] for d in inventory['devices']}
        for name, lower, upper in (('known_targets', 256, 24575), ('retired_addresses', 32896, 65407)):
            values = metadata.get(name, [])
            if (not isinstance(values, list) or any(type(v) is not int or not lower <= v <= upper for v in values)
                    or len(set(values)) != len(values)):
                _fail('invalid_reservations', 'Saved mesh address reservations are invalid.')
        if ids & set(metadata.get('retired_addresses', [])):
            _fail('retired_device_in_inventory', 'A retired device is still in the saved inventory; review the original state.')
        retired = metadata.get('retired_claims', [])
        if not isinstance(retired, list) or any(not isinstance(v, str) or not v for v in retired) or len(set(retired)) != len(retired):
            _fail('invalid_reservations', 'Saved device retirement records are invalid.')
        rooms = metadata.get('device_rooms', {})
        if not isinstance(rooms, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in rooms.items()):
            _fail('invalid_rooms', 'The saved room labels are invalid.')
        return dict(metadata=metadata, journals=journals, bindings=bindings, summary=summary)
    except RestoreStateError:
        raise
    except (backup.BackupError, OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        _fail('invalid_database', 'The saved database could not be validated; no installation was changed.')


def _physical_inventory(metadata):
    inventory = metadata.get('inventory', {'devices': [], 'groups': []})
    return {'devices': sorted((d['device_id'], d['product_id']) for d in inventory['devices']),
            'groups': sorted((g['group_id'], g['protected'], g['name'] if g['protected'] else None, tuple(sorted(g['members']))) for g in inventory['groups'])}


def _physical_scenes(metadata):
    from .scene_engine import SceneEngine
    from .protocol import integer
    rows = metadata.get(SCENES, [])
    if not isinstance(rows, list) or len(rows) > 20:
        _fail('invalid_scenes', 'The saved scene definitions are invalid.')
    ids, refs = set(), set()
    try:
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']
                    or row['id'] in ids or type(row.get('exposed')) is not bool
                    or not isinstance(row.get('owner'), str) or not re.fullmatch('[0-9a-f]{64}', row['owner'])):
                raise ValueError
            integer(row['reference'], 1, 20, 'scene reference')
            if row['reference'] in refs:
                raise ValueError
            SceneEngine._validate(None, row['name'], row['members'])
            ids.add(row['id']); refs.add(row['reference'])
    except (ValueError, TypeError, KeyError):
        _fail('invalid_scenes', 'The saved scene definitions are invalid.')
    return {s['id']: {k: v for k, v in s.items() if k not in ('name', 'exposed')} for s in rows}


def _receipt_state(rows):
    # Startup only changes attempted to uncertain; neither is permission to retry.
    return {k: dict(v, status='uncertain' if v['status'] == 'attempted' else v['status']) for k, v in rows.items()}


def _review(candidate_bytes, *, backup_digest, created_at, current_bytes=None):
    if not isinstance(backup_digest, str) or not re.fullmatch('[0-9a-f]{64}', backup_digest):
        _fail('invalid_backup_identity', 'Expected the verified full-backup digest.')
    try:
        if not isinstance(created_at, str) or not 1 <= len(created_at) <= 64:
            raise ValueError
        when = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
        if when.tzinfo is None:
            raise ValueError
    except ValueError:
        _fail('invalid_backup_time', 'The backup creation time is invalid.')
    candidate = _read_database(candidate_bytes)
    metadata = candidate['metadata']
    _physical_scenes(metadata)
    current = _read_database(current_bytes) if current_bytes is not None else None
    if current is not None:
        live = current['metadata']
        if live['mesh_fingerprint'] != metadata['mesh_fingerprint']:
            _fail('different_mesh', 'This backup belongs to another network. Restore it on an empty replacement Pi.')
        if live['state_id'] != metadata['state_id'] or live['tx_admission'] != metadata['tx_admission']:
            _fail('different_installation', 'This backup belongs to a different bridge history. Use an empty replacement Pi.')
        if (_physical_inventory(live) != _physical_inventory(metadata)
                or _physical_scenes(live) != _physical_scenes(metadata)):
            _fail('physical_inventory_changed', 'Lights, group memberships or scene presets changed since this backup. Use a current backup; restoring here would discard those changes.')
        if _receipt_state(current['journals']) != _receipt_state(candidate['journals']):
            _fail('operation_history_changed', 'Device programming or recovery records changed since this backup. Use a current backup; unfinished and newer operations cannot be overwritten.')
        chosen = copy.deepcopy(current)
        # Current operational history always wins. Only explicitly cosmetic
        # inventory fields and room labels are restored from an older backup.
        chosen['metadata']['inventory'] = copy.deepcopy(metadata.get('inventory', {'devices': [], 'groups': []}))
        chosen['metadata']['device_rooms'] = copy.deepcopy(metadata.get('device_rooms', {}))
        if SCENES in metadata:
            chosen['metadata'][SCENES] = copy.deepcopy(metadata[SCENES])
        chosen['metadata']['sequence'] = max(metadata['sequence'], live['sequence'])
        # Preserve reservations from either known state, never reclaim addresses.
        for name in ('known_targets', 'retired_addresses', 'retired_claims'):
            chosen['metadata'][name] = sorted(set(metadata.get(name, [])) | set(live.get(name, [])))
        histories = live.get(RESTORE_HISTORY, []) + metadata.get(RESTORE_HISTORY, [])
        history = []
        for row in histories:
            if row not in history:
                history.append(copy.deepcopy(row))
        chosen['metadata'][RESTORE_HISTORY] = history
    else:
        chosen = copy.deepcopy(candidate)
    return chosen, candidate, current


def _prepare(candidate_bytes, *, backup_digest, created_at, current_bytes=None):
    chosen, candidate, current = _review(candidate_bytes, backup_digest=backup_digest, created_at=created_at, current_bytes=current_bytes)
    meta = chosen['metadata']
    floor = meta['sequence']
    doc = dict(format=1, mode='backup_compatible_restore', state_id=meta['state_id'],
               mesh_fingerprint=meta['mesh_fingerprint'], source=SOURCE, high_water_mark=floor,
               original_admission_id=meta['tx_admission']['id'], backup_sha256=backup_digest,
               backup_created_at=created_at, fresh_mesh=False, history_complete=False,
               previous_writers_stopped=True, previous_writers_verified_by_bridge=False,
               current_state_preserved=current is not None)
    receipt = dict(id=canonical_digest(doc), document=doc, history_verified_by_bridge=False)
    history = meta.setdefault(RESTORE_HISTORY, [])
    if receipt in history:
        history.remove(receipt)
    history.append(receipt)
    if len(history) > 1024:
        _fail('restore_history_full', 'The saved restore history is full; preserve it for review.')
    meta[RESTORE_ADMISSION] = receipt
    # Stale observations must not be presented as current readback evidence.
    # Persistent HA mapping tables/cache remain available under their existing
    # dependency checks. Scene definitions/owners/receipts remain unchanged.
    if SCENE_PROOFS in meta:
        meta[SCENE_PROOFS] = {}
    for row in chosen['bindings'].values():
        row['verified'] = 0
    for row in chosen['journals'].values():
        if row['status'] == 'attempted':
            row['status'] = 'uncertain'
    pending = sum(row['status'] in ('prepared', 'attempted', 'uncertain') for row in chosen['journals'].values())
    inventory = meta.get('inventory', {'devices': [], 'groups': []})
    report = dict(mode='backup_compatible_restore', history_complete=False,
                  previous_writers_verified_by_bridge=False, current_state_preserved=current is not None,
                  backup_sequence=candidate['metadata']['sequence'],
                  current_sequence=current['metadata']['sequence'] if current else None,
                  restored_sequence=floor, devices=len(inventory['devices']), groups=len(inventory['groups']),
                  scenes=len(meta.get(SCENES, [])), pending_operations=pending,
                  fresh_state_reads_required=True, scene_readback_required=bool(meta.get(SCENES)),
                  original_admission_preserved=True, automatically_retried_operations=0)
    return chosen, report


def validate_restored_database(candidate_bytes, *, backup_digest, created_at, current_bytes=None):
    """Preview only. The caller still must confirm writer shutdown before apply."""
    _, report = _prepare(candidate_bytes, backup_digest=backup_digest, created_at=created_at, current_bytes=current_bytes)
    return report


def prepare_restored_database(candidate_bytes, *, backup_digest, created_at, current_bytes=None):
    """Return staged bytes/report after the caller's explicit restore review.

    This does not stop any bridge or install/start the result. The caller must
    hold its installation lock and shut down its runtime before replacing files.
    """
    chosen, report = _prepare(candidate_bytes, backup_digest=backup_digest, created_at=created_at, current_bytes=current_bytes)
    try:
        with tempfile.TemporaryDirectory(prefix='halo-restored-state-') as folder:
            path = Path(folder) / 'bridge.sqlite3'
            db = sqlite3.connect(path)
            try:
                db.execute('PRAGMA trusted_schema=OFF')
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('PRAGMA synchronous=FULL')
                db.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE journals(id TEXT PRIMARY KEY, kind TEXT NOT NULL, target INTEGER NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL);
                    CREATE TABLE bindings(target INTEGER PRIMARY KEY, snapshot TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0);''')
                with db:
                    db.executemany('INSERT INTO metadata VALUES (?,?)', [(k, json.dumps(v)) for k, v in chosen['metadata'].items()])
                    db.executemany('INSERT INTO journals VALUES (?,?,?,?,?)', [(r['id'], r['kind'], r['target'], r['status'], json.dumps(r['detail'])) for r in chosen['journals'].values()])
                    db.executemany('INSERT INTO bindings VALUES (?,?,?)', [(t, json.dumps(r['snapshot']), 0) for t, r in chosen['bindings'].items()])
            finally:
                db.close()
            path.chmod(0o600)
            result = path.read_bytes()
        _read_database(result)
        return result, report
    except RestoreStateError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
        _fail('restore_preparation_failed', 'The restored database could not be prepared; no installation was changed.')
