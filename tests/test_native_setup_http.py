"""Synthetic API checks for fresh native setup and reviewed removal."""
import asyncio
from unittest.mock import AsyncMock, patch
from test_http import HttpTests

class NativeSetupHttpTests(HttpTests):
    # Use the existing synthetic HTTP harness, without duplicating its tests.
    async def test_native_setup_routes_one_job_and_requires_typed_known_accessory(self):
        self.runtime.capabilities['accessory_onboarding'] = True
        inventory = self.store.inventory()
        target = inventory['devices'][0]['device_id']
        inventory['devices'][0]['product_id'] = 91
        self.store.import_inventory(inventory)
        from avionmesh_pi.lifecycle import reload_inventory
        reload_inventory(self.runtime)
        self.runtime.mesh_ready = True
        with patch('avionmesh_pi.accessory.AccessoryOnboarding.setup_native', new=AsyncMock(return_value={'ready':True})) as setup:
            response = await self.client.post('/api/controller/setup-native', headers=self.headers,
                json={'target':target,'group':291})
            self.assertEqual(response.status,202)
            await asyncio.sleep(0.01)
            setup.assert_awaited_once_with(target,291)
        for body in ({'target':target}, {'target':True,'group':291}, {'target':target,'group':'291'}):
            response = await self.client.post('/api/controller/setup-native', headers=self.headers,json=body)
            self.assertEqual(response.status,400)

    async def test_removal_preview_read_only_even_when_disabled_and_revision_required(self):
        with patch('avionmesh_pi.lifecycle.Lifecycle.preview_remove',return_value={'eligible':False,'blockers':['removal_disabled']}) as preview:
            response = await self.client.post('/api/unclaim/preview',headers=self.headers,json={'target':40001})
            self.assertEqual(response.status,200)
            preview.assert_called_once_with(40001)
        self.runtime.capabilities['removal'] = True
        with patch('avionmesh_pi.lifecycle.Lifecycle.remove_and_verify',new=AsyncMock(return_value={'removed':False})) as remove:
            response = await self.client.post('/api/unclaim',headers=self.headers,json={'target':40001})
            self.assertEqual(response.status,409)
            body = await response.json()
            self.assertEqual(body['error'],'removal_review_changed')
            self.assertFalse(body['operation_started'])
            self.assertFalse(body['retry_allowed'])
            remove.assert_not_awaited()
            self.assertEqual(self.transport.frames,[])
            self.assertEqual(self.store.journals(),[])
            with patch('avionmesh_pi.lifecycle.Lifecycle.preview_remove',return_value={
                    'eligible':True,'enabled':True,'blockers':[],'revision':'reviewed'}):
                response = await self.client.post('/api/unclaim',headers=self.headers,json={'target':40001,'revision':'reviewed'})
            self.assertEqual(response.status,200)
            remove.assert_awaited_once_with(40001,expected_revision='reviewed')

    async def test_removal_http_verifies_after_late_scan_without_resending(self):
        from avionmesh_pi.association import uuid_hash
        from avionmesh_pi.lifecycle import Lifecycle
        from test_lifecycle import UUID
        self.runtime.capabilities['removal'] = True
        claim = self.store.begin('claim', 40002, {'uuid':UUID,
            'uuid_hash':uuid_hash(bytes.fromhex(UUID)), 'product_id':134})
        self.store.transition(claim,'prepared','attempted')
        self.store.transition(claim,'attempted','verified')
        preview = await self.client.post('/api/unclaim/preview',headers=self.headers,json={'target':40002})
        review = await preview.json()
        observed = [{'uuid':UUID,'uuid_hash':uuid_hash(bytes.fromhex(UUID))}]
        with patch('avionmesh_pi.commissioning.Commissioning.discover',new=AsyncMock(side_effect=[[],observed])) as scans:
            response = await self.client.post('/api/unclaim',headers=self.headers,
                json={'target':40002,'revision':review['revision']})
        self.assertEqual(response.status,200)
        result = await response.json()
        self.assertTrue(result['removed'])
        self.assertEqual(result['verification_attempts'],2)
        self.assertFalse(result['needs_exact_uuid_scan'])
        self.assertEqual(scans.await_count,2)
        self.assertEqual(len(self.transport.frames),1)
        self.assertNotIn(40002,self.runtime.core.devices)
        self.assertFalse(self.store.journals(pending=True))

# Avoid running the inherited HTTP suite a second time.
for name in tuple(vars(HttpTests)):
    if name.startswith('test_') and name not in vars(NativeSetupHttpTests):
        setattr(NativeSetupHttpTests,name,None)
del HttpTests
