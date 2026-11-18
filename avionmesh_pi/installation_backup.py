"""Encrypted, portable installation snapshots with no restore or radio side effects.

Callers own obtaining a consistent SQLite snapshot and deciding whether it may
be restored. This module never opens Store, changes a live database, starts a
service or grants permission to transmit. Every database byte, including private
operation receipts, remains inside an authenticated password-encrypted envelope.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import struct
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import __version__
from . import backup as state_backup
from .bridge import CONTROLLERS, LIGHTS, load_devices
from .mqtt_settings import SettingsError, _document as mqtt_document
from .protocol import derive_mcp_key

MAGIC = b'HALO-LOCAL-INSTALLATION\x00\x01'
# Keep UI requests and private validation copies within the Pi container limits.
# The separate offline state-backup format retains its existing 64 MiB bound.
MAX_DATABASE_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 128 * 1024
_SALT_BYTES, _NONCE_BYTES, _TAG_BYTES = 16, 12, 16
_HEADER_BYTES = len(MAGIC) + _SALT_BYTES + _NONCE_BYTES
MAX_ARCHIVE_BYTES = _HEADER_BYTES + _TAG_BYTES + 4 + MAX_METADATA_BYTES + MAX_DATABASE_BYTES
_VERSION = re.compile(r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(?:(?:\.dev|a|b|rc)[0-9]{1,6})?')
_CAPABILITIES = frozenset({
    'ordinary_claim', 'bulk_claim', 'claim_reset_recovery', 'wired_editor',
    'battery_editor', 'inventory_complete', 'removal', 'wired_onboarding',
    'battery_onboarding', 'accessory_onboarding', 'group_membership',
    'group_membership_readback', 'dimmer_editor', 'anyplace_editor',
    'anyplace_onboarding', 'cloud_sync', 'native_scenes', 'observation_refresh',
    'startup_light_read',
})
_SETTINGS = frozenset({'node', 'mesh_passphrase', 'api_token', 'capabilities', 'mqtt', 'browser_login'})


class InstallationBackupError(ValueError):
    """Fixed, credential-free diagnostics suitable for the management API."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _fail(code, message):
    raise InstallationBackupError(code, message)


def _password(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 256 or '\x00' in value:
        _fail('invalid_backup_password', 'Use a backup password containing 12–256 characters.')
    try:
        encoded = value.encode('utf-8')
    except UnicodeError:
        _fail('invalid_backup_password', 'The backup password contains invalid text.')
    if len(encoded) > 1024:
        _fail('invalid_backup_password', 'The backup password is too long.')
    return encoded


def _key(password, salt):
    # The archive cannot select a costly KDF. These fixed parameters require
    # roughly 32 MiB of working memory, bounded even for unauthenticated input.
    return Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password)


def _text(value, maximum, *, empty=False):
    return (isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _json(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_METADATA_BYTES:
        _fail('invalid_backup', 'Backup metadata is missing or too large.')
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate')
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('nonfinite')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=nonfinite)
    except (UnicodeError, ValueError, RecursionError):
        _fail('invalid_backup', 'Backup metadata is invalid.')


def _settings(value):
    if not isinstance(value, dict) or set(value) != _SETTINGS:
        _fail('invalid_backup_settings', 'The backup does not contain supported portable settings.')
    if not isinstance(value['node'], str) or not re.fullmatch(r'[a-z0-9_]{1,40}', value['node']):
        _fail('invalid_backup_settings', 'The saved bridge identity is invalid.')
    if (not _text(value['api_token'], 4096) or type(value['browser_login']) is not bool
            or not isinstance(value['capabilities'], dict)
            or set(value['capabilities']) - _CAPABILITIES
            or any(type(v) is not bool for v in value['capabilities'].values())):
        _fail('invalid_backup_settings', 'The saved bridge options are invalid.')
    try:
        derive_mcp_key(value['mesh_passphrase'])
    except (TypeError, ValueError, UnicodeError):
        _fail('invalid_backup_settings', 'The saved mesh credential is invalid.')
    mqtt = value['mqtt']
    if not isinstance(mqtt, dict) or not isinstance(mqtt.get('broker'), dict):
        _fail('invalid_backup_settings', 'The saved MQTT settings are invalid.')
    broker = dict(mqtt['broker'])
    ca_pem = broker.pop('ca_pem', None)
    if 'ca_file' in broker:
        _fail('invalid_backup_settings', 'Backup certificates must be included rather than referenced by a file path.')
    if ca_pem is not None:
        try:
            if (not isinstance(ca_pem, str) or not 1 <= len(ca_pem.encode('utf-8')) <= 65536
                    or '\x00' in ca_pem or 'PRIVATE KEY' in ca_pem):
                raise ValueError('certificate')
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=ca_pem)
        except (ssl.SSLError, ValueError, UnicodeError):
            _fail('invalid_backup_settings', 'The saved MQTT certificate is invalid.')
    try:
        validated_mqtt = mqtt_document(dict(mqtt, broker=broker))
    except (SettingsError, TypeError, ValueError):
        _fail('invalid_backup_settings', 'The saved MQTT settings are invalid.')
    if ca_pem is not None:
        validated_mqtt['broker']['ca_pem'] = ca_pem
    return dict(value, capabilities=dict(value['capabilities']), mqtt=validated_mqtt)


def _inventory(value):
    if not isinstance(value, dict) or set(value) != {'devices', 'groups'}:
        raise ValueError('inventory')
    devices = load_devices(value['devices'])
    ids = {d.device_id for d in devices}
    groups = value['groups']
    if not isinstance(groups, list) or len(groups) > 128:
        raise ValueError('groups')
    seen = set()
    for group in groups:
        if (not isinstance(group, dict) or set(group) != {'group_id','name','members','exposed','protected'}
                or type(group['group_id']) is not int or not 256 <= group['group_id'] <= 24575
                or group['group_id'] in seen or not _text(group['name'], 128)
                or type(group['exposed']) is not bool or type(group['protected']) is not bool
                or not isinstance(group['members'], list) or len(group['members']) > len(ids)
                or any(type(i) is not int or i not in ids for i in group['members'])
                or len(set(group['members'])) != len(group['members'])
                or group['protected'] and (group['members'] or group['exposed'])):
            raise ValueError('group')
        seen.add(group['group_id'])
    return devices, groups


def inspect_database(database, mesh_passphrase):
    """Inspect only a private temporary copy; return private identity and counts.

    This is integrity/schema validation, not a safe-to-restore or current-history
    declaration. Unknown metadata and every journal remain byte-for-byte intact.
    """
    if (not isinstance(database, bytes) or not 512 <= len(database) <= MAX_DATABASE_BYTES
            or database[:16] != b'SQLite format 3\x00' or database[18:20] != b'\x01\x01'):
        _fail('invalid_backup_database', 'The backup does not contain a supported complete database snapshot.')
    try:
        fingerprint = hashlib.sha256(derive_mcp_key(mesh_passphrase)).hexdigest()
        with tempfile.TemporaryDirectory(prefix='halo-installation-inspect-') as directory:
            path = Path(directory).resolve() / 'snapshot.sqlite3'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(database)
            db = state_backup._connect(path)
            try:
                state = state_backup._summary(db)
                metadata = {k: state_backup._json(v) for k, v in db.execute('SELECT key,value FROM metadata')}
                if not hmac.compare_digest(state['mesh_fingerprint'], fingerprint):
                    _fail('backup_mesh_mismatch', 'The saved mesh credential does not match this backup.')
                devices, groups = _inventory(metadata.get('inventory', {'devices': [], 'groups': []}))
                room_map = metadata.get('device_rooms', {})
                if not isinstance(room_map, dict) or any(not _text(v, 128) for v in room_map.values()):
                    raise ValueError('rooms')
                scenes = metadata.get('native_scenes_v1', [])
                if not isinstance(scenes, list) or len(scenes) > 20 or any(not isinstance(v, dict) for v in scenes):
                    raise ValueError('scenes')
                admission = metadata.get('tx_admission')
                if admission is not None and (not isinstance(admission, dict)
                        or not isinstance(admission.get('id'), str)
                        or not re.fullmatch('[0-9a-f]{64}', admission['id'])
                        or not isinstance(admission.get('document'), dict)
                        or admission['document'].get('state_id') != state['state_id']
                        or admission['document'].get('mesh_fingerprint') != state['mesh_fingerprint']):
                    raise ValueError('admission')
                pending = sum(state['journal_statuses'][name] for name in ('prepared', 'attempted', 'uncertain'))
                return dict(state, devices=len(devices), lights=sum(d.product_id in LIGHTS for d in devices),
                    controllers=sum(d.product_id in CONTROLLERS for d in devices),
                    groups=sum(not g['protected'] and g['group_id'] != 24566 for g in groups),
                    rooms=len({room for target, room in room_map.items() if str(target) in {str(d.device_id) for d in devices}}),
                    scenes=len(scenes), pending_operations=pending, admitted=bool(admission),
                    admission_mode=admission['document'].get('mode') if admission else None,
                    retirement_record_present='tx_retirement' in metadata,
                    sequence_exhausted=state['sequence'] == 0xffffff)
            finally:
                db.close()
    except InstallationBackupError:
        raise
    except (state_backup.BackupError, OSError, sqlite3.Error, TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        _fail('invalid_backup_database', 'The saved database could not be validated. No installation was changed.')


def _stamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value):
        _fail('invalid_backup', 'The backup date is invalid.')
    try:
        datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        _fail('invalid_backup', 'The backup date is invalid.')
    return value


def _version(value):
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        _fail('invalid_backup', 'The backup software version is invalid.')
    return value


def _summary(state, metadata):
    fields = ('devices','lights','controllers','groups','rooms','scenes','pending_operations',
              'retirement_record_present','sequence_exhausted')
    return {key: state[key] for key in fields} | {
        'node': metadata['settings']['node'], 'created_at': metadata['created_at'],
        'version': metadata['version'], 'mqtt_enabled': metadata['settings']['mqtt']['enabled'],
        'encrypted': True, 'integrity_verified': True,
    }


def encode_backup(database, settings, password, *, created_at=None, version=__version__):
    """Validate and encrypt an already consistent installation snapshot."""
    secret = _password(password)
    settings = _settings(settings)
    inspect_database(database, settings['mesh_passphrase'])
    metadata = {'format': 1, 'created_at': _stamp(created_at or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')),
        'version': _version(version), 'settings': settings,
        'database': {'bytes': len(database), 'sha256': hashlib.sha256(database).hexdigest()}}
    try:
        encoded = json.dumps(metadata, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail('invalid_backup_settings', 'The portable backup settings could not be encoded.')
    if len(encoded) > MAX_METADATA_BYTES:
        _fail('invalid_backup_settings', 'The portable backup settings are too large.')
    salt, nonce = os.urandom(_SALT_BYTES), os.urandom(_NONCE_BYTES)
    header = MAGIC + salt + nonce
    plaintext = struct.pack('>I', len(encoded)) + encoded + database
    return header + AESGCM(_key(secret, salt)).encrypt(nonce, plaintext, header)


def decode_backup(blob, password):
    """Authenticate and validate an archive, without applying its contents."""
    secret = _password(password)
    if (not isinstance(blob, bytes) or not _HEADER_BYTES + _TAG_BYTES + 4 + 512 < len(blob) <= MAX_ARCHIVE_BYTES
            or blob[:len(MAGIC)] != MAGIC):
        _fail('invalid_backup', 'Choose a supported HALO Local installation backup.')
    header = blob[:_HEADER_BYTES]
    salt = header[len(MAGIC):len(MAGIC) + _SALT_BYTES]
    nonce = header[-_NONCE_BYTES:]
    try:
        plaintext = AESGCM(_key(secret, salt)).decrypt(nonce, blob[_HEADER_BYTES:], header)
    except InvalidTag:
        _fail('invalid_password_or_backup', 'The password is incorrect or the backup is damaged.')
    length = struct.unpack('>I', plaintext[:4])[0]
    if not 1 <= length <= MAX_METADATA_BYTES or len(plaintext) < 4 + length + 512:
        _fail('invalid_backup', 'The backup payload is incomplete or invalid.')
    metadata = _json(plaintext[4:4 + length])
    if (not isinstance(metadata, dict) or set(metadata) != {'format','created_at','version','settings','database'}
            or type(metadata['format']) is not int or metadata['format'] != 1):
        _fail('unsupported_backup_format', 'This backup format is not supported by this bridge.')
    _stamp(metadata['created_at']); _version(metadata['version'])
    settings = _settings(metadata['settings'])
    database = plaintext[4 + length:]
    description = metadata['database']
    if (not isinstance(description, dict) or set(description) != {'bytes','sha256'}
            or type(description['bytes']) is not int or description['bytes'] != len(database)
            or not isinstance(description['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', description['sha256'])
            or not hmac.compare_digest(description['sha256'], hashlib.sha256(database).hexdigest())):
        _fail('invalid_backup', 'The database size or checksum does not match the backup.')
    state = inspect_database(database, settings['mesh_passphrase'])
    return {'format': 1, 'created_at': metadata['created_at'], 'version': metadata['version'],
            'database': database, 'settings': settings, 'summary': _summary(state, metadata)}
