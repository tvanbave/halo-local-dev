"""Synthetic bounded notification/disconnect lifecycle; never opens Bluetooth."""
import asyncio
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.bluez import BlueZTransport, LOW
from avionmesh_pi.runtime import Runtime, BLUETOOTH_STARTUP_GRACE_MS
from support import synthetic_store
from test_service import FakeClient, FakeScanner, FakeTransport, KEY


class BluetoothLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_connection_retry_has_a_bounded_startup_window(self):
        for elapsed in (10_000, BLUETOOTH_STARTUP_GRACE_MS):
            with self.subTest(elapsed=elapsed), tempfile.TemporaryDirectory() as directory:
                store = synthetic_store(directory, KEY)
                now = 500_000
                transport = FakeTransport()
                transport.ready = False
                transport.queue = asyncio.Queue()
                runtime = Runtime(store, KEY, transport, clock=lambda: now)
                transitions = asyncio.Queue()
                runtime.session_observers.append(lambda: transitions.put_nowait(runtime.mesh_ready))

                async def connect():
                    if not runtime.last_error:
                        raise OSError('Adapter starting')
                    transport.ready = True

                transport.connect = connect
                task = asyncio.create_task(runtime.bluetooth_loop())
                try:
                    self.assertFalse(await asyncio.wait_for(transitions.get(), 3))
                    now += elapsed
                    status = runtime.status()
                    self.assertEqual(status['last_error'], 'bluetooth_connection_failed')
                    self.assertFalse(status['mesh_ready'])
                    self.assertEqual(status['bluetooth_starting'], elapsed < BLUETOOTH_STARTUP_GRACE_MS)
                    self.assertTrue(await asyncio.wait_for(transitions.get(), 3))
                    self.assertIsNone(runtime.status()['last_error'])
                    self.assertFalse(runtime.status()['bluetooth_starting'])
                    transport.ready = False
                    self.assertFalse(await asyncio.wait_for(transitions.get(), 3))
                    self.assertFalse(runtime.status()['bluetooth_starting'])
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    store.close()

    async def test_notification_timeout_disconnects_without_ready_or_retry(self):
        clients = []
        class Client(FakeClient):
            async def start_notify(self, char, callback):
                self.notifications[char] = callback
                await asyncio.Event().wait()
        def factory(*args, **kwargs):
            clients.append(Client(*args, **kwargs))
            return clients[-1]
        transport = BlueZTransport('00:11:22:33:44:55', scanner=FakeScanner, client_factory=factory)
        with patch('avionmesh_pi.bluez.NOTIFY_TIMEOUT', .02):
            with self.assertRaises(asyncio.TimeoutError):
                await transport.connect()
        self.assertFalse(transport.ready)
        self.assertIsNone(transport.client)
        self.assertEqual(len(clients), 1)
        self.assertEqual(list(clients[0].notifications), [LOW])
        self.assertEqual(transport.counts['notification_setup_failed'], 1)

    async def test_disconnect_timeout_retains_owner_and_blocks_new_client(self):
        transport = BlueZTransport('00:11:22:33:44:55', scanner=FakeScanner, client_factory=FakeClient)
        await transport.connect()
        client = transport.client
        async def blocked():
            await asyncio.Event().wait()
        client.disconnect = blocked
        with patch('avionmesh_pi.bluez.DISCONNECT_TIMEOUT', .02):
            await transport.disconnect()
        self.assertEqual(transport.counts['disconnect_failed'], 1)
        self.assertIs(transport.client, client)
        self.assertFalse(transport.ready)
        with self.assertRaisesRegex(RuntimeError, 'Previous Bluetooth connection'):
            await transport.connect()
        client.is_connected = False
        client.disconnected(client)
        await transport.connect()
        self.assertIsNot(transport.client, client)
        self.assertTrue(transport.ready)
        await transport.disconnect()

    async def test_cancel_notification_setup_disconnects_and_preserves_cancellation(self):
        entered = asyncio.Event()
        class Client(FakeClient):
            async def start_notify(self, char, callback):
                entered.set()
                await asyncio.Event().wait()
        transport = BlueZTransport('00:11:22:33:44:55', scanner=FakeScanner, client_factory=Client)
        task = asyncio.create_task(transport.connect())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(transport.ready)
        self.assertIsNone(transport.client)
