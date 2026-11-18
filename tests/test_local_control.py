"""Offline separation between local management and HA exposure."""
import asyncio
import copy
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.runtime import Runtime
from support import synthetic_store
from test_service import KEY, INVENTORY, FakeTransport


class LocalControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = synthetic_store(self.temp.name, KEY)
        inventory = copy.deepcopy(INVENTORY)
        for row in inventory['devices'] + inventory['groups']:
            row['exposed'] = False
        self.store.import_inventory(inventory)
        self.transport = FakeTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: 10)
        self.transport.receiver = self.runtime.observe
        self.runtime.mesh_ready = True
        self.output = []
        self.runtime.sink = self.output.append

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_hidden_local_light_confirms_without_mqtt_or_publication(self):
        result = await self.runtime.control(40002, {'brightness': 128}, local=True)
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['readbacks'][0]['brightness'], 128)
        self.assertFalse(self.output)
        self.assertFalse(self.runtime.discovery())
        self.assertFalse(self.store.inventory()['devices'][1]['exposed'])
        self.assertFalse(self.runtime.mqtt_ready)

    async def test_snapshot_observation_is_fresh_read_only_and_session_bound(self):
        self.assertEqual(self.runtime.device_states(), {})
        await self.runtime.control(40002, {'brightness': 128}, local=True)
        count = len(self.transport.frames)
        value = self.runtime.device_states()['40002']['brightness']
        self.assertEqual(value, {'value': 128, 'observed_ms': 10, 'age_ms': 0, 'fresh': True})
        self.runtime.clock = lambda: 120010
        self.assertFalse(self.runtime.device_states()['40002']['brightness']['fresh'])
        self.assertEqual(len(self.transport.frames), count)
        with patch.object(self.runtime, 'quarantined', return_value={40002}):
            self.assertEqual(self.runtime.device_states(), {})
        self.runtime.session()
        self.assertEqual(self.runtime.device_states(), {})

    async def test_newest_observed_field_wins_across_exposure_changes(self):
        self.runtime.local_light_states[40002] = {'brightness': (64, 1), 'color_temp': (3500, 9)}
        self.runtime.core.states[40002] = {'brightness': (128, 8), 'color_temp': (4000, 2)}
        state = self.runtime.device_states()['40002']
        self.assertEqual(state['brightness']['value'], 128)
        self.assertEqual(state['brightness']['observed_ms'], 8)
        self.assertEqual(state['color_temp']['value'], 3500)
        self.assertEqual(state['color_temp']['observed_ms'], 9)
        self.assertEqual(self.transport.frames, [])

    async def test_mqtt_default_rejects_hidden_light_and_group(self):
        for target in (40002, 291):
            with self.assertRaises(ValueError):
                await self.runtime.control(target, {'brightness': 128})
        self.assertEqual(self.transport.frames, [])

    async def test_local_group_confirms_all_known_members(self):
        result = await self.runtime.control(291, {'brightness': 128}, local=True)
        self.assertTrue(result['confirmed'])
        self.assertEqual([r['device_id'] for r in result['readbacks']], [40002])
        self.assertFalse(self.output)

    async def test_local_preserves_target_and_retained_guards(self):
        for target in (292, 12345, 40001):
            with self.assertRaises(ValueError):
                await self.runtime.control(target, {'state': 'ON'}, local=True)
        with self.assertRaises(ValueError):
            await self.runtime.control(40002, {'state': 'ON'}, local=True, retained=True)
        self.assertEqual(self.transport.frames, [])

    async def test_local_preserves_quarantine_busy_session_offline_and_admission(self):
        for target in (40002, 291):
            with patch.object(self.runtime, 'quarantined', return_value={40002}):
                with self.assertRaises(RuntimeError):
                    await self.runtime.control(target, {'state': 'ON'}, local=True)
        with patch.object(self.runtime, 'quarantined_groups', return_value={291}):
            with self.assertRaises(RuntimeError):
                await self.runtime.control(291, {'state': 'ON'}, local=True)
        async with self.runtime.operation:
            with self.assertRaises(RuntimeError):
                await self.runtime.control(40002, {'state': 'ON'}, local=True)
        with self.assertRaises(RuntimeError):
            await self.runtime.control(40002, {'state': 'ON'}, local=True, generation=-1)
        self.runtime.mesh_ready = False
        with self.assertRaises(RuntimeError):
            await self.runtime.control(40002, {'state': 'ON'}, local=True)
        self.runtime.mesh_ready = True
        with patch.object(self.store, 'allocate_sequence', side_effect=RuntimeError('Not admitted')):
            with self.assertRaises(RuntimeError):
                await self.runtime.control(40002, {'state': 'ON'}, local=True)
        self.assertEqual(self.transport.frames, [])

    async def test_hidden_devices_not_automatically_polled(self):
        calls = 0
        async def sleep(_):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise asyncio.CancelledError
        with patch('avionmesh_pi.runtime.asyncio.sleep', side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.runtime.poll()
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.output, [])

    async def test_status_separates_mqtt_configuration_and_connection(self):
        self.assertFalse(self.runtime.status()['mqtt_enabled'])
        self.runtime.mqtt_enabled = True
        status = self.runtime.status()
        self.assertTrue(status['mqtt_enabled'])
        self.assertFalse(status['mqtt_ready'])
        self.assertTrue(status['capabilities']['local_control_independent_of_ha'])

    async def test_explicit_read_sends_only_queries_and_observes_hidden_color_light(self):
        from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
        original_send = self.transport.send
        async def send(frame):
            await original_send(frame)
            packet = decrypt_mcp(frame, KEY)
            if packet.payload[:2] == bytes([1, 29]):
                reply = encrypt_mcp(bytes.fromhex('008073011d00000dac'), KEY,
                                    self.transport.sequence, packet.mcp_address)
                self.transport.sequence += 1
                self.runtime.observe(reply, 10)
        self.transport.send = send
        result = await self.runtime.read_light(40002)
        self.assertEqual(result, {'target': 40002, 'observed': {
            'state': 'ON', 'brightness': 128, 'color_temp': 3500}, 'complete': True})
        packets = [decrypt_mcp(frame, KEY) for frame in self.transport.frames]
        self.assertEqual([p.payload[:2] for p in packets], [bytes([1, 10]), bytes([1, 29])])
        self.assertEqual(self.runtime.device_states()['40002']['color_temp']['value'], 3500)
        self.assertEqual(self.output, [])

    async def test_read_ignores_wrong_source_opcode_and_noun(self):
        from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
        original_send = self.transport.send
        async def send(frame):
            packet = decrypt_mcp(frame, KEY)
            if packet.payload[:2] == bytes([1, 10]):
                for source, payload in ((40003, '008073010a004d'),
                                        (40002, '008072010a004d'),
                                        (40002, '008073011d00000dac')):
                    self.runtime.observe(encrypt_mcp(bytes.fromhex(payload), KEY,
                        self.transport.sequence, source), 10)
                    self.transport.sequence += 1
            await original_send(frame)
            if packet.payload[:2] == bytes([1, 29]):
                self.runtime.observe(encrypt_mcp(bytes.fromhex('008073011d00000fa0'), KEY,
                    self.transport.sequence, 40002), 10)
                self.transport.sequence += 1
        self.transport.send = send
        result = await self.runtime.read_light(40002)
        self.assertEqual(result['observed']['brightness'], 128)
        self.assertEqual(result['observed']['color_temp'], 4000)
        self.assertTrue(result['complete'])

    async def test_read_session_boundary_stops_before_followup_query(self):
        async def send(frame):
            self.transport.frames.append(frame)
            self.runtime.session()
        self.transport.send = send
        with self.assertRaises(RuntimeError):
            await self.runtime.read_light(40002)
        self.assertEqual(len(self.transport.frames), 1)
        self.assertEqual(self.runtime.waiters, [])

    async def test_read_timeout_never_fills_unknown_with_zero_or_old_cache(self):
        from unittest.mock import AsyncMock
        self.runtime.local_light_states[40002] = {'brightness': (128, 1)}
        with patch.object(self.runtime, 'exchange', new=AsyncMock(side_effect=asyncio.TimeoutError)):
            result = await self.runtime.read_light(40002)
        self.assertEqual(result, {'target': 40002, 'observed': {}, 'complete': False})

    async def test_read_target_pending_busy_offline_admission_guards(self):
        for target in (True, 291, 40001, 65407):
            with self.assertRaises(ValueError):
                await self.runtime.read_light(target)
        with patch.object(self.store, 'journals', return_value=[{'target': 40002, 'kind': 'claim'}]):
            with self.assertRaises(RuntimeError):
                await self.runtime.read_light(40002)
        async with self.runtime.operation:
            with self.assertRaises(RuntimeError):
                await self.runtime.read_light(40002)
        self.runtime.mesh_ready = False
        with self.assertRaises(RuntimeError):
            await self.runtime.read_light(40002)
        self.runtime.mesh_ready = True
        with patch.object(self.store, 'allocate_sequence', side_effect=RuntimeError('Not admitted')):
            with self.assertRaises(RuntimeError):
                await self.runtime.read_light(40002)
        self.assertEqual(self.transport.frames, [])

    async def test_read_http_validates_request_and_does_not_set(self):
        from unittest.mock import AsyncMock
        from aiohttp.test_utils import TestClient, TestServer
        from avionmesh_pi.server import create_app
        token = 'synthetic-management-token-01234567890123'
        headers = {'Authorization': 'Bearer ' + token}
        async with TestClient(TestServer(create_app(self.runtime, token))) as client:
            for request in ({'target': True}, {'target': 40002, 'values': {'state': 'ON'}}):
                response = await client.post('/api/device/read', headers=headers, json=request)
                self.assertEqual(response.status, 400)
            with patch.object(self.runtime, 'exchange', new=AsyncMock(side_effect=asyncio.TimeoutError)):
                response = await client.post('/api/device/read', headers=headers, json={'target': 40002})
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.json(), {'target': 40002, 'observed': {}, 'complete': False})
        self.assertEqual(self.transport.frames, [])

    async def test_http_control_uses_local_path(self):
        from aiohttp.test_utils import TestClient, TestServer
        from avionmesh_pi.server import create_app
        token = 'synthetic-management-token-01234567890123'
        async with TestClient(TestServer(create_app(self.runtime, token))) as client:
            response = await client.post('/api/control', headers={'Authorization': 'Bearer ' + token},
                                         json={'target': 40002, 'values': {'brightness': 128}})
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())['confirmed'])
        self.assertFalse(self.store.inventory()['devices'][1]['exposed'])
