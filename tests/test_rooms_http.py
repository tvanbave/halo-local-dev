"""Authenticated synthetic HTTP room setup; physical gates stay off by default."""
import unittest
import test_http as http
import test_membership_http as membership_http


@unittest.skipIf(http.create_app is None, 'HTTP dependencies are tested in container')
class RoomsHttpTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = membership_http.MembershipHttpTests.asyncSetUp
    asyncTearDown = membership_http.MembershipHttpTests.asyncTearDown

    async def test_room_overview_is_authenticated_metadata_not_physical_proof(self):
        response = await self.client.get('/api/rooms')
        self.assertEqual(response.status, 401)
        response = await self.client.get('/api/rooms', headers=self.headers)
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertFalse(result['programming_enabled'])
        self.assertTrue(all(g['membership_evidence'] == 'recorded_metadata' for g in result['rooms']))
        self.assertEqual(self.transport.frames, [])

    async def test_room_write_requires_both_capabilities_and_authentication(self):
        body = {'targets': [40002], 'group': 291, 'member': False}
        response = await self.client.post('/api/rooms/membership', json=body)
        self.assertEqual(response.status, 401)
        for readback, programming in ((False, False), (True, False), (False, True)):
            self.runtime.capabilities.update(group_membership=programming, group_membership_readback=readback)
            response = await self.client.post('/api/rooms/membership', headers=self.headers, json=body)
            self.assertEqual(response.status, 501)
        self.assertEqual(self.transport.frames, [])

    async def test_explicit_selection_returns_actual_readback(self):
        self.runtime.capabilities.update(group_membership=True, group_membership_readback=True)
        response = await self.client.post('/api/rooms/membership', headers=self.headers,
                                          json={'targets': [40002], 'group': 291, 'member': False})
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertTrue(result['physical_memberships_verified'])
        self.assertEqual(result['rows'][0]['status'], 'verified')
        self.assertEqual(self.transport.mutations, [(40002, 291, False)])

    async def test_uncertain_programming_has_receipt_not_success(self):
        self.runtime.capabilities.update(group_membership=True, group_membership_readback=True)
        self.transport.fail_after_write = True
        response = await self.client.post('/api/rooms/membership', headers=self.headers,
                                          json={'targets': [40002], 'group': 291, 'member': False})
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertFalse(result['physical_memberships_verified'])
        self.assertEqual(result['rows'][0]['status'], 'needs_readback')
        self.assertEqual(result['rows'][0]['transaction'], self.store.journals(pending=True)[0]['id'])
        self.assertFalse(result['automatic_resume'])

    async def test_malformed_or_foreign_request_never_programs(self):
        self.runtime.capabilities.update(group_membership=True, group_membership_readback=True)
        for body in ({'targets': [], 'group': 291, 'member': True},
                     {'targets': [40002, 40002], 'group': 291, 'member': True},
                     {'targets': [40002], 'group': 291, 'member': True, 'resume': True}):
            response = await self.client.post('/api/rooms/membership', headers=self.headers, json=body)
            self.assertEqual(response.status, 400)
        response = await self.client.post('/api/rooms/membership', headers=dict(self.headers, Origin='https://foreign.invalid'),
                                          json={'targets': [40002], 'group': 291, 'member': True})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.transport.frames, [])
