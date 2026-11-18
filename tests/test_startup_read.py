"""One-time read-only light read after the bridge first reaches the mesh. Synthetic only."""
import asyncio
import copy
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import KEY

INVENTORY = {'devices': [
    {'device_id': 40001, 'product_id': 127, 'name': 'Keypad', 'exposed': False},
    {'device_id': 40003, 'product_id': 93, 'name': 'RL4', 'exposed': False},
    {'device_id': 40002, 'product_id': 134, 'name': 'A19', 'exposed': False}],
    'groups': []}


class ReadOnlyMesh:
    """Answers brightness/colour-temperature reads; records every frame sent."""
    ready = True
    dropped = 0

    def __init__(self):
        self.frames, self.receiver, self.sequence, self.silent = [], None, 1, set()

    async def disconnect(self):
        self.ready = False

    async def send(self, frame):
        command = decrypt_mcp(frame, KEY)
        self.frames.append((command.mcp_address, command.payload[:2]))
        if command.mcp_address in self.silent:
            return
        reply = {b'\x01\x0a': b'\x01\x0a\x00\x80', b'\x01\x1d': b'\x01\x1d\x00\x01\x0b\xb8'}.get(command.payload[:2])
        if reply:
            self.sequence += 1
            self.receiver(encrypt_mcp(b'\x00\x80\x73' + reply, KEY, self.sequence, command.mcp_address), 10)


class StartupReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory(copy.deepcopy(INVENTORY))
        self.mesh = ReadOnlyMesh()
        self.runtime = Runtime(self.store, KEY, self.mesh, clock=lambda: 10)
        self.mesh.receiver = self.runtime.observe
        self.runtime.mesh_ready = True
        self.runtime.session()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def run_read(self, **kwargs):
        options = dict(settle=0, gap=0, mesh_wait=5, busy_wait=0)
        options.update(kwargs)
        return await asyncio.wait_for(self.runtime.startup_read(**options), 30)

    async def test_reads_each_light_once_with_read_only_queries(self):
        progress = await self.run_read()
        self.assertEqual(progress, {'state': 'done', 'lights': 2, 'answered': 2, 'skipped': 0})
        # Lights in id order; RL4 and A19 both support colour temperature. No keypad, no setters.
        self.assertEqual(self.mesh.frames, [(40002, b'\x01\x0a'), (40002, b'\x01\x1d'),
                                            (40003, b'\x01\x0a'), (40003, b'\x01\x1d')])
        states = self.runtime.device_states()
        self.assertEqual(states['40002']['brightness']['value'], 0x80)
        self.assertEqual(states['40003']['color_temp']['value'], 3000)
        self.assertIs(self.runtime.startup_read_progress, progress)

    async def test_waits_for_first_mesh_connection(self):
        self.runtime.mesh_ready = False
        async def connect_later():
            await asyncio.sleep(1.2)
            self.runtime.mesh_ready = True
        task = asyncio.create_task(connect_later())
        progress = await self.run_read()
        await task
        self.assertEqual(progress['state'], 'done')
        self.assertEqual(progress['answered'], 2)

    async def test_gives_up_quietly_without_mesh(self):
        self.runtime.mesh_ready = False
        progress = await self.run_read(mesh_wait=1)
        self.assertEqual(progress['state'], 'mesh_unavailable')
        self.assertEqual(self.mesh.frames, [])

    async def test_light_with_unresolved_operation_is_skipped_not_retried(self):
        self.store.begin('claim', 40003, {'uuid': '00' * 16})
        progress = await self.run_read()
        self.assertEqual(progress, {'state': 'done', 'lights': 2, 'answered': 1, 'skipped': 1})
        self.assertNotIn(40003, {target for target, _ in self.mesh.frames})

    async def test_silent_light_is_skipped_once(self):
        self.mesh.silent.add(40002)
        with patch.object(Runtime, 'exchange', autospec=True,
                          side_effect=self.fast_exchange(Runtime.exchange)):
            progress = await self.run_read()
        self.assertEqual(progress, {'state': 'done', 'lights': 2, 'answered': 1, 'skipped': 1})
        # Each field asked exactly once: no retry of the silent light.
        self.assertEqual([p for t, p in self.mesh.frames if t == 40002], [b'\x01\x0a', b'\x01\x1d'])

    @staticmethod
    def fast_exchange(original):
        async def exchange(self, plaintext, predicate, timeout=3, **kwargs):
            return await original(self, plaintext, predicate, timeout=0.05, **kwargs)
        return exchange

    async def test_busy_bridge_goes_first(self):
        async with self.runtime.operation:
            progress = await self.run_read()
        self.assertEqual(progress, {'state': 'done', 'lights': 2, 'answered': 0, 'skipped': 2})
        self.assertEqual(self.mesh.frames, [])

    async def test_unexpected_error_never_escapes(self):
        with patch.object(Runtime, 'read_light', side_effect=KeyError('synthetic')):
            progress = await self.run_read()
        self.assertEqual(progress['state'], 'failed')


class StartupReadConfigTests(unittest.TestCase):
    def test_service_starts_it_unless_disabled_and_validates_flag(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / 'avionmesh_pi' / 'service.py')
        if not source.is_file():
            import avionmesh_pi.service as service
            source = Path(service.__file__)
        text = source.read_text()
        self.assertIn("if runtime.capabilities.get('startup_light_read') is not False:", text)
        self.assertIn('"startup_light_read"}', text)


class MetadataKeepsReadingsTests(unittest.IsolatedAsyncioTestCase):
    """A rename or room edit must not forget what local-only lights last reported."""
    asyncSetUp = StartupReadTests.asyncSetUp
    asyncTearDown = StartupReadTests.asyncTearDown
    run_read = StartupReadTests.run_read

    def edit(self, target, name, exposed=False):
        from avionmesh_pi.lifecycle import reload_inventory
        from avionmesh_pi.management import edit_metadata, revision
        edit_metadata(self.store, {'operation': 'update', 'revision': revision(self.store),
                                   'kind': 'device', 'target': target, 'name': name, 'exposed': exposed})
        reload_inventory(self.runtime)

    def values(self):
        return {k: {f: o['value'] for f, o in v.items()} for k, v in self.runtime.device_states().items()}

    async def test_rename_keeps_last_reported_states_and_still_bumps_session(self):
        await self.run_read()
        before, generation = self.values(), self.runtime.generation
        self.assertEqual(set(before), {'40002', '40003'})
        self.edit(40003, 'Renamed RL4')
        self.assertEqual(self.values(), before)
        self.assertGreater(self.runtime.generation, generation)  # in-flight work still invalidated
        self.assertEqual(self.runtime.core.devices[40003].name, 'Renamed RL4')

    async def test_exposing_a_light_requires_a_fresh_reading_for_it_only(self):
        await self.run_read()
        self.edit(40003, 'RL4', exposed=True)
        self.assertEqual(set(self.values()), {'40002'})
        self.assertNotIn(40003, self.runtime.local_light_states)

    async def test_real_session_change_still_forgets_readings(self):
        await self.run_read()
        self.runtime.session()
        self.assertEqual(self.values(), {})
