"""A19 uses the ESP group contract, including all guarded failure cases."""
import json
import test_membership_verified as verified
import test_rooms_http as rooms_http
from avionmesh_pi.lifecycle import reload_inventory
from avionmesh_pi.management import recovery_advice


class A19MembershipTests(verified.VerifiedMembershipTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        data = self.store.inventory()
        for device in data['devices']:
            if device['device_id'] in (40002, 40003):
                device['product_id'] = 134
        self.store.import_inventory(data)
        reload_inventory(self.runtime)

    async def test_a19_receipt_and_mixed_batch_retain_model_identity(self):
        data = self.store.inventory()
        next(d for d in data['devices'] if d['device_id'] == 40002)['product_id'] = 93
        self.store.import_inventory(data)
        reload_inventory(self.runtime)
        result = await self.member.change_many([40002, 40003], 293, True)
        self.assertTrue(result['complete'])
        self.assertEqual(self.transport.mutations, [(40002, 293, True), (40003, 293, True)])
        receipts = self.store.journals()
        self.assertEqual([r['detail']['readback_schema'] for r in receipts], ['rl4-groups-v1', 'a19-groups-v1'])
        self.assertEqual([r['detail']['product_id'] for r in receipts], [93, 134])
        self.assertEqual(self.transport.tables[40002][:2], [291, 293])

    async def test_a19_recovery_refuses_changed_model_without_sending_reads(self):
        self.transport.fail_after_write = True
        with self.assertRaises(RuntimeError):
            await self.member.change(40003, 293, True)
        journal = self.store.journals(pending=True)[0]
        self.assertEqual(recovery_advice(journal)['actions'], ['reconcile'])
        data = self.store.inventory()
        next(d for d in data['devices'] if d['device_id'] == 40003)['product_id'] = 93
        with self.assertRaisesRegex(ValueError, 'Resolve pending'):
            self.store.import_inventory(data)
        # Inject inconsistent synthetic state below the normal import guard to
        # verify recovery's independent model check; never used on real state.
        self.store.set('inventory', data)
        reload_inventory(self.runtime)
        frames = list(self.transport.frames)
        with self.assertRaisesRegex(ValueError, 'receipt model'):
            await self.member.reconcile(journal['id'])
        self.assertEqual(self.transport.frames, frames)
        self.assertEqual(self.store.journals(pending=True)[0], journal)

    async def test_a19_recovery_refuses_mismatched_receipt_product(self):
        self.transport.fail_after_write = True
        with self.assertRaises(RuntimeError):
            await self.member.change(40003, 293, True)
        journal = self.store.journals(pending=True)[0]
        journal['detail']['product_id'] = 93
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(journal['detail']), journal['id']))
        frames = list(self.transport.frames)
        with self.assertRaisesRegex(ValueError, 'receipt model'):
            await self.member.reconcile(journal['id'])
        self.assertEqual(self.transport.frames, frames)


class A19RoomsHttpTests(rooms_http.RoomsHttpTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        data = self.store.inventory()
        next(d for d in data['devices'] if d['device_id'] == 40002)['product_id'] = 134
        self.store.import_inventory(data)
        reload_inventory(self.runtime)
