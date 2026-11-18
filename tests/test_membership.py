import unittest
from avionmesh_pi.membership import Membership
from avionmesh_pi.protocol import decrypt_mcp
import test_service as service

class MembershipTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = service.RuntimeTests.asyncSetUp
    asyncTearDown = service.RuntimeTests.asyncTearDown

    async def test_typed_programming_is_uncertain_and_quarantines_group(self):
        self.runtime.capabilities['group_membership'] = True
        data = self.store.inventory()
        data['groups'][0]['members'] = []
        self.store.set('inventory',data)
        result = await Membership(self.runtime).change(40002,291,True)
        self.assertFalse(result['membership_verified'])
        self.assertEqual(self.store.inventory()['groups'][0]['members'],[])
        packet = decrypt_mcp(self.transport.frames[0],KEY)
        self.assertEqual(packet.payload,bytes.fromhex('02030000000123000000'))
        with self.assertRaises(RuntimeError):
            await self.runtime.control(291,{'state':'ON'})
        with self.assertRaises(RuntimeError):
            await Membership(self.runtime).change(40002,291,True)
        self.assertEqual(len(self.transport.frames),1)

    async def test_protected_and_disabled_membership_refuse(self):
        with self.assertRaises(ValueError):
            await Membership(self.runtime).change(40002,291,True)
        self.runtime.capabilities['group_membership'] = True
        with self.assertRaises(ValueError):
            await Membership(self.runtime).change(40002,292,True)
        self.assertEqual(self.transport.frames,[])

from test_service import KEY
