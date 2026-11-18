"""Group device metadata must preserve existing entity and command identities."""
import tempfile
import unittest
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import KEY, INVENTORY, FakeTransport

class GroupDeviceDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_adds_stable_group_device_without_changing_entity_or_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp, KEY)
            try:
                store.import_inventory(INVENTORY)
                runtime = Runtime(store, KEY, FakeTransport(), clock=lambda:10)
                def config():
                    return next(m for m in runtime.discovery() if m['topic'] == 'homeassistant/light/pi_lab_group_291/config')
                first = config()
                p = first['payload']
                self.assertEqual(p['unique_id'], 'pi_lab_group_291')
                self.assertEqual(p['device']['identifiers'], [p['unique_id']])
                self.assertEqual(p['device']['model'], 'Mesh light group')
                self.assertEqual(p['command_topic'], 'avionmesh_pi/pi_lab/light/291/set')
                inventory = store.inventory()
                next(g for g in inventory['groups'] if g['group_id']==291)['name'] = 'Renamed room'
                store.import_inventory(inventory)
                renamed = config()
                self.assertEqual(renamed['topic'],first['topic'])
                self.assertEqual(renamed['payload']['unique_id'],p['unique_id'])
                self.assertEqual(renamed['payload']['device']['identifiers'],p['device']['identifiers'])
                self.assertEqual(renamed['payload']['device']['name'],'Renamed room')
            finally:
                store.close()
