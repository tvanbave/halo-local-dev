"""Actual encrypted ingress keeps permanent scene tracking outside scoped hooks."""
import tempfile
import unittest

from avionmesh_pi.protocol import encrypt_mcp
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.scene_engine import KEY as SCENES
from support import synthetic_store as Store
from test_service import KEY, INVENTORY, FakeTransport


class SceneRuntimeIngressTests(unittest.TestCase):
    def test_authenticated_nonduplicate_mutation_revokes_lease_without_owning_scoped_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, KEY)
            try:
                store.import_inventory(INVENTORY)
                runtime = Runtime(store, KEY, FakeTransport(), clock=lambda: 10)
                runtime.mesh_ready = True
                engine = runtime.scene_engine
                row = {'id': 'a' * 32, 'reference': 1, 'name': 'Synthetic scene',
                       'members': [{'target': 40002, 'brightness': 128, 'kelvin': 4000}],
                       'exposed': False, 'owner': engine._owner()}
                store.set(SCENES, [row])
                engine.leases[row['id']] = engine._stamp(row)
                self.assertEqual(runtime.packet_observers, [])
                seen = []
                def scoped(packet, received):
                    seen.append(packet.payload)
                    self.assertFalse(engine.leases)
                runtime.packet_observers.append(scoped)
                payload = bytes.fromhex('022200005a01020fa000')
                plain = (40002).to_bytes(2, 'little') + b'\x73' + payload
                invalid = encrypt_mcp(plain, bytes(reversed(KEY)), 1)
                runtime.observe(invalid, 10)
                self.assertTrue(engine.leases)
                self.assertEqual(engine.mutation_epoch, 0)
                frame = encrypt_mcp(plain, KEY, 2)
                runtime.observe(frame, 10)
                self.assertFalse(engine.leases)
                self.assertEqual(engine.mutation_epoch, 1)
                self.assertEqual(seen, [payload])
                runtime.observe(frame, 10)
                self.assertEqual(engine.mutation_epoch, 1)
                self.assertEqual(seen, [payload])
                runtime.packet_observers.remove(scoped)
                self.assertEqual(runtime.packet_observers, [])
                engine.leases[row['id']] = engine._stamp(row)
                reply = encrypt_mcp(bytes.fromhex('00807302225a05'), KEY, 3, source=40002)
                runtime.observe(reply, 10)
                self.assertFalse(engine.leases)
                self.assertEqual(engine.mutation_epoch, 2)
                self.assertEqual(runtime.packet_observers, [])
            finally:
                store.close()
