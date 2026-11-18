"""Synthetic bounded notification/disconnect lifecycle; never opens Bluetooth."""
import asyncio
import unittest
from unittest.mock import patch

from avionmesh_pi.bluez import BlueZTransport, LOW
from test_service import FakeClient, FakeScanner


class BluetoothLifecycleTests(unittest.IsolatedAsyncioTestCase):
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
