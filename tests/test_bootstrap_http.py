import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import test_http as http
from avionmesh_pi.bootstrap import BootstrapSelection, DeferredBlueZTransport
from avionmesh_pi.bluez import SERVICE

@unittest.skipIf(http.create_app is None,'HTTP dependencies tested in container')
class BootstrapHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await http.HttpTests.asyncSetUp(self)
        # Replace the inherited synthetic fixture with an empty bootstrap fixture.
        self.store.set('inventory',{'devices':[],'groups':[]})
        self.runtime.transport=DeferredBlueZTransport()
        self.runtime.capabilities['ordinary_claim']=True
        self.scanner=SimpleNamespace(discover=AsyncMock(return_value={1:(SimpleNamespace(address='02:00:00:00:00:88'),SimpleNamespace(service_uuids=[SERVICE],rssi=-60))}))
        self.runtime.bootstrap=BootstrapSelection(self.runtime,scanner=self.scanner)
    asyncTearDown=http.HttpTests.asyncTearDown
    async def test_authentication_origin_and_disabled_mode_prevent_scan(self):
        r=await self.client.post('/api/bootstrap/scan',json={});self.assertEqual(r.status,401)
        r=await self.client.post('/api/bootstrap/scan',headers=dict(self.headers,Origin='https://foreign.invalid'),json={});self.assertEqual(r.status,403)
        self.runtime.capabilities['ordinary_claim']=False
        r=await self.client.post('/api/bootstrap/scan',headers=self.headers,json={});self.assertEqual(r.status,501)
        self.scanner.discover.assert_not_awaited()
    async def test_explicit_scan_and_selection_only(self):
        r=await self.client.post('/api/bootstrap/scan',headers=self.headers,json={});self.assertEqual(r.status,200)
        self.assertEqual(len((await r.json())['candidates']),1)
        r=await self.client.post('/api/bootstrap/select',headers=self.headers,json={'address':'02:00:00:00:00:88','owner_reset':False});self.assertEqual(r.status,400)
        r=await self.client.post('/api/bootstrap/select',headers=self.headers,json={'address':'02:00:00:00:00:88','owner_reset':True});self.assertEqual(r.status,200)
        self.assertFalse((await r.json())['association_started'])
        self.assertEqual(self.store.journals(),[])
        self.assertFalse(self.runtime.transport.ready)
