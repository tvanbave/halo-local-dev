"""Optional BlueZ/private D-Bus supervisor. Never changes host services or mesh state."""
import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import signal
import stat
import uuid

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

RUNTIME = Path('/run/halo-bluetooth')
PRIVATE_BUS = 'unix:path=/run/halo-bluetooth/bus'
HOST_BUS = 'unix:path=/host-dbus/system_bus_socket'
BUS_CONFIG = '/etc/halo-bluetooth/bus.conf'
BLUEZ_CONFIG = '/etc/halo-bluetooth/main.conf'


class BluetoothError(RuntimeError):
    """Actionable messages without raw D-Bus bodies or device identifiers."""


def adapter_name():
    name = os.environ.get('HALO_ADAPTER', 'hci0')
    if re.fullmatch(r'hci[0-9]+', name) is None:
        raise BluetoothError('HALO_ADAPTER must be an adapter name such as hci0.')
    return name


async def close_bus(bus):
    bus.disconnect()
    try:
        await asyncio.wait_for(bus.wait_for_disconnect(), 1)
    except asyncio.CancelledError:
        raise  # A stop request must reach the caller, or the supervisor cannot finish shutting down.
    except Exception:
        pass


async def call(address, message):
    bus = MessageBus(bus_address=address)
    try:
        await asyncio.wait_for(bus.connect(), 2)
        reply = await asyncio.wait_for(bus.call(message), 2)
        if reply.message_type == MessageType.ERROR:
            raise BluetoothError('D-Bus request failed; check Bluetooth logs and socket permissions.')
        return reply.body
    finally:
        await close_bus(bus)


async def require_host_free():
    # NameHasOwner never activates org.bluez. Do not contact any host adapter API.
    try:
        body = await call(HOST_BUS, Message(destination='org.freedesktop.DBus',
            path='/org/freedesktop/DBus', interface='org.freedesktop.DBus',
            member='NameHasOwner', signature='s', body=['org.bluez']))
    except Exception:
        raise BluetoothError('Cannot check host Bluetooth ownership. The host /run/dbus '
                             'directory must be mounted read-only at /host-dbus.') from None
    if body != [False]:
        raise BluetoothError('Host BlueZ is running. Stop and mask bluetooth.service on this '
                             'dedicated host, or use compose.yaml for host Bluetooth.')


async def adapter_properties(adapter):
    body = await call(PRIVATE_BUS, Message(destination='org.bluez', path='/',
        interface='org.freedesktop.DBus.ObjectManager', member='GetManagedObjects'))
    return body[0].get('/org/bluez/' + adapter, {}).get('org.bluez.Adapter1')


def powered(props):
    return bool(props and props.get('Powered') and props['Powered'].value is True)


async def prepare_adapter(adapter, attempts=60):
    # Discovery of local kernel adapters only. No BLE scan, device pairing or reset.
    props = None
    for _ in range(attempts):
        await require_host_free()
        try:
            props = await adapter_properties(adapter)
        except Exception:
            props = None
        if props is not None:
            break
        await asyncio.sleep(.5)
    if props is None:
        raise BluetoothError('Bluetooth adapter not found. Check HALO_ADAPTER, Linux driver/firmware '
                             'and the host Bluetooth hardware. Docker Desktop is not supported.')
    if not powered(props):
        await require_host_free()
        try:
            # One power request only. An uncertain write is not replayed in this process.
            await call(PRIVATE_BUS, Message(destination='org.bluez', path='/org/bluez/' + adapter,
                interface='org.freedesktop.DBus.Properties', member='Set', signature='ssv',
                body=['org.bluez.Adapter1', 'Powered', Variant('b', True)]))
        except Exception:
            raise BluetoothError('Could not enable Bluetooth. Check host rfkill, adapter firmware '
                                 'and NET_ADMIN/NET_RAW permissions; no scan or pairing was attempted.') from None
    for _ in range(10):
        await require_host_free()
        if powered(await adapter_properties(adapter)):
            return
        await asyncio.sleep(.5)
    raise BluetoothError('Bluetooth power could not be confirmed. Check host rfkill and Bluetooth logs.')


@contextmanager
def runtime_lock(directory=RUNTIME):
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd = os.open(directory / 'supervisor.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BluetoothError('Another bundled Bluetooth service already owns the shared socket volume.') from None
        (directory / 'ready').unlink(missing_ok=True)
        sock = directory / 'bus'
        if sock.exists() or sock.is_symlink():
            if not stat.S_ISSOCK(sock.lstat().st_mode):
                raise BluetoothError('Private bus path is not a socket; preserve the volume and inspect it.')
            sock.unlink()
        machine_id = directory / 'machine-id'
        if not machine_id.exists():
            with machine_id.open('x') as stream:
                stream.write(uuid.uuid4().hex + '\n')
            machine_id.chmod(0o444)
        yield
    finally:
        os.close(fd)


async def terminate(process):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 4)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def monitor_host():
    while True:
        await require_host_free()
        await asyncio.sleep(2)


async def start_and_mark_ready(adapter):
    await prepare_adapter(adapter)
    RUNTIME.joinpath('ready').write_text(adapter + '\n')
    print('Bundled Bluetooth ready. Private D-Bus is available to HALO Local.', flush=True)


async def supervise(adapter, stopped):
    children, tasks = [], []
    try:
        await require_host_free()
        children.append(await asyncio.create_subprocess_exec('dbus-daemon',
            '--config-file=' + BUS_CONFIG, '--nofork', '--nopidfile', '--nosyslog'))
        # Wait for the private bus before starting bluetoothd. No activation services exist.
        for _ in range(40):
            if children[0].returncode is not None:
                raise BluetoothError('Private D-Bus exited before startup completed.')
            if stopped.is_set():
                return
            if (RUNTIME / 'bus').is_socket():
                break
            await asyncio.sleep(.1)
        else:
            raise BluetoothError('Private D-Bus did not create its socket.')
        await require_host_free()
        env = dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=PRIVATE_BUS)
        children.append(await asyncio.create_subprocess_exec('/usr/libexec/bluetooth/bluetoothd',
            '--nodetach', '--configfile=' + BLUEZ_CONFIG,
            '--noplugin=audio,a2dp,avrcp,input,network,sap', env=env))
        # Monitor child death and host conflicts even while waiting for adapter readiness.
        tasks = [asyncio.create_task(p.wait()) for p in children]
        tasks += [asyncio.create_task(monitor_host()), asyncio.create_task(stopped.wait())]
        startup = asyncio.create_task(start_and_mark_ready(adapter))
        tasks.append(startup)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if startup in done:
            startup.result()
            tasks.remove(startup)
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if stopped.is_set():
            return
        for task in done:
            task.result()
        raise BluetoothError('A Bluetooth service exited; stopping both daemons for a clean restart.')
    finally:
        RUNTIME.joinpath('ready').unlink(missing_ok=True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for process in reversed(children):
            await terminate(process)


async def healthcheck(adapter):
    if not RUNTIME.joinpath('ready').exists():
        raise BluetoothError('Bluetooth startup has not completed.')
    await require_host_free()
    if not powered(await adapter_properties(adapter)):
        raise BluetoothError('Selected Bluetooth adapter is missing or powered off.')


async def run():
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    with runtime_lock():
        await supervise(adapter_name(), stopped)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'healthcheck'))
    parser.add_argument('--allow-adapter-control', action='store_true')
    args = parser.parse_args()
    if args.action == 'run' and not args.allow_adapter_control:
        parser.error('run requires --allow-adapter-control on a dedicated Linux host')
    try:
        asyncio.run(run() if args.action == 'run' else healthcheck(adapter_name()))
    except BluetoothError as exc:
        print(str(exc), flush=True)
        return 1
    except Exception:
        print('Bundled Bluetooth could not start or became unavailable. Check both service logs.', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
