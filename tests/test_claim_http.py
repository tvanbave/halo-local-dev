import unittest
from unittest.mock import AsyncMock, patch
import test_http as http
from avionmesh_pi.commissioning import Commissioning

@unittest.skipIf(http.create_app is None, 'HTTP dependencies are tested in container')
class ClaimHttpTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = http.HttpTests.asyncSetUp
    asyncTearDown = http.HttpTests.asyncTearDown

    async def test_current_first_device_models_and_remaining_claims(self):
        from types import SimpleNamespace
        for automatic in (True, False):
            self.runtime.bootstrap = SimpleNamespace(status=lambda: {'needs_first_claim':True, 'automatic':automatic})
            response = await self.client.get('/api/status', headers=self.headers)
            pairing = (await response.json())['pairing']
            self.assertEqual(134 in pairing['models'], automatic)
            self.assertIn(93, pairing['models'])
            self.assertEqual(pairing['attempts_remaining'], 128)
        identifier = self.store.begin('claim',40051,{})
        self.store.transition(identifier,'prepared','cancelled')
        response = await self.client.get('/api/status', headers=self.headers)
        self.assertEqual((await response.json())['pairing']['attempts_remaining'],127)

    async def test_claim_routes_require_auth_and_foreign_origins_are_refused(self):
        for route,body in [('/api/claim/batch',{'devices':[]}),('/api/claim/reset-reconcile',{'transaction':'x','owner_reset':True})]:
            response=await self.client.post(route,json=body)
            self.assertEqual(response.status,401)
            response=await self.client.post(route,headers=dict(self.headers,Origin='https://foreign.invalid'),json=body)
            self.assertEqual(response.status,403)
        response=await self.client.post('/api/claim/batch',headers=self.headers,json={'devices':[]})
        self.assertEqual(response.status,501)
        response=await self.client.post('/api/claim/reset-reconcile',headers=self.headers,json={'transaction':'x','owner_reset':True})
        self.assertEqual(response.status,400)

    async def test_batch_forwards_explicit_inventory_capability_and_validates_shape(self):
        self.runtime.capabilities.update(ordinary_claim=True,bulk_claim=True,inventory_complete=True)
        with patch.object(Commissioning,'claim_many',new_callable=AsyncMock,return_value={'complete':False,'automatic_resume':False}) as claim:
            response=await self.client.post('/api/claim/batch',headers=self.headers,json={'devices':[],'unexpected':True})
            self.assertEqual(response.status,400)
            claim.assert_not_awaited()
            response=await self.client.post('/api/claim/batch',headers=self.headers,json={'devices':[]})
            self.assertEqual(response.status,200)
            claim.assert_awaited_once_with([],inventory_complete=True)
