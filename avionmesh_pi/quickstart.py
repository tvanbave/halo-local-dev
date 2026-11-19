"""Trusted-LAN Compose setup with optional login and explicit mesh creation."""
import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import stat
import struct
import sys
import urllib.request
from aiohttp import web

from .backup_manager import trusted_lan_backup_access
from .browser_auth import authorized, challenge, basic_header
from .migration import admit, canonical_digest
from .protocol import derive_mcp_key
from .service import load_config, private_text, regular_text
from .storage import Store


class SetupError(RuntimeError):
    """Fixed actionable text only; never include credential/input values."""


CAPABILITIES = {name: True for name in (
    'ordinary_claim', 'inventory_complete', 'bulk_claim', 'group_membership',
    'group_membership_readback', 'wired_editor', 'wired_onboarding',
    'battery_editor', 'battery_onboarding', 'accessory_onboarding',
    'dimmer_editor', 'anyplace_editor', 'anyplace_onboarding', 'observation_refresh', 'removal')}
CAPABILITIES.update(claim_reset_recovery=False, cloud_sync=False)
SETUP_HTML = (Path(__file__).with_name('web') / 'setup.html').read_text()



def private_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
        return any(address in ipaddress.IPv4Network(n) for n in
                   ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    except (ValueError, TypeError):
        return False


def default_interfaces(text):
    candidates = []
    for line in text.splitlines()[1:]:
        row = line.split()
        if len(row) < 8 or row[1] != '00000000' or row[7] != '00000000':
            continue
        try:
            if int(row[3], 16) & 1:
                candidates.append((int(row[6]), row[0]))
        except ValueError:
            continue
    return [name for _, name in sorted(candidates)]


def interface_address(name):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        result = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack('256s', name.encode()[:15]))
    return socket.inet_ntoa(result[20:24])


def lan_address(override=None):
    if override:
        if not private_ipv4(override):
            raise SetupError('HALO_HOST must be a private LAN IPv4 address assigned to this Pi.')
        return str(ipaddress.IPv4Address(override))
    if sys.platform != 'linux':
        raise SetupError('Live Compose setup requires Linux with Bluetooth. Docker Desktop is for offline tests only.')
    try:
        for name in default_interfaces(Path('/proc/net/route').read_text()):
            if name.startswith(('docker', 'br-', 'veth', 'tun', 'tap', 'wg')):
                continue
            try:
                address = interface_address(name)
            except OSError:
                continue
            if private_ipv4(address):
                return address
    except OSError:
        pass
    raise SetupError('No private LAN address found. Set HALO_HOST to this Pi\'s LAN IPv4 address and use Linux host networking.')


def check_bind(host, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
    except OSError:
        raise SetupError('Cannot bind the bridge address/port. Stop another bridge using it, or set HALO_HOST and HALO_PORT.') from None


async def check_bluetooth(adapter):
    """Read adapter inventory only; never scan, pair or change adapter power."""
    from dbus_fast import BusType, Message, MessageType
    from dbus_fast.aio import MessageBus
    bus = None
    try:
        bus = MessageBus(bus_type=BusType.SYSTEM)
        await asyncio.wait_for(bus.connect(), 8)
        reply = await asyncio.wait_for(bus.call(Message(destination='org.bluez', path='/',
            interface='org.freedesktop.DBus.ObjectManager', member='GetManagedObjects')), 8)
        if reply.message_type == MessageType.ERROR:
            raise SetupError('BlueZ is unavailable or D-Bus access was denied. Check the selected Bluetooth service logs and its D-Bus policy.')
        props = reply.body[0].get('/org/bluez/' + adapter, {}).get('org.bluez.Adapter1')
        if props is None:
            raise SetupError('Bluetooth adapter not found. Attach an adapter or set HALO_ADAPTER (default hci0).')
        if not props.get('Powered') or props['Powered'].value is not True:
            raise SetupError('Bluetooth is powered off. Enable Bluetooth on the Pi (check rfkill) and restart the container.')
    except SetupError:
        raise
    except (BrokenPipeError, ConnectionResetError, EOFError):
        # A system bus hangs up, without an error reply, on a user ID it cannot look up on its host.
        uid, gid = os.getuid(), os.getgid()
        raise SetupError(f'The system message bus closed the connection. It does this when the bridge user (ID {uid}) '
            'does not exist on the host. Create it on the host, then restart the bridge: '
            f'sudo groupadd --gid {gid} halo-local; '
            f'sudo useradd --uid {uid} --gid {gid} --no-create-home --shell /usr/sbin/nologin halo-local') from None
    except Exception as error:
        raise SetupError(f'Cannot access Bluetooth ({type(error).__name__}). Check the selected Compose stack, '
            'Bluetooth service logs and D-Bus socket mount.') from None
    finally:
        if bus is not None:
            bus.disconnect()
            try:
                await asyncio.wait_for(bus.wait_for_disconnect(), 2)
            except asyncio.CancelledError:
                raise  # Do not carry on starting after a stop request.
            except Exception:
                pass


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def write_json(path, data):
    write_new(path, json.dumps(data, indent=2) + '\n')


@contextmanager
def installation_lock(directory):
    directory = Path(directory)
    if not directory.is_absolute():
        raise SetupError('The data directory must be an absolute path.')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077:
        raise SetupError('The data directory must be private (0700) and owned by the container user.')
    fd = os.open(directory / 'startup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SetupError('Another bridge already owns this installation.') from None
        yield directory
    finally:
        os.close(fd)


def password(directory, *, show_login=False):
    path = directory / 'ui-password'
    if path.exists() or path.is_symlink():
        value = private_text(path)
        if len(value) < 32:
            raise SetupError('Saved login credential is invalid; automatic replacement was refused.')
        return value
    if set(p.name for p in directory.iterdir()) != {'startup.lock'}:
        raise SetupError('Existing data has no login credential. Preserve the volume; automatic initialization was refused.')
    value = secrets.token_urlsafe(32)
    write_new(path, value + '\n')
    sync_directory(directory)
    if show_login:
        print('First-run login: username halo; password ' + value, flush=True)
    return value


def initialize(directory, host, port, adapter, *, imported=None, browser_login=False):
    """Only the confirmed setup POST may call this function."""
    if type(browser_login) is not bool:
        raise SetupError('Expected a boolean browser login setting.')
    if set(p.name for p in directory.iterdir()) != {'startup.lock', 'ui-password'}:
        raise SetupError('Setup already exists or was interrupted. Preserve the volume; no initialization was repeated.')
    final, stage = directory / 'bridge', directory / '.initializing'
    stage.mkdir(mode=0o700)
    passphrase = imported['passphrase'] if imported is not None else secrets.token_urlsafe(32)
    write_new(stage / 'mesh_passphrase', passphrase + '\n')
    key = derive_mcp_key(passphrase)
    store = Store(stage / 'state', key)
    try:
        identity = {'format': 1, 'state_id': store.get('state_id'),
                    'mesh_fingerprint': store.get('mesh_fingerprint'), 'node': 'halo_' + secrets.token_hex(8)}
        if imported is None:
            # This key is generated here before any transmitter can use it.
            admit(store, dict(format=1, state_id=identity['state_id'],
                mesh_fingerprint=identity['mesh_fingerprint'], source=0x8000, mode='fresh_mesh',
                high_water_mark=0, previous_writers_stopped=True,
                evidence_sha256=canonical_digest(dict(identity, key_generated_here=True)),
                fresh_mesh=True, history_complete=True, evidence_kind='new_mesh_key'))
        else:
            from .migration import admit_cloud_import
            admit_cloud_import(store, location_id=imported['location']['id'],
                               inventory_digest=canonical_digest(imported['inventory']))
            store.import_inventory(imported['inventory'])
            store.set('cloud_location', imported['location'])
            from .cloud_provenance import retain_cloud_devices
            retain_cloud_devices(store, imported['inventory']['devices'], imported['location']['id'])
            identity['origin'] = 'halo_cloud_import'

    finally:
        store.close()
    config = {'relay_address': None, 'node': identity['node'], 'state_directory': str(final / 'state'),
              'mesh_passphrase_file': str(final / 'mesh_passphrase'), 'api_token_file': str(directory / 'ui-password'),
              'http_host': host, 'http_port': port, 'adapter': adapter, 'commissioning_only': True, 'mqtt': None,
              'first_device_bootstrap': True, 'automatic_bootstrap': True,
              'browser_login': browser_login, 'trusted_lan_no_auth': not browser_login,
              'trusted_lan_backups': trusted_lan_backup_access(default=True),
              'allow_lan_http': True, 'capabilities': CAPABILITIES.copy()}
    if imported is not None:
        config.update(first_device_bootstrap=False, automatic_bootstrap=False, automatic_relay=True)
        config['capabilities']['cloud_sync'] = True
    write_json(stage / 'service.json', config)
    write_json(stage / 'installation.json', identity)
    sync_directory(stage / 'state')
    sync_directory(stage)
    os.rename(stage, final)
    sync_directory(directory)
    return config


def restore_installation(directory, host, port, adapter, token, review, *, browser_login=False):
    """First-run restore into an empty installation; keep all device pairings."""
    from .backup_manager import make_installation
    from .restore_state import prepare_restored_database
    if set(p.name for p in directory.iterdir()) != {'startup.lock', 'ui-password'}:
        raise SetupError('Setup already exists or was interrupted. Saved data was preserved.')
    database, report = prepare_restored_database(review['decoded']['database'],
        backup_digest=review['digest'], created_at=review['decoded']['created_at'])
    final, stage = directory / 'bridge', directory / '.initializing'
    config = make_installation(stage, final, database, review['decoded']['settings'],
        dict(api_token=token, service=dict(http_host=host, http_port=port, adapter=adapter,
            browser_login=browser_login, trusted_lan_no_auth=not browser_login, allow_lan_http=True,
            trusted_lan_backups=trusted_lan_backup_access(default=True), commissioning_only=True, mqtt=None)))
    # Managed installations keep their destination browser credential location.
    config['api_token_file'] = str(directory / 'ui-password')
    replacement = stage / 'service.restore.json'
    write_json(replacement, config)
    os.replace(replacement, stage / 'service.json')
    write_json(stage / 'restore-report.json', report)
    sync_directory(stage)
    os.rename(stage, final)
    sync_directory(directory)
    return config


def removal_setting(value):
    """An explicit Compose opt-in/out; an unset value preserves saved settings."""
    if value in (None, ''): return None
    if value == 'true': return True
    if value == 'false': return False
    raise SetupError('Set HALO_REMOVAL to true or false, or leave it empty to keep the saved setting.')


def browser_login_setting(value):
    """Fresh installs default off; an unset override preserves existing access mode."""
    if value in (None, ''): return None
    if value == 'true': return True
    if value == 'false': return False
    raise SetupError('Set HALO_BROWSER_LOGIN to true or false, or leave it empty to keep the saved setting.')


def existing_config(directory, host, port, adapter, *, removal=None, browser_login=None):
    """Never create a database to replace missing durable transmit history."""
    if removal is not None and type(removal) is not bool:
        raise SetupError('Expected a boolean removal setting.')
    if browser_login is not None and type(browser_login) is not bool:
        raise SetupError('Expected a boolean browser login setting.')
    final = directory / 'bridge'
    try:
        for path in (final, final / 'state'):
            if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
                raise ValueError('Invalid directory')
        for name in ('installation.json', 'service.json', 'mesh_passphrase', 'state/bridge.sqlite3'):
            info = (final / name).lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError('Invalid private file')
        identity = json.loads(regular_text(final / 'installation.json', private=True, limit=4096))
        config = load_config(final / 'service.json')
        if identity.get('format') != 1 or config['node'] != identity['node'] or not (config.get('browser_login') is True or config.get('trusted_lan_no_auth') is True):
            raise ValueError('Identity/access mode mismatch')
        for name, path in (('state_directory', final / 'state'), ('mesh_passphrase_file', final / 'mesh_passphrase'), ('api_token_file', directory / 'ui-password')):
            if config[name] != str(path):
                raise ValueError('Managed paths changed')
        fingerprint = hashlib.sha256(derive_mcp_key(private_text(final / 'mesh_passphrase'))).hexdigest()
        if fingerprint != identity['mesh_fingerprint']:
            raise ValueError('Mesh identity mismatch')
        db = sqlite3.connect((final / 'state/bridge.sqlite3').as_uri() + '?mode=ro', uri=True)
        try:
            metadata = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM metadata')}
        finally:
            db.close()
        if (metadata.get('state_id') != identity['state_id'] or metadata.get('mesh_fingerprint') != fingerprint
                or not metadata.get('tx_admission') or metadata.get('tx_retirement')
                or type(metadata.get('sequence')) is not int or not 0 <= metadata['sequence'] < 0xffffff):
            raise ValueError('Stored transmit state is not usable')
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        raise SetupError('Saved installation is incomplete, changed or retired. Preserve this volume; no new key or transmit history was created.') from None
    config.update(http_host=host, http_port=port, adapter=adapter)
    if browser_login is not None:
        config.update(browser_login=browser_login, trusted_lan_no_auth=not browser_login)
    if removal is not None:
        config.setdefault('capabilities', {})['removal'] = removal
    path = final / 'service.json'
    if json.loads(regular_text(path)) != config:
        pending = final / 'service.next.json'
        if pending.exists() or pending.is_symlink():
            raise SetupError('An interrupted configuration update needs review; saved state was preserved.')
        write_json(pending, config)
        os.replace(pending, path)
        sync_directory(final)
    return load_config(path)


def setup_app(directory, host, port, adapter, token, ready, *, cloud_setup=None, browser_login=False):
    from .server import _trusted_lan_host
    from .cloud_setup import CloudSetup, ImportSetupError
    from .backup_manager import BackupManager, BackupManagerError, MAX_HTTP_BYTES
    from .installation_backup import InstallationBackupError
    from .restore_state import RestoreStateError
    cloud_setup = cloud_setup or CloudSetup()
    allowed_host = _trusted_lan_host(host + ':' + str(port))
    async def install_backup(review):
        await asyncio.to_thread(restore_installation, directory, host, port, adapter, token, review,
                                browser_login=browser_login)
        ready.set()
    backups = BackupManager(install=install_backup)
    @web.middleware
    async def guard(request, handler):
        if request.headers.getall('Host', []) != [allowed_host]:
            return web.json_response({'error': 'invalid_host'}, status=403)
        if request.headers.get('Sec-Fetch-Site') == 'cross-site' or request.headers.getall('Origin', []) not in ([], ['http://' + allowed_host]):
            return web.json_response({'error': 'invalid_origin'}, status=403)
        if browser_login and not authorized(request.headers.get('Authorization', ''), token):
            return challenge()
        if request.path != '/api/backup/preview':
            request._client_max_size = 32768
        if request.path != '/api/backup/preview' and request.content_length and request.content_length > 32768:
            raise web.HTTPRequestEntityTooLarge(max_size=32768, actual_size=request.content_length)
        if request.method == 'POST' and request.content_type != 'application/json':
            return web.json_response({'error': 'json_required'}, status=415)
        try:
            response = await handler(request)
        except (BackupManagerError, InstallationBackupError, RestoreStateError) as error:
            response = web.json_response({'error': error.code, 'message': str(error)}, status=getattr(error, 'status', 400))
        except ImportSetupError as error:
            response = web.json_response({'error': error.code, 'message': str(error)}, status=409)
        except (ValueError, UnicodeError):
            response = web.json_response({'error': 'invalid_request'}, status=400)
        response.headers.update({'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff'})
        return response
    app = web.Application(middlewares=[guard], client_max_size=MAX_HTTP_BYTES)
    async def home(request):
        return web.Response(text=SETUP_HTML, content_type='text/html')
    async def create(request):
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            return web.json_response({'error': 'invalid_request'}, status=400)
        if data != {'create_new_mesh': True} or type(data.get('create_new_mesh')) is not bool:
            return web.json_response({'error': 'confirmation_required'}, status=400)
        try:
            initialize(directory, host, port, adapter, browser_login=browser_login)
        except SetupError as exc:
            print('Setup: ' + str(exc), file=sys.stderr, flush=True)
            return web.json_response({'error': 'setup_incomplete_preserved'}, status=409)
        except (OSError, ValueError, RuntimeError):
            print('Setup could not be saved. Preserve the volume and check storage permissions/free space.', file=sys.stderr, flush=True)
            return web.json_response({'error': 'setup_incomplete_preserved'}, status=409)
        ready.set()
        return web.json_response({'created': True}, status=201)
    def empty_installation():
        if set(p.name for p in directory.iterdir()) != {'startup.lock', 'ui-password'}:
            cloud_setup.cancel()
            raise ImportSetupError('invalid_request')

    async def cloud_action(request):
        empty_installation()
        data = await request.json()
        if not isinstance(data, dict):
            raise ImportSetupError('invalid_request')
        action = request.match_info['action']
        if action == 'login' and set(data) == {'email', 'password'}:
            result = await cloud_setup.login(data['email'], data['password'])
        elif action == 'preview' and set(data) == {'session', 'location'}:
            result = await cloud_setup.preview(data['session'], data['location'])
        elif action == 'cancel' and data == {}:
            cloud_setup.cancel()
            return web.json_response({'cancelled': True})
        elif (action == 'apply' and set(data) == {'session', 'preview', 'enable_local_control'}
              and data['enable_local_control'] is True):
            try:
                await cloud_setup.apply(data['session'], data['preview'],
                    lambda plan: initialize(directory, host, port, adapter, imported=plan, browser_login=browser_login))
            except ImportSetupError:
                raise
            except (OSError, ValueError, RuntimeError):
                return web.json_response({'error':'setup_incomplete_preserved',
                    'message':'Import could not be confirmed. Preserve the data volume and reload; it will not be repeated automatically.'}, status=409)
            ready.set()
            return web.json_response({'imported':True, 'local_control':True, 'repaired_devices':False}, status=201)
        else:
            raise ImportSetupError('invalid_request')
        return web.json_response(result)

    async def backup_action(request):
        if request.method == 'GET':
            return web.json_response(dict(backups.status(), owner_password_required=not browser_login and not backups.trusted_lan_backups))
        if not browser_login and not backups.trusted_lan_backups and not authorized(request.headers.get('Authorization', ''), token):
            from .browser_auth import backup_owner_refused
            return backup_owner_refused()
        empty_installation()
        action = request.match_info['action']
        try:
            return web.json_response(await getattr(backups, action)(await request.json()))
        except SetupError:
            return web.json_response({'error':'setup_incomplete_preserved',
                'message':'Restore could not finish. Saved files were preserved; reload before any other setup.'}, status=409)

    async def asset(request):
        filename, kind = {'/setup.js': ('setup.js', 'application/javascript'),
                          '/setup.css': ('setup.css', 'text/css'), '/backup.js': ('backup.js', 'text/javascript')}[request.path]
        return web.Response(text=(Path(__file__).with_name('web') / filename).read_text(), content_type=kind)

    async def cleanup(app):
        cloud_setup.cancel()
        backups.clear()
    app.on_cleanup.append(cleanup)
    app.router.add_get('/', home)
    app.router.add_get('/setup.js', asset)
    app.router.add_get('/setup.css', asset)
    app.router.add_get('/backup.js', asset)
    app.router.add_get('/api/backup/{action:status}', backup_action)
    app.router.add_post('/api/backup/{action:preview|restore}', backup_action)
    app.router.add_post('/api/setup', create)
    app.router.add_post('/api/setup/cloud/{action}', cloud_action)
    return app


async def start(directory, *, host=None, port=18089, adapter='hci0'):
    from .service import serve
    removal = removal_setting(os.environ.get('HALO_REMOVAL'))
    browser_login = browser_login_setting(os.environ.get('HALO_BROWSER_LOGIN'))
    address = lan_address(host)
    if type(port) is not int or not 1 <= port <= 65535 or not re.fullmatch(r'hci[0-9]+', adapter):
        raise SetupError('Use HALO_PORT from 1 to 65535 and HALO_ADAPTER such as hci0.')
    check_bind(address, port)
    await check_bluetooth(adapter)
    with installation_lock(directory) as locked:
        token = password(locked, show_login=browser_login is True)
        print('HALO Local: open http://' + address + ':' + str(port), flush=True)
        final = locked / 'bridge'
        if not final.exists() and not final.is_symlink():
            if set(p.name for p in locked.iterdir()) != {'startup.lock', 'ui-password'}:
                raise SetupError('Setup was interrupted. Preserve the data volume for recovery; no setup was repeated.')
            ready = asyncio.Event()
            runner = web.AppRunner(setup_app(locked, address, port, adapter, token, ready, browser_login=browser_login is True), access_log=None)
            try:
                await runner.setup()
                await web.TCPSite(runner, address, port).start()
                print(('Sign in' if browser_login is True else 'Open the page') +
                      ' to create a new mesh or import your HALO Home setup.', flush=True)
                await ready.wait()
            finally:
                await runner.cleanup()
        config = existing_config(locked, address, port, adapter, removal=removal, browser_login=browser_login)
        await serve(config)


def healthcheck(directory):
    directory = Path(directory)
    config_path = directory / 'bridge/service.json'
    if config_path.exists():
        config = load_config(config_path)
        host, port = config['http_host'], config['http_port']
        browser_login = config.get('browser_login') is True
    else:
        host, port = lan_address(os.environ.get('HALO_HOST')), int(os.environ.get('HALO_PORT', '18089'))
        browser_login = browser_login_setting(os.environ.get('HALO_BROWSER_LOGIN')) is True
    headers = {}
    if browser_login:
        headers['Authorization'] = basic_header('halo', private_text(directory / 'ui-password'))
    from .server import _trusted_lan_host
    headers['Host'] = _trusted_lan_host(host + ':' + str(port))
    request = urllib.request.Request('http://' + host + ':' + str(port) + '/', headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        if response.status != 200:
            raise SetupError('The bridge UI is not responding.')
