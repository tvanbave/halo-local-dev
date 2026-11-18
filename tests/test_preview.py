import unittest
import test_http as http
from avionmesh_pi.preview import NoTransport

@unittest.skipIf(http.create_app is None, 'HTTP dependencies are tested in container')
class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await http.HttpTests.asyncSetUp(self)
        await self.client.close()
        from aiohttp.test_utils import TestClient, TestServer
        self.client=TestClient(TestServer(http.create_app(self.runtime,'x'*40,read_only=True)))
        await self.client.start_server()
        self.headers={'Authorization':'Bearer '+'x'*40}
    asyncTearDown=http.HttpTests.asyncTearDown

    async def test_preview_loads_without_sharing_management_token(self):
        response=await self.client.get('/')
        self.assertEqual(response.status,200)
        self.assertIn('READ-ONLY PREVIEW',await response.text())
        response=await self.client.get('/api/snapshot',headers=self.headers)
        self.assertEqual(response.status,200)

    async def test_every_mutating_route_is_denied_before_dispatch(self):
        for path in ('/api/discover','/api/claim','/api/import','/api/metadata','/api/cloud-sync','/api/control'):
            response=await self.client.post(path,headers=self.headers,json={})
            self.assertEqual(response.status,403)
            self.assertEqual((await response.json())['error'],'read_only_preview')

    async def test_host_rebinding_and_cross_site_navigation_rejected(self):
        for headers in ({'Host':'attacker.invalid'},{'Sec-Fetch-Site':'cross-site'}):
            response=await self.client.get('/',headers=headers)
            self.assertEqual(response.status,403)

class NoTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_radio_methods_always_refuse(self):
        transport=NoTransport()
        with self.assertRaises(RuntimeError):await transport.connect()
        with self.assertRaises(RuntimeError):await transport.send(b'x')
