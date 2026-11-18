"""Offline lifecycle tests and optional real private-D-Bus tests, never Bluetooth I/O."""
import asyncio
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from dbus_fast import Message, Variant, PropertyAccess
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property
from avionmesh_pi import bluetooth_stack as b


def props(on=True):
    return {'Powered': Variant('b', on)}


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_host_is_rejected_without_any_adapter_calls(self):
        with patch.object(b, 'call', AsyncMock(return_value=[True])) as call:
            with self.assertRaisesRegex(b.BluetoothError, 'Host BlueZ is running'):
                await b.require_host_free()
        self.assertEqual(call.await_count, 1)
        self.assertEqual(call.call_args.args[1].member, 'NameHasOwner')
        self.assertEqual(call.call_args.args[1].destination, 'org.freedesktop.DBus')

    async def test_missing_or_denied_host_bus_fails_closed(self):
        for effect in (OSError('synthetic'), b.BluetoothError('denied')):
            with patch.object(b, 'call', AsyncMock(side_effect=effect)):
                with self.assertRaisesRegex(b.BluetoothError, 'Cannot check host'):
                    await b.require_host_free()

    async def test_host_check_requires_definite_false(self):
        for body in ([], [True], ['false']):
            with patch.object(b, 'call', AsyncMock(return_value=body)):
                with self.assertRaises(b.BluetoothError):
                    await b.require_host_free()
        with patch.object(b, 'call', AsyncMock(return_value=[False])):
            await b.require_host_free()

    async def test_stop_request_while_closing_a_bus_is_not_swallowed(self):
        # A slow disconnect is normal on a Pi. Cancelling there must still stop the caller.
        class SlowBus:
            def disconnect(self): pass
            async def wait_for_disconnect(self): await asyncio.Event().wait()
        task = asyncio.create_task(b.close_bus(SlowBus()))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        class FailedBus(SlowBus):
            async def wait_for_disconnect(self): raise ConnectionError('closed')
        await b.close_bus(FailedBus())  # ordinary close errors are still ignored

    async def test_monitor_stops_when_cancelled_during_a_slow_disconnect(self):
        closing = asyncio.Event()
        class SlowBus:
            def __init__(self, **kwargs): pass
            async def connect(self): pass
            async def call(self, message): return type('Reply', (), {'message_type': None, 'body': [False]})()
            def disconnect(self): closing.set()
            async def wait_for_disconnect(self): await asyncio.Event().wait()
        with patch.object(b, 'MessageBus', SlowBus):
            task = asyncio.create_task(b.monitor_host())
            await asyncio.wait_for(closing.wait(), 2)
            task.cancel()
            done, pending = await asyncio.wait([task], timeout=2)
        self.assertFalse(pending, 'the host monitor kept running after it was cancelled')
        self.assertTrue(task.cancelled())

    async def test_powered_adapter_needs_no_write(self):
        with patch.object(b, 'require_host_free', AsyncMock()), \
             patch.object(b, 'adapter_properties', AsyncMock(return_value=props())), \
             patch.object(b, 'call', AsyncMock()) as call:
            await b.prepare_adapter('hci0')
            call.assert_not_called()

    async def test_selected_adapter_is_powered_once_then_read_back(self):
        with patch.object(b, 'require_host_free', AsyncMock()), \
             patch.object(b, 'adapter_properties', AsyncMock(side_effect=[props(False), props()])), \
             patch.object(b, 'call', AsyncMock()) as call:
            await b.prepare_adapter('hci2')
        self.assertEqual(call.await_count, 1)
        request = call.call_args.args[1]
        self.assertEqual(request.path, '/org/bluez/hci2')
        self.assertEqual(request.member, 'Set')
        self.assertEqual(request.body[:2], ['org.bluez.Adapter1', 'Powered'])

    async def test_uncertain_power_setter_is_not_retried(self):
        with patch.object(b, 'require_host_free', AsyncMock()), \
             patch.object(b, 'adapter_properties', AsyncMock(return_value=props(False))), \
             patch.object(b, 'call', AsyncMock(side_effect=TimeoutError)) as call:
            with self.assertRaisesRegex(b.BluetoothError, 'Could not enable Bluetooth'):
                await b.prepare_adapter('hci0')
        self.assertEqual(call.await_count, 1)

    async def test_missing_adapter_never_writes(self):
        with patch.object(b, 'require_host_free', AsyncMock()), \
             patch.object(b, 'adapter_properties', AsyncMock(return_value=None)), \
             patch.object(b, 'call', AsyncMock()) as call, \
             patch.object(b.asyncio, 'sleep', AsyncMock()):
            with self.assertRaisesRegex(b.BluetoothError, 'adapter not found'):
                await b.prepare_adapter('hci0', attempts=2)
        call.assert_not_called()

    async def test_host_conflict_prevents_daemon_start(self):
        with tempfile.TemporaryDirectory() as d, patch.object(b, 'RUNTIME', Path(d)), \
             patch.object(b, 'require_host_free', AsyncMock(side_effect=b.BluetoothError('conflict'))), \
             patch.object(b.asyncio, 'create_subprocess_exec', AsyncMock()) as create:
            with self.assertRaises(b.BluetoothError):
                await b.supervise('hci0', asyncio.Event())
            create.assert_not_called()

    async def test_healthcheck_is_read_only_and_checks_adapter(self):
        with tempfile.TemporaryDirectory() as d, patch.object(b, 'RUNTIME', Path(d)), \
             patch.object(b, 'require_host_free', AsyncMock()), \
             patch.object(b, 'adapter_properties', AsyncMock(return_value=props(False))), \
             patch.object(b, 'call', AsyncMock()) as call:
            with self.assertRaisesRegex(b.BluetoothError, 'startup'):
                await b.healthcheck('hci0')
            Path(d, 'ready').touch()
            with self.assertRaisesRegex(b.BluetoothError, 'powered off'):
                await b.healthcheck('hci0')
            call.assert_not_called()

    async def test_both_daemons_are_stopped_when_host_conflict_appears(self):
        class Child:
            returncode = None
            def __init__(self): self.done = asyncio.Event(); self.terminated = False
            async def wait(self): await self.done.wait(); return self.returncode
            def terminate(self): self.terminated = True; self.returncode = 0; self.done.set()
        children = [Child(), Child()]
        async def conflict():
            await asyncio.sleep(.01)
            raise b.BluetoothError('host conflict')
        with tempfile.TemporaryDirectory() as d, patch.object(b, 'RUNTIME', Path(d)), \
             patch.object(b, 'require_host_free', AsyncMock()), \
             patch.object(b, 'start_and_mark_ready', AsyncMock()), \
             patch.object(b, 'monitor_host', conflict), \
             patch.object(b.asyncio, 'create_subprocess_exec', AsyncMock(side_effect=children)):
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(Path(d, 'bus')))
                with self.assertRaisesRegex(b.BluetoothError, 'host conflict'):
                    await b.supervise('hci0', asyncio.Event())
            self.assertTrue(all(p.terminated for p in children))
            self.assertFalse(Path(d, 'ready').exists())


class LockTests(unittest.TestCase):
    def test_duplicate_stack_cannot_remove_running_socket_or_ready(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            with b.runtime_lock(path):
                path.joinpath('ready').touch()
                with self.assertRaisesRegex(b.BluetoothError, 'already owns'):
                    with b.runtime_lock(path): pass
                self.assertTrue(path.joinpath('ready').exists())

    def test_regular_file_at_socket_path_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'bus').write_text('preserve')
            with self.assertRaises(b.BluetoothError):
                with b.runtime_lock(Path(d)): pass
            self.assertEqual(Path(d, 'bus').read_text(), 'preserve')

    def test_stale_socket_is_replaced_but_machine_identity_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(path / 'bus'))
            with b.runtime_lock(path):
                machine_id = path.joinpath('machine-id').read_text()
                self.assertFalse(path.joinpath('bus').exists())
            with b.runtime_lock(path):
                self.assertEqual(path.joinpath('machine-id').read_text(), machine_id)

    def test_invalid_adapter_rejected(self):
        with patch.dict(os.environ, HALO_ADAPTER='../hci0'):
            with self.assertRaises(b.BluetoothError): b.adapter_name()


class FakeAdapter(ServiceInterface):
    def __init__(self): super().__init__('org.bluez.Adapter1')
    @dbus_property(access=PropertyAccess.READ)
    def Powered(self) -> 'b':
        return True


@unittest.skipUnless(os.environ.get('HALO_TEST_PRIVATE_DBUS') == '1',
                     'Private D-Bus integration runs only in the isolated Bluetooth image')
class PrivateBusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        directory.chmod(0o755)
        self.address = 'unix:path=' + str(directory / 'bus')
        config = Path(b.BUS_CONFIG).read_text().replace('/run/halo-bluetooth/bus', str(directory / 'bus'))
        path = directory / 'bus.conf'; path.write_text(config)
        self.daemon = await asyncio.create_subprocess_exec('dbus-daemon', '--config-file=' + str(path),
            '--nofork', '--nopidfile', '--nosyslog')
        self.addAsyncCleanup(b.terminate, self.daemon)
        for _ in range(40):
            if self.daemon.returncode is not None:
                self.fail('Private D-Bus failed to start')
            if (directory/'bus').is_socket(): break
            await asyncio.sleep(.05)
        self.bus = await MessageBus(bus_address=self.address).connect()
        self.addAsyncCleanup(b.close_bus, self.bus)
        self.bus.export('/org/bluez/hci0', FakeAdapter())
        await self.bus.request_name('org.bluez')

    async def child(self, uid, code):
        process = await asyncio.create_subprocess_exec('python', '-c', code,
            env=dict(os.environ, SYNTHETIC_BUS=self.address), user=uid,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 8)
        self.assertEqual(process.returncode, 0, stderr.decode())
        return stdout.decode().strip()

    async def test_bridge_uid_can_read_adapter_but_cannot_own_bluez(self):
        result = await self.child(65532, '''
import asyncio, os
from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus
async def test():
 b=await MessageBus(bus_address=os.environ['SYNTHETIC_BUS']).connect()
 r=await b.call(Message(destination='org.bluez',path='/',interface='org.freedesktop.DBus.ObjectManager',member='GetManagedObjects'))
 assert r.message_type==MessageType.METHOD_RETURN and '/org/bluez/hci0' in r.body[0], (r.message_type,r.error_name,r.body)
 r=await b.call(Message(destination='org.freedesktop.DBus',path='/org/freedesktop/DBus',interface='org.freedesktop.DBus',member='RequestName',signature='su',body=['org.bluez',0]))
 assert r.message_type==MessageType.ERROR
 b.disconnect(); await b.wait_for_disconnect(); print('allowed-read-denied-own')
asyncio.run(test())
''')
        self.assertEqual(result, 'allowed-read-denied-own')

    async def test_other_uid_cannot_connect(self):
        result = await self.child(10001, '''
import asyncio, os
from dbus_fast.aio import MessageBus
async def test():
 b=MessageBus(bus_address=os.environ['SYNTHETIC_BUS'])
 try: await asyncio.wait_for(b.connect(),2)
 except Exception: print('denied')
 else: raise AssertionError('unexpected access')
 finally: b.disconnect()
asyncio.run(test())
''')
        self.assertEqual(result, 'denied')

    async def test_real_daemon_failure_stops_private_bus_cleanly(self):
        # The CI container has network:none and no NET_ADMIN/NET_RAW. Real
        # bluetoothd must exit when it cannot open the kernel management socket.
        await self.bus.release_name('org.bluez')
        with patch.object(b, 'HOST_BUS', self.address), b.runtime_lock():
            with self.assertRaisesRegex(b.BluetoothError, 'service exited'):
                await asyncio.wait_for(b.supervise('hci0', asyncio.Event()), 10)
        self.assertFalse(b.RUNTIME.joinpath('ready').exists())
        with self.assertRaises(Exception):
            await b.call(b.PRIVATE_BUS, Message(destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus', interface='org.freedesktop.DBus', member='ListNames'))

    async def test_real_host_owner_guard_does_not_activate_services(self):
        with patch.object(b, 'HOST_BUS', self.address):
            with self.assertRaisesRegex(b.BluetoothError, 'Host BlueZ is running'):
                await b.require_host_free()
            await self.bus.release_name('org.bluez')
            await b.require_host_free()


if __name__ == '__main__': unittest.main()
