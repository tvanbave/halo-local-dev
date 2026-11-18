import unittest
import test_http as http
from test_membership_verified import GroupTransport
from avionmesh_pi.lifecycle import reload_inventory


@unittest.skipIf(http.create_app is None, 'HTTP dependencies are tested in container')
class MembershipHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await http.HttpTests.asyncSetUp(self)
        data = self.store.inventory()
        data['devices'][1]['product_id'] = 93
        self.store.import_inventory(data)
        reload_inventory(self.runtime)
        self.transport = GroupTransport()
        self.transport.receiver = self.runtime.observe
        self.runtime.transport = self.transport

    asyncTearDown = http.HttpTests.asyncTearDown

    async def test_read_and_batch_are_authenticated_and_capability_gated(self):
        for route, body in [('/api/group-membership/read', {'target': 40002}),
                            ('/api/group-membership/batch', {'targets': [40002], 'group': 291, 'member': False})]:
            response = await self.client.post(route, json=body)
            self.assertEqual(response.status, 401)
            response = await self.client.post(route, headers=self.headers, json=body)
            self.assertEqual(response.status, 501)
        self.assertEqual(self.transport.frames, [])

    async def test_read_and_batch_return_observation_not_only_submission(self):
        self.runtime.capabilities.update(group_membership=True, group_membership_readback=True)
        response = await self.client.post('/api/group-membership/read', headers=self.headers, json={'target': 40002})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['table']['groups'], [291])
        response = await self.client.post('/api/group-membership/batch', headers=self.headers,
                                          json={'targets': [40002], 'group': 291, 'member': False})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())['complete'])
        self.assertEqual(self.transport.mutations, [(40002, 291, False)])

    async def test_uncertain_write_uses_read_only_reconcile_endpoint(self):
        self.runtime.capabilities.update(group_membership=True, group_membership_readback=True)
        self.transport.fail_after_write = True
        response = await self.client.post('/api/group-membership', headers=self.headers,
                                          json={'target': 40002, 'group': 291, 'member': False})
        self.assertEqual(response.status, 409)
        identifier = self.store.journals(pending=True)[0]['id']
        response = await self.client.post('/api/reconcile', headers=self.headers, json={'transaction': identifier})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())['desired_matched'])
        self.assertEqual(len(self.transport.mutations), 1)

    async def test_invalid_or_foreign_requests_do_not_start_batch(self):
        self.runtime.capabilities.update(group_membership=True, group_membership_readback=True)
        response = await self.client.post('/api/group-membership/batch', headers=self.headers,
                                          json={'targets': [40002, 40002], 'group': 291, 'member': True})
        self.assertEqual(response.status, 400)
        response = await self.client.post('/api/group-membership/read',
                                          headers=dict(self.headers, Origin='https://foreign.invalid'), json={'target': 40002})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.transport.frames, [])
