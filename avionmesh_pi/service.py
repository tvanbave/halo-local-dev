"""Explicit opt-in live service entry. Never called by replay or unit fixtures."""
import asyncio
import json
import os
from pathlib import Path
import re
import stat
import ipaddress
import logging
import sys

from .bluez import BlueZTransport
from .mqtt import MqttService
from .mqtt_settings import MqttSettings
from .protocol import derive_mcp_key
from .runtime import Runtime
from .storage import Store


SHUTDOWN_TIMEOUTS = {'http': 5, 'workers': 3, 'mqtt': 2, 'bluetooth': 5}
_LOG = logging.getLogger(__name__)


async def _shutdown_service(runner, tasks, settings, transport, store):
    """Give every owner a cleanup chance, without exposing exception contents."""
    for task in tasks:
        task.cancel()
    complete = True

    async def finish(stage, work):
        nonlocal complete
        try:
            await asyncio.wait_for(work, SHUTDOWN_TIMEOUTS[stage])
        except asyncio.TimeoutError:
            complete = False
            _LOG.warning('service_shutdown_%s_timeout', stage)
        except asyncio.CancelledError:
            complete = False
            # This task is shielded from process/request cancellation. An owner
            # that finishes by cancelling itself must not skip the other owners.
            _LOG.warning('service_shutdown_%s_cancelled', stage)
        except Exception:
            complete = False
            _LOG.warning('service_shutdown_%s_failed', stage)

    try:
        if runner is not None:
            await finish('http', runner.cleanup())
        await finish('workers', asyncio.gather(*tasks, return_exceptions=True))
        if settings is not None:
            await finish('mqtt', settings.close())
        if transport is not None:
            await finish('bluetooth', transport.disconnect())
    finally:
        store.close()
    return complete


async def _protected_shutdown(runner, tasks, settings, transport, store):
    # Additional cancellation must not interrupt a journal's uncertainty cleanup
    # or skip the owned Bluetooth disconnect. The first cancellation is re-raised
    # by serve's finally; a cancellation first received here is propagated too.
    primary_error = sys.exc_info()[0] is not None
    cleanup = asyncio.create_task(_shutdown_service(runner, tasks, settings, transport, store))
    cancelled = False
    complete = False
    while True:
        try:
            complete = await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError:
            if cleanup.cancelled():
                raise
            cancelled = True
        except Exception:
            if not primary_error:
                raise
            _LOG.warning('service_shutdown_finalization_failed')
            break
    if cancelled and not primary_error:
        raise asyncio.CancelledError
    return complete


def regular_text(path, *, private=False, limit=65536):
    # O_NONBLOCK prevents a malicious FIFO from blocking before fstat rejects it.
    fd=os.open(path,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size>limit:
            raise ValueError('Expected a bounded regular file')
        if private and info.st_mode & 0o077:
            raise ValueError('Secret file must be private to its owner')
        data=bytearray()
        while len(data)<=limit:
            chunk=os.read(fd,min(4096,limit+1-len(data)))
            if not chunk:break
            data.extend(chunk)
        if len(data)>limit:raise ValueError('Input file too large')
        return data.decode('utf-8')
    finally:
        os.close(fd)


def private_text(path):
    value=regular_text(path,private=True,limit=4096).strip()
    if not value:raise ValueError('Secret must be nonempty')
    return value


def load_config(path):
    return validate_config(json.loads(regular_text(path)))


def validate_config(config):
    required = {"relay_address", "node", "state_directory", "mesh_passphrase_file", "api_token_file", "mqtt"}
    optional = {"adapter", "http_host", "http_port", "capabilities", "commissioning_only", "allow_lan_http", "first_device_bootstrap", "automatic_bootstrap", "automatic_relay", "trusted_lan_no_auth", "browser_login"}
    if not isinstance(config, dict) or not required <= set(config) or set(config) - required - optional:
        raise ValueError("Invalid service configuration")
    def text(value, maximum=4096):
        return isinstance(value,str) and 0<len(value)<=maximum and not any(ord(c)<32 for c in value)
    def absolute_path(value):
        return text(value) and Path(value).is_absolute()
    def port(value):
        return type(value) is int and 1<=value<=65535
    bootstrap = bootstrap_mode(config)
    automatic = automatic_bootstrap_mode(config)
    automatic_relay = automatic_relay_mode(config)
    no_auth = trusted_lan_mode(config)
    browser_login = browser_login_mode(config)
    if not (bootstrap or automatic_relay) and (not text(config['relay_address']) or not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", config["relay_address"])):
        raise ValueError("Invalid relay address")
    if not text(config['node']) or not re.fullmatch(r"[a-z0-9_]{1,40}", config["node"]):
        raise ValueError("Invalid node name")
    for key in ('state_directory','mesh_passphrase_file','api_token_file'):
        if not absolute_path(config[key]):raise ValueError('Configuration paths must be absolute')
    if 'adapter' in config and (not text(config['adapter']) or not re.fullmatch(r'hci[0-9]+',config['adapter'])):
        raise ValueError('Invalid BlueZ adapter')
    if 'http_port' in config and not port(config['http_port']):raise ValueError('Invalid HTTP port')
    if 'http_host' in config:
        host=config['http_host']
        if not text(host):raise ValueError('Invalid HTTP bind address')
        if host!='localhost':ipaddress.ip_address(host)
    commissioning_only = commissioning_mode(config)
    broker = {} if commissioning_only else config["mqtt"]
    if not isinstance(broker, dict) or (not commissioning_only and "host" not in broker) or set(broker) - {"host", "port", "username", "password_file", "tls", "ca_file"}:
        raise ValueError("Invalid broker configuration")
    if not commissioning_only and (not text(broker['host'],253) or any(c.isspace() or c in '/@\\' for c in broker['host'])):
        raise ValueError('Expected MQTT hostname or address, not a URL')
    if 'port' in broker and not port(broker['port']):raise ValueError('Invalid broker port')
    if 'username' in broker and not text(broker['username'],256):raise ValueError('Invalid broker username')
    for key in ('password_file','ca_file'):
        if key in broker and not absolute_path(broker[key]):raise ValueError('Broker file paths must be absolute')
    if "tls" in broker and type(broker["tls"]) is not bool:
        raise ValueError("Invalid TLS setting")
    caps = config.get("capabilities", {})
    if not isinstance(caps, dict) or set(caps) - {"ordinary_claim", "bulk_claim", "claim_reset_recovery", "wired_editor", "battery_editor", "inventory_complete", "removal", "wired_onboarding", "battery_onboarding", "accessory_onboarding", "group_membership", "group_membership_readback", "dimmer_editor", "anyplace_editor", "anyplace_onboarding", "cloud_sync", "native_scenes", "observation_refresh", "startup_light_read"} or any(type(v) is not bool for v in caps.values()):
        raise ValueError("Invalid capability configuration")
    # Read credentials only after the complete public configuration validates.
    if "password_file" in broker:
        broker["password"] = private_text(broker.pop("password_file"))
    return config


def commissioning_mode(config):
    value = config.get('commissioning_only', False)
    lan = config.get('allow_lan_http', False)
    if type(lan) is not bool:
        raise ValueError('allow_lan_http must be boolean')
    if type(value) is not bool:
        raise ValueError('commissioning_only must be boolean')
    if value:
        if config.get('mqtt') is not None:
            raise ValueError('Commissioning-only mode requires mqtt:null')
        host = config.get('http_host', '127.0.0.1')
        if host not in ('127.0.0.1', '::1', 'localhost'):
            address = ipaddress.ip_address(host)
            private_lan = address.version == 4 and any(address in ipaddress.ip_network(network) for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
            if not lan or not private_lan:
                raise ValueError('Commissioning LAN HTTP requires explicit opt-in and a private IPv4 bind')
    return value


def bootstrap_mode(config):
    value=config.get('first_device_bootstrap',False)
    if type(value) is not bool:raise ValueError('first_device_bootstrap must be boolean')
    if value and (config.get('commissioning_only') is not True or config.get('relay_address') is not None):
        raise ValueError('First-device bootstrap requires commissioning mode and relay_address:null')
    return value


def automatic_bootstrap_mode(config):
    value = config.get('automatic_bootstrap', False)
    if type(value) is not bool:
        raise ValueError('automatic_bootstrap must be boolean')
    if value and not bootstrap_mode(config):
        raise ValueError('Automatic bootstrap requires first-device bootstrap configuration')
    return value


def automatic_relay_mode(config):
    value = config.get('automatic_relay', False)
    if type(value) is not bool:
        raise ValueError('automatic_relay must be boolean')
    if value and (config.get('relay_address') is not None or bootstrap_mode(config)):
        raise ValueError('Automatic relay requires relay_address:null without first-device bootstrap')
    return value


def trusted_lan_mode(config):
    value = config.get('trusted_lan_no_auth', False)
    if type(value) is not bool:
        raise ValueError('trusted_lan_no_auth must be boolean')
    if value:
        if config.get('allow_lan_http') is not True:
            raise ValueError('Token-free mode requires explicit trusted-LAN opt-in')
        address = ipaddress.ip_address(config.get('http_host', ''))
        if address.version != 4 or not any(address in ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):
            raise ValueError('Token-free mode requires an explicit private LAN IPv4 address')
    return value


def browser_login_mode(config):
    value = config.get('browser_login', False)
    if type(value) is not bool:
        raise ValueError('browser_login must be boolean')
    if value:
        if config.get('trusted_lan_no_auth') is True or config.get('allow_lan_http') is not True:
            raise ValueError('Browser login requires authenticated LAN access')
        from .server import _trusted_lan_host
        _trusted_lan_host(str(config.get('http_host', '')) + ':' + str(config.get('http_port', 8088)))
    return value


class _RestoreRestart(Exception):
    pass


async def serve(config):
    from .backup_manager import resolve_config, finish_restore
    current = resolve_config(config)
    while True:
        event = asyncio.Event()
        requested = []
        def restart(plan):
            if requested:
                raise RuntimeError('Restore already requested')
            requested.append(plan)
            event.set()
        try:
            await _serve_once(current, restart, event)
            return
        except _RestoreRestart:
            # _serve_once's protected finally has closed every live owner.
            # A failed install leaves preserved files and stops startup; it is
            # never permission to reactivate old state or replay device writes.
            current = finish_restore(current, requested.pop())


async def _serve_once(config, restart, restart_event):
    from aiohttp import web
    from .server import create_app, KEYPAD_SERVICE_KEY
    commissioning_only = commissioning_mode(config)
    bootstrap = bootstrap_mode(config)
    automatic = automatic_bootstrap_mode(config)
    automatic_relay = automatic_relay_mode(config)
    no_auth = trusted_lan_mode(config)
    browser_login = browser_login_mode(config)
    os.umask(0o077)
    key = derive_mcp_key(private_text(config["mesh_passphrase_file"]))
    token = None if no_auth else private_text(config["api_token_file"])
    store = Store(config["state_directory"], key)
    runner=transport=runtime=settings=None
    tasks=[]
    try:
        store.require_tx_ready()  # Fail before any HTTP, broker or Bluetooth startup.
        if bootstrap:
            from .bootstrap import DeferredBlueZTransport, BootstrapSelection
            if store.get('tx_admission', {}).get('document', {}).get('mode') != 'fresh_mesh':
                raise ValueError('First-device bootstrap requires fresh-mesh admission')
            if automatic:
                from .bootstrap import AutoBlueZTransport, AutomaticBootstrap
                transport = AutoBlueZTransport(adapter=config.get('adapter','hci0'))
            else:
                if store.get('bootstrap_relay_address') and store.get('bootstrap_owner_reset_declared') is not True:
                    raise ValueError('Retained first-device selection lacks owner declaration')
                transport = DeferredBlueZTransport(store.get('bootstrap_relay_address'), config.get('adapter','hci0'))
        elif automatic_relay:
            from .bootstrap import AutoBlueZTransport
            transport = AutoBlueZTransport(adapter=config.get("adapter", "hci0"))
        else:
            transport = BlueZTransport(config["relay_address"], config.get("adapter", "hci0"))
        runtime = Runtime(store, key, transport, node=config["node"])
        if hasattr(transport, 'preferred_relays'):
            from .bootstrap import paired_relay_addresses
            transport.preferred_relays = lambda: paired_relay_addresses(store)
        runtime.capabilities = config.get("capabilities", {})
        if bootstrap:runtime.bootstrap = AutomaticBootstrap(runtime) if automatic else BootstrapSelection(runtime)
        # A Settings save is an explicit MQTT opt-in. Its private durable
        # override is independent of the initial commissioning/access mode.
        settings = MqttSettings(runtime, config['state_directory'],
                                initial_config=None if commissioning_only else config['mqtt'])
        runtime.mqtt_settings = settings
        from .backup_manager import BackupManager
        runtime.backup_manager = BackupManager(runtime, config, restart)
        app_options = {"trusted_lan_no_auth": True, "allowed_host": str(config["http_host"])+":"+str(config.get("http_port",8088))} if no_auth else {}
        if browser_login:
            app_options = {"browser_login": True, "allowed_host": str(config["http_host"])+":"+str(config.get("http_port",8088))}
        app = create_app(runtime, token, **app_options)
        from .keypad_refresh import WiredKeypadRefresh
        runtime.keypad_binding_refresher = WiredKeypadRefresh(runtime, app[KEYPAD_SERVICE_KEY])
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, config.get("http_host", "127.0.0.1"), config.get("http_port", 8088))
        await site.start()
        async def restore_requested():
            await restart_event.wait()
            await asyncio.sleep(0.5)  # permit the accepted HTTP response to flush
            raise _RestoreRestart()
        tasks = [asyncio.create_task(runtime.bluetooth_loop()), asyncio.create_task(settings.run()),
                 asyncio.create_task(restore_requested())]
        # Existing opt-out remains quiet unless refresh is explicitly enabled.
        refresh = runtime.capabilities.get('observation_refresh',
            runtime.capabilities.get('startup_light_read') is not False)
        if refresh:
            tasks.append(asyncio.create_task(runtime.observation_refresh(
                broadcast_startup=runtime.capabilities.get('startup_light_read') is not False)))
        elif runtime.capabilities.get('startup_light_read') is not False:
            tasks.append(asyncio.create_task(runtime.startup_read(broadcast_startup=True)))
        # Unlike gather(), wait() does not cancel/join children before entering
        # our finally when the service receives SIGTERM. Cleanup admission and
        # its budgets therefore start immediately, even during MQTT teardown.
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()
    finally:
        if runtime is not None:
            runtime.mesh_ready=False
            if hasattr(runtime, 'backup_manager'):
                runtime.backup_manager.clear()
        clean = await _protected_shutdown(runner, tasks, settings, transport, store)
        if restart_event.is_set() and clean is not True:
            raise RuntimeError('Restore stopped because service shutdown could not be confirmed')
