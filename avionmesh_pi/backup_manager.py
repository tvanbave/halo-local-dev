"""Full installation backups and explicitly reviewed, staged recovery.

HTTP never replaces a running database. The service closes MQTT/Bluetooth/Store
before committing a prepared installation, and retains the previous directory.
"""
import asyncio
import base64
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time
from . import __version__
from .installation_backup import (encode_backup, decode_backup, inspect_database,
                                  InstallationBackupError, MAX_ARCHIVE_BYTES, MAX_DATABASE_BYTES)
from .restore_state import prepare_restored_database, validate_restored_database, RestoreStateError

MAX_HTTP_BYTES = ((MAX_ARCHIVE_BYTES + 2) // 3) * 4 + 16384
POINTER = 'active-installation.json'
PENDING = 'pending-installation.json'
PREVIEW_SECONDS = 600


class BackupManagerError(ValueError):
    def __init__(self, code, message, status=409):
        self.code, self.status = code, status
        super().__init__(message)


def fail(code, message, status=409):
    raise BackupManagerError(code, message, status)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def check_version(version):
    # Format 1 is accepted only from this version or earlier in the same minor
    # series. A newer bridge may have state semantics this reader cannot know.
    def parse(value):
        match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?', value)
        if not match:
            fail('backup_version_unsupported', 'This backup requires a compatible bridge version.')
        a,b,c,dev = match.groups()
        return (int(a),int(b),int(c),int(dev) if dev is not None else 10**9)
    source, current = parse(version), parse(__version__)
    if source[:2] != current[:2] or source > current:
        fail('backup_version_unsupported', 'Update this bridge to the backup version or newer before restoring.')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def snapshot_db(connection):
    # SQLite's online backup API makes a coherent copy without initializing Store
    # or changing operation statuses. It also includes pending receipts as-is.
    if connection.execute('pragma page_count').fetchone()[0] * connection.execute('pragma page_size').fetchone()[0] > MAX_DATABASE_BYTES:
        fail('backup_too_large', 'The saved database exceeds the 8 MiB backup limit.')
    if connection.in_transaction:
        fail('backup_busy', 'A database change is in progress. Try again when it finishes.')
    with tempfile.TemporaryDirectory(prefix='halo-snapshot-') as folder:
        path = Path(folder) / 'state.sqlite3'
        db = sqlite3.connect(path)
        try:
            connection.backup(db)
        finally:
            db.close()
        if path.stat().st_size > MAX_DATABASE_BYTES:
            fail('backup_too_large', 'This installation exceeds the supported backup size.')
        return path.read_bytes()


def read_database(path):
    from .service import regular_text
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DATABASE_BYTES:
        fail('invalid_installation', 'The saved database is missing or invalid.')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        return snapshot_db(db)


def fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def write_json(path, value):
    write_private(path, (json.dumps(value, indent=2) + '\n').encode())


def replace_json(path, value):
    temporary = path.with_name('.' + path.name + '-' + secrets.token_hex(8))
    write_json(temporary, value)
    os.replace(temporary, path)
    fsync_dir(path.parent)


def read_json(path):
    from .service import regular_text
    return json.loads(regular_text(path, private=True, limit=16384))


def portable_settings(runtime, config):
    from .service import private_text, regular_text
    mqtt = copy.deepcopy(runtime.mqtt_settings._value)
    ca = mqtt['broker'].pop('ca_file', None)
    if ca:
        mqtt['broker']['ca_pem'] = regular_text(ca, limit=65536)
    return dict(node=config['node'], mesh_passphrase=private_text(config['mesh_passphrase_file']),
                api_token=private_text(config['api_token_file']), capabilities=copy.deepcopy(runtime.capabilities),
                mqtt=mqtt, browser_login=config.get('browser_login', False))


def make_installation(stage, final, database, settings, destination):
    """Write one inert complete generation. No service, Store or radio startup."""
    stage.mkdir(mode=0o700)
    state = stage / 'state'; state.mkdir(mode=0o700)
    write_private(state / 'bridge.sqlite3', database)
    write_private(stage / 'mesh_passphrase', (settings['mesh_passphrase'] + '\n').encode())
    # Browser access belongs to this Pi. Mesh/node/MQTT identity belongs to backup.
    token = destination['api_token']
    write_private(stage / 'api_token', (token + '\n').encode())
    mqtt = copy.deepcopy(settings['mqtt'])
    certificate = mqtt['broker'].pop('ca_pem', None)
    if certificate:
        write_private(stage / 'mqtt-ca.pem', certificate.encode())
        mqtt['broker']['ca_file'] = str(final / 'mqtt-ca.pem')
    write_json(state / 'mqtt-settings.json', mqtt)
    config = dict(node=settings['node'], relay_address=None, state_directory=str(final / 'state'),
        mesh_passphrase_file=str(final / 'mesh_passphrase'), api_token_file=str(final / 'api_token'),
        automatic_relay=True, capabilities=copy.deepcopy(settings['capabilities']))
    config.update(destination['service'])
    if destination.get('removal') is not None:
        config['capabilities']['removal'] = destination['removal']
    details = inspect_database(database, settings['mesh_passphrase'])
    # An empty fresh network has no relay to reconnect to yet.
    if config['commissioning_only'] and not details['devices'] and details['admission_mode'] == 'fresh_mesh':
        config.pop('automatic_relay')
        config.update(first_device_bootstrap=True, automatic_bootstrap=True)
    from .service import validate_config
    validate_config(config)
    write_json(stage / 'service.json', config)
    write_json(stage / 'installation.json', dict(format=1, state_id=details['state_id'],
        mesh_fingerprint=details['mesh_fingerprint'], node=settings['node'], origin='backup_restore'))
    fsync_dir(state); fsync_dir(stage)
    return config


def destination_service(config):
    # Preserve the actual access policy, including the defaults for token-only
    # services. A restored installation reads its broker from mqtt-settings.json.
    commissioning = config.get('commissioning_only', False)
    return dict(http_host=config.get('http_host', '127.0.0.1'), http_port=config.get('http_port', 8088),
                adapter=config.get('adapter', 'hci0'), allow_lan_http=config.get('allow_lan_http', False),
                browser_login=config.get('browser_login', False),
                trusted_lan_no_auth=config.get('trusted_lan_no_auth', False),
                commissioning_only=commissioning, mqtt=None if commissioning else {'host': config['mqtt']['host']})


def destination_settings(config):
    from .service import private_text
    return dict(service=destination_service(config), removal=config.get('capabilities', {}).get('removal', False),
                api_token=private_text(config['api_token_file']))


def generation_path(root, marker):
    if (not isinstance(marker, dict) or set(marker) != {'format', 'generation'} or marker['format'] != 1
            or not isinstance(marker['generation'], str) or not re.fullmatch('[0-9a-f]{32}', marker['generation'])):
        fail('restore_marker_invalid', 'The restore record needs review. Saved installations were preserved.')
    parent = root / 'installations'
    path = parent / marker['generation']
    if parent.is_symlink() or path.is_symlink() or not path.is_dir():
        fail('restore_generation_missing', 'The restored installation is missing. The previous state was preserved.')
    if (path / 'state').is_symlink() or not (path / 'state').is_dir():
        fail('restore_generation_invalid', 'The restored state directory needs review.')
    for relative in ('service.json', 'installation.json', 'mesh_passphrase', 'api_token', 'state/bridge.sqlite3', 'state/mqtt-settings.json'):
        member = path / relative
        if member.is_symlink() or not member.is_file() or member.stat().st_mode & 0o077:
            fail('restore_generation_invalid', 'A restored file is missing or has unsafe permissions.')
    return path


def resolve_config(config):
    """Follow a single durable pointer. Never silently fall back to old state."""
    from .service import validate_config
    root = Path(config['state_directory'])
    pending, pointer = root / PENDING, root / POINTER
    committing = pending.exists() or pending.is_symlink()
    if not committing and not pointer.exists() and not pointer.is_symlink():
        return config
    marker = read_json(pending if committing else pointer)
    generation = generation_path(root, marker)
    result = read_json(generation / 'service.json')
    if result['state_directory'] != str(generation / 'state'):
        fail('restore_marker_invalid', 'The restore paths need review.')
    # The original service file remains authoritative for this Pi. Overlay the
    # destination before validation, including for generations made by dev69/70.
    result.update(destination_service(config))
    if not result['commissioning_only'] and result.pop('first_device_bootstrap', False):
        result.pop('automatic_bootstrap', None)
        result['automatic_relay'] = True
    result['api_token_file'] = config['api_token_file']
    result.setdefault('capabilities', {})['removal'] = config.get('capabilities', {}).get('removal', False)
    validate_config(result)
    if committing:
        complete_switch(root, marker, result)
    result['_installation_root'] = str(root)
    return result


@contextmanager
def stopped_lock(directory):
    fd = os.open(Path(directory) / 'bridge.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fail('restore_busy', 'The previous bridge is still using its database. Restore was stopped.')
        yield
    finally:
        os.close(fd)


def complete_switch(root, marker, config):
    """An interrupted authorized pointer commit is idempotent, never a radio retry."""
    generation = generation_path(root, marker)
    from .service import private_text
    inspect_database(read_database(generation / 'state/bridge.sqlite3'), private_text(config['mesh_passphrase_file']))
    previous_marker = read_json(root / POINTER) if (root / POINTER).exists() else None
    previous = generation_path(root, previous_marker) / 'state' if previous_marker else root
    if previous != generation / 'state':
        with stopped_lock(previous):
            read_database(previous / 'bridge.sqlite3')
            with sqlite3.connect(previous / 'bridge.sqlite3') as db:
                db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ('installation_superseded_v1', json.dumps(marker)))
    replace_json(root / POINTER, marker)
    (root / PENDING).unlink(missing_ok=True)
    fsync_dir(root)


def finish_restore(config, plan):
    """Called only after old HTTP/workers/MQTT/Bluetooth/Store have stopped."""
    root = Path(config.get('_installation_root', config['state_directory']))
    current_dir = Path(config['state_directory'])
    with stopped_lock(current_dir):
        current = read_database(current_dir / 'bridge.sqlite3')
        database, report = prepare_restored_database(plan['decoded']['database'],
            backup_digest=plan['digest'], created_at=plan['decoded']['created_at'], current_bytes=current)
        identifier = secrets.token_hex(16)
        parent = root / 'installations'
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.is_symlink() or parent.stat().st_mode & 0o077:
            fail('invalid_installation', 'The restore directory must be private.')
        stage = parent / ('.preparing-' + identifier); final = parent / identifier
        make_installation(stage, final, database, plan['decoded']['settings'], destination_settings(config))
        # Recovery copy uses the owner's supplied encryption password. The old
        # generation also remains intact; no database rollback is automatic.
        archive = encode_backup(current, plan['current_settings'], plan['password'])
        write_private(stage / 'before-restore.halobackup', archive)
        write_json(stage / 'restore-report.json', dict(report, restored_at=now()))
        fsync_dir(stage); os.rename(stage, final); fsync_dir(parent)
        marker = {'format': 1, 'generation': identifier}
        write_json(root / PENDING, marker); fsync_dir(root)
    return resolve_config(dict(config, state_directory=str(root)))


class BackupManager:
    def __init__(self, runtime=None, config=None, restart=None, *, install=None, destination=None, clock=time.monotonic):
        self.runtime, self.config, self.restart = runtime, config, restart
        self.install, self.destination, self.clock = install, destination, clock
        self.lock = asyncio.Lock(); self.review = None; self.expiry = None

    def clear(self):
        self.review = None
        if self.expiry:
            self.expiry.cancel(); self.expiry = None

    def status(self):
        if self.runtime is None:
            return dict(enabled=True, backup_supported=False, restore_supported=True, reason=None,
                current=None, last_backup=None)
        inventory = self.runtime.store.inventory()
        from .bridge import LIGHTS, CONTROLLERS
        return dict(enabled=True, backup_supported=True, restore_supported=True, reason=None,
            current=dict(name=self.config['node'], version=__version__, devices=len(inventory['devices']),
                controllers=sum(d['product_id'] in CONTROLLERS for d in inventory['devices']),
                groups=sum(not g['protected'] for g in inventory['groups']),
                scenes=len(self.runtime.store.get('native_scenes_v1', []))),
            last_backup=self.runtime.store.get('installation_last_backup'))

    async def capture(self):
        if self.runtime is None:
            return None, None
        if getattr(self.runtime, 'maintenance', False):
            fail('restore_in_progress', 'Restore is already restarting this bridge.')
        settings = self.runtime.mqtt_settings
        async with settings._change:
            try:
                await asyncio.wait_for(self.runtime.operation.acquire(), 10)
            except asyncio.TimeoutError:
                fail('backup_busy', 'A device operation is running. Try again when it finishes.')
            try:
                return snapshot_db(self.runtime.store.db), portable_settings(self.runtime, self.config)
            finally:
                self.runtime.operation.release()

    async def create(self, data):
        if not isinstance(data, dict) or set(data) != {'password'}:
            fail('invalid_request', 'Enter a backup password.', 400)
        async with self.lock:
            database, settings = await self.capture()
            if database is None:
                fail('backup_unavailable', 'There is no installation to back up yet.')
            archive = await asyncio.to_thread(encode_backup, database, settings, data['password'])
            decoded = await asyncio.to_thread(decode_backup, archive, data['password'])
            summary = decoded['summary']
            self.runtime.store.set('installation_last_backup', dict(created_at=decoded['created_at'], version=__version__))
            return dict(filename='halo-local-' + decoded['created_at'].replace(':', '-') + '.halobackup',
                        archive=base64.b64encode(archive).decode('ascii'), summary=summary)

    async def preview(self, data):
        if not isinstance(data, dict) or set(data) != {'password', 'archive'}:
            fail('invalid_request', 'Choose a backup file and enter its password.', 400)
        if not isinstance(data['archive'], str) or len(data['archive']) > MAX_HTTP_BYTES:
            fail('backup_too_large', 'The backup file is too large.', 413)
        async with self.lock:
            self.clear()
            try:
                archive = base64.b64decode(data['archive'], validate=True)
            except (ValueError, TypeError):
                fail('invalid_backup', 'This is not a valid HALO Local backup.', 400)
            decoded = await asyncio.to_thread(decode_backup, archive, data['password'])
            check_version(decoded['version'])
            current, settings = await self.capture()
            archive_digest = hashlib.sha256(archive).hexdigest()
            blockers = []
            if self.runtime is not None and decoded['settings']['node'] != self.config['node']:
                blockers.append('This backup belongs to a different bridge identity. Restore it on a fresh installation.')
            try:
                report = await asyncio.to_thread(validate_restored_database, decoded['database'],
                    backup_digest=archive_digest, created_at=decoded['created_at'], current_bytes=current)
            except RestoreStateError as error:
                blockers.append(str(error)); report = None
            mode = 'same_bridge' if self.runtime else 'replacement'
            identifier = secrets.token_urlsafe(24)
            revision = digest(dict(digest=archive_digest, mode=mode, node=self.config['node'] if self.config else None))
            if not blockers:
                self.review = dict(id=identifier, revision=revision, decoded=decoded, digest=archive_digest,
                    password=data['password'], expires=self.clock()+PREVIEW_SECONDS)
                self.expiry = asyncio.get_running_loop().call_later(PREVIEW_SECONDS, self.clear)
            return dict(preview_id=identifier if not blockers else None, revision=revision, expires_in=PREVIEW_SECONDS,
                summary=decoded['summary'], mode=mode, can_restore=not blockers, blockers=blockers,
                warnings=['Changes made after this backup are not included. Later radio history cannot be verified from this file.',
                    'The previous bridge must remain off. Unfinished device operations will remain available for review.',
                    'Home Assistant automations are stored in Home Assistant and are not included in this backup.'])

    async def restore(self, data):
        fields={'preview_id','revision','confirm_old_bridge_stopped','confirm_replace','confirm_backup_history'}
        if (not isinstance(data,dict) or set(data)!=fields or any(data[k] is not True for k in fields if k.startswith('confirm_'))):
            fail('confirmation_required', 'Review the backup and confirm the restore conditions.', 400)
        async with self.lock:
            review=self.review
            if (not review or review['expires']<self.clock() or data['preview_id']!=review['id'] or data['revision']!=review['revision']):
                fail('preview_expired', 'This restore review expired or was already used. Preview the file again.')
            self.clear()  # one use even when a later step fails; never replay apply
            if self.runtime is None:
                await asyncio.to_thread(validate_restored_database, review['decoded']['database'],
                    backup_digest=review['digest'], created_at=review['decoded']['created_at'])
                await self.install(review)
            else:
                runtime = self.runtime
                async with runtime.mqtt_settings._change:
                    try:
                        await asyncio.wait_for(runtime.operation.acquire(), 10)
                    except asyncio.TimeoutError:
                        fail('backup_busy', 'A device operation is still running. Preview the backup again after it finishes.')
                    accepted = False
                    try:
                        if getattr(runtime, 'maintenance', False):
                            fail('restore_in_progress', 'Restore is already restarting this bridge.')
                        runtime.maintenance = True
                        current = snapshot_db(runtime.store.db)
                        review['current_settings'] = portable_settings(runtime, self.config)
                        await asyncio.to_thread(validate_restored_database, review['decoded']['database'],
                            backup_digest=review['digest'], created_at=review['decoded']['created_at'], current_bytes=current)
                        if review['decoded']['settings']['node'] != self.config['node']:
                            fail('different_bridge', 'This backup belongs to a different bridge.')
                        self.restart(review)
                        accepted = True
                    finally:
                        if not accepted:
                            runtime.maintenance = False
                        runtime.operation.release()
            return dict(accepted=True,restart_required=True,device_programming=False,
                        message='Restore accepted. Wait for the bridge to restart, then reload this page.')
