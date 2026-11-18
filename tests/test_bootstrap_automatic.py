"""Automatic bearer choice with synthetic advertisements/GATT only."""
import asyncio
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from avionmesh_pi.bluez import HIGH, LOW, SERVICE
from avionmesh_pi.bootstrap import AutoBlueZTransport, AutomaticBootstrap
from avionmesh_pi.runtime import Runtime
from support import synthetic_store


FIRST = '00:11:22:33:44:55'
SECOND = '00:11:22:33:44:66'


def advertisement(address, rssi, service=SERVICE):
    return (SimpleNamespace(address=address),
            SimpleNamespace(service_uuids=[service] if service else [], rssi=rssi))


class Client:
    def __init__(self, device, disconnected_callback, **kwargs):
        self.device = device
        self.disconnected = disconnected_callback
        self.is_connected = False
        self.services = self
        self.notifications = {}
        self.writes = []
        self.missing = None
        if kwargs != {'bluez': {'adapter': 'hci0'}}:
            raise AssertionError('Unexpected adapter')

    def get_characteristic(self, characteristic):
        return None if characteristic == self.missing else characteristic

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        self.disconnected(self)

    async def start_notify(self, characteristic, callback):
        self.notifications[characteristic] = callback

    async def write_gatt_char(self, characteristic, data, response):
        self.writes.append((characteristic, data))


class AutomaticTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []
        self.scanner = SimpleNamespace(
            discover=AsyncMock(return_value={1: advertisement(FIRST, -60),
                                            2: advertisement(SECOND, -40)}),
            find_device_by_address=AsyncMock(side_effect=lambda address, **kwargs:
                                             SimpleNamespace(address=address)))
        def factory(*args, **kwargs):
            client = Client(*args, **kwargs)
            self.clients.append(client)
            return client
        self.transport = AutoBlueZTransport(scanner=self.scanner, client_factory=factory)

    async def asyncTearDown(self):
        await self.transport.disconnect()

    async def test_construction_has_no_io_and_strongest_service_is_automatic(self):
        self.scanner.discover.assert_not_awaited()
        self.assertIsNone(self.transport.selected_address)
        self.scanner.discover.return_value[3] = advertisement('00:11:22:33:44:77', -10, None)
        await self.transport.connect()
        self.scanner.discover.assert_awaited_once_with(
            timeout=5, return_adv=True, bluez={'adapter': 'hci0'})
        self.scanner.find_device_by_address.assert_awaited_once_with(
            SECOND, timeout=10, bluez={'adapter': 'hci0'})
        self.assertEqual(self.transport.selected_address, SECOND)
        self.assertEqual(set(self.transport.client.notifications), {LOW, HIGH})
        self.assertEqual(self.transport.client.writes, [])

    async def test_tied_rssi_has_stable_address_order(self):
        self.scanner.discover.return_value = {
            1: advertisement(SECOND, -40), 2: advertisement(FIRST, -40)}
        await self.transport.connect()
        self.assertEqual(self.transport.selected_address, FIRST)

    async def test_malformed_candidates_do_not_outvote_valid_bearer(self):
        self.scanner.discover.return_value = {
            1: advertisement('not-an-address', 10),
            2: advertisement('00:00:00:00:00:00', 10),
            3: advertisement(SECOND, float('nan')),
            4: advertisement(SECOND, True),
            5: advertisement(FIRST.lower(), -60, SERVICE.upper())}
        await self.transport.connect()
        self.assertEqual(self.transport.selected_address, FIRST)

    async def test_no_csrmesh_candidate_never_constructs_client(self):
        self.scanner.discover.return_value = {1: advertisement(FIRST, -10, None)}
        with self.assertRaisesRegex(RuntimeError, 'No CSRMesh'):
            await self.transport.connect()
        self.scanner.find_device_by_address.assert_not_awaited()
        self.assertEqual(self.clients, [])
        self.assertIsNone(self.transport.selected_address)

    async def test_truncated_scan_fails_closed_before_connect(self):
        self.scanner.discover.return_value = {
            index: advertisement(f'00:11:22:33:44:{index:02X}', -40)
            for index in range(129)}
        with self.assertRaisesRegex(RuntimeError, 'truncated'):
            await self.transport.connect()
        self.scanner.find_device_by_address.assert_not_awaited()
        self.assertEqual(self.clients, [])
        self.assertIsNone(self.transport.selected_address)

    async def test_scan_timeout_is_bounded_and_next_attempt_can_retry(self):
        async def stuck(**kwargs):
            await asyncio.Event().wait()
        self.scanner.discover.side_effect = stuck
        wait_for = asyncio.wait_for
        async def short_wait(future, timeout):
            self.assertEqual(timeout, 8)
            return await wait_for(future, 0.001)
        with patch('avionmesh_pi.bootstrap.asyncio.wait_for', side_effect=short_wait):
            with self.assertRaises(asyncio.TimeoutError):
                await self.transport.connect()
        self.assertEqual(self.clients, [])
        self.assertIsNone(self.transport.selected_address)
        self.scanner.discover.side_effect = None
        await self.transport.connect()
        self.assertTrue(self.transport.ready)

    async def test_cancelled_scan_does_not_lock_out_next_attempt(self):
        started = asyncio.Event()
        async def stuck(**kwargs):
            started.set()
            await asyncio.Event().wait()
        self.scanner.discover.side_effect = stuck
        task = asyncio.create_task(self.transport.connect())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.scanner.discover.side_effect = None
        await self.transport.connect()
        self.assertTrue(self.transport.ready)

    async def test_reconnect_rescans_and_uses_new_strongest_not_old_address(self):
        await self.transport.connect()
        old = self.transport.client
        notify = old.notifications[HIGH]
        await self.transport.disconnect()
        self.assertIsNone(self.transport.selected_address)
        self.scanner.discover.return_value = {
            1: advertisement(FIRST, -30), 2: advertisement(SECOND, -80)}
        await self.transport.connect()
        self.assertEqual(self.transport.selected_address, FIRST)
        self.assertEqual(self.scanner.discover.await_count, 2)
        old.disconnected(old)
        notify(None, bytes(17))
        self.assertTrue(self.transport.ready)
        self.assertTrue(self.transport.queue.empty())
        self.assertTrue(all(not client.writes for client in self.clients))

    async def test_failed_rescan_does_not_reuse_stale_bearer(self):
        await self.transport.connect()
        await self.transport.disconnect()
        self.scanner.discover.side_effect = RuntimeError('Synthetic scan failure')
        with self.assertRaisesRegex(RuntimeError, 'Synthetic scan failure'):
            await self.transport.connect()
        self.assertEqual(len(self.clients), 1)
        self.assertIsNone(self.transport.selected_address)

    async def test_disappearing_candidate_fails_before_client_construction(self):
        self.scanner.find_device_by_address.side_effect = None
        self.scanner.find_device_by_address.return_value = None
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            await self.transport.connect()
        self.assertEqual(self.clients, [])
        self.assertIsNone(self.transport.selected_address)

    async def test_normal_gatt_characteristic_checks_still_apply(self):
        factory = self.transport.client_factory
        def missing(*args, **kwargs):
            client = factory(*args, **kwargs)
            client.missing = HIGH
            return client
        self.transport.client_factory = missing
        with self.assertRaisesRegex(RuntimeError, 'missing CSRMesh'):
            await self.transport.connect()
        self.assertEqual(len(self.clients), 2)
        self.assertTrue(all(not client.is_connected for client in self.clients))
        self.assertTrue(all(not client.writes for client in self.clients))
        self.assertIsNone(self.transport.selected_address)

    async def test_strongest_connect_failure_falls_back_to_second_candidate(self):
        factory = self.transport.client_factory
        def fail_strongest(*args, **kwargs):
            self.assertTrue(all(not client.is_connected for client in self.clients))
            client = factory(*args, **kwargs)
            if client.device.address == SECOND:
                client.connect = AsyncMock(side_effect=RuntimeError('Synthetic refusal'))
            return client
        self.transport.client_factory = fail_strongest
        await self.transport.connect()
        self.assertEqual(self.transport.selected_address, FIRST)
        self.assertEqual([client.device.address for client in self.clients], [SECOND, FIRST])
        self.assertTrue(all(not client.writes for client in self.clients))

    async def test_strongest_missing_high_falls_back_after_confirmed_disconnect(self):
        factory = self.transport.client_factory
        def missing_strongest(*args, **kwargs):
            self.assertTrue(all(not client.is_connected for client in self.clients))
            client = factory(*args, **kwargs)
            if client.device.address == SECOND:
                client.missing = HIGH
            return client
        self.transport.client_factory = missing_strongest
        await self.transport.connect()
        self.assertEqual(self.transport.selected_address, FIRST)
        self.assertEqual(len(self.clients), 2)
        self.assertFalse(self.clients[0].is_connected)
        self.assertEqual(set(self.clients[1].notifications), {LOW, HIGH})
        self.assertTrue(all(not client.writes for client in self.clients))

    async def test_all_candidates_fail_with_four_attempt_limit(self):
        self.scanner.discover.return_value = {
            index: advertisement(f'00:11:22:33:44:{index:02X}', -30 - index)
            for index in range(6)}
        factory = self.transport.client_factory
        def missing(*args, **kwargs):
            self.assertTrue(all(not client.is_connected for client in self.clients))
            client = factory(*args, **kwargs)
            client.missing = HIGH
            return client
        self.transport.client_factory = missing
        with self.assertRaisesRegex(RuntimeError, 'missing CSRMesh'):
            await self.transport.connect()
        self.assertEqual([client.device.address for client in self.clients],
                         [f'00:11:22:33:44:{index:02X}' for index in range(4)])
        self.assertEqual(self.scanner.discover.await_count, 1)
        self.assertEqual(self.scanner.find_device_by_address.await_count, 4)
        self.assertTrue(all(not client.is_connected for client in self.clients))
        self.assertTrue(all(not client.writes for client in self.clients))
        self.assertIsNone(self.transport.selected_address)

    async def test_cancelled_connection_stops_before_fallback(self):
        factory = self.transport.client_factory
        started = asyncio.Event()
        def slow(*args, **kwargs):
            client = factory(*args, **kwargs)
            async def connect():
                client.is_connected = True
                started.set()
                await asyncio.Event().wait()
            client.connect = connect
            return client
        self.transport.client_factory = slow
        task = asyncio.create_task(self.transport.connect())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.clients), 1)
        self.assertFalse(self.clients[0].is_connected)
        self.assertEqual(self.clients[0].writes, [])
        self.assertEqual(self.scanner.find_device_by_address.await_count, 1)
        self.assertIsNone(self.transport.selected_address)

    async def test_unclosed_failed_candidate_stops_fallback(self):
        factory = self.transport.client_factory
        def unclosed(*args, **kwargs):
            client = factory(*args, **kwargs)
            client.missing = HIGH
            client.disconnect = AsyncMock(side_effect=RuntimeError('Cannot close'))
            return client
        self.transport.client_factory = unclosed
        with self.assertRaisesRegex(RuntimeError, 'missing CSRMesh'):
            await self.transport.connect()
        self.assertEqual(len(self.clients), 1)
        self.assertTrue(self.clients[0].is_connected)
        self.assertEqual(self.clients[0].writes, [])
        self.assertEqual(self.scanner.find_device_by_address.await_count, 1)
        self.assertIsNone(self.transport.selected_address)
        self.clients[0].is_connected = False

    async def test_unclosed_session_blocks_scan_and_address_change(self):
        await self.transport.connect()
        self.transport.client.disconnect = AsyncMock(side_effect=RuntimeError('Cannot close'))
        await self.transport.disconnect()
        with self.assertRaisesRegex(RuntimeError, 'has not closed'):
            await self.transport.connect()
        self.assertEqual(self.scanner.discover.await_count, 1)
        self.assertEqual(self.transport.address, SECOND)
        self.assertEqual(len(self.clients), 1)
        self.transport.client.is_connected = False

    async def test_overlapping_connect_does_not_start_second_scan(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def slow(**kwargs):
            started.set()
            await finish.wait()
            return {1: advertisement(FIRST, -40)}
        self.scanner.discover.side_effect = slow
        task = asyncio.create_task(self.transport.connect())
        await started.wait()
        try:
            with self.assertRaisesRegex(RuntimeError, 'already active'):
                await self.transport.connect()
        finally:
            finish.set()
            await task
        self.assertEqual(self.scanner.discover.await_count, 1)
        self.assertEqual(len(self.clients), 1)


class AutomaticStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_uses_only_live_bearer_without_persisting_or_claiming(self):
        with tempfile.TemporaryDirectory() as directory:
            store = synthetic_store(directory, bytes(range(16)))
            try:
                store.set('bootstrap_relay_address', FIRST)
                transport = AutoBlueZTransport()
                runtime = Runtime(store, bytes(range(16)), transport)
                bootstrap = AutomaticBootstrap(runtime)
                self.assertEqual(bootstrap.status(), {
                    'enabled': True, 'automatic': True,
                    'selected_address': None, 'needs_first_claim': True})
                transport.address, transport.ready = SECOND, True
                self.assertEqual(bootstrap.selected_address, SECOND)
                self.assertEqual(store.get('bootstrap_relay_address'), FIRST)
                self.assertIsNone(store.get('bootstrap_owner_reset_declared'))
                self.assertEqual(store.get('sequence'), 0)
                self.assertEqual(store.journals(), [])
                store.import_inventory({'devices': [
                    {'device_id': 40000, 'product_id': 93, 'name': 'Synthetic', 'exposed': False}],
                    'groups': []})
                self.assertFalse(bootstrap.needs_first_claim)
                transport.ready = False
                self.assertIsNone(bootstrap.selected_address)
            finally:
                store.close()
