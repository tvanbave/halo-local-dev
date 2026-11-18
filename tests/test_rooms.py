"""Synthetic room UX contract through the real guarded membership implementation."""
from avionmesh_pi.rooms import Rooms
from avionmesh_pi.management import edit_metadata, revision
import test_membership_verified as verified
import unittest


class RoomWorkflowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = verified.VerifiedMembershipTests.asyncSetUp
    asyncTearDown = verified.VerifiedMembershipTests.asyncTearDown
    group = verified.VerifiedMembershipTests.group

    async def test_disabled_room_view_is_metadata_only_and_sends_nothing(self):
        self.runtime.capabilities['group_membership_readback'] = False
        rooms = Rooms(self.runtime)
        view = rooms.overview()
        self.assertFalse(view['programming_enabled'])
        self.assertIn('qualification', view['disabled_reason'])
        self.assertTrue(all(g['membership_evidence'] == 'recorded_metadata' for g in view['rooms']))
        self.assertTrue(all(not g['protected'] for g in view['rooms']))
        with self.assertRaises(ValueError):
            await rooms.change([40003], 291, True)
        self.assertEqual(self.transport.frames, [])

    async def test_named_empty_room_does_not_program_until_explicit_selection(self):
        result = edit_metadata(self.store, {'operation': 'create_group', 'name': 'Evening room',
                                           'revision': revision(self.store)})
        self.assertFalse(result['physical_memberships_changed'])
        room = next(g for g in self.store.inventory()['groups'] if g['name'] == 'Evening room')
        self.assertEqual(room['members'], [])
        self.assertEqual(self.transport.frames, [])
        result = await Rooms(self.runtime).change([40003], room['group_id'], True)
        self.assertTrue(result['physical_memberships_verified'])
        self.assertEqual([row['status'] for row in result['rows']], ['verified'])
        self.assertEqual(self.transport.mutations, [(40003, room['group_id'], True)])
        self.assertEqual(self.group(room['group_id'])['members'], [40003])

    async def test_every_selected_target_validates_before_programming(self):
        for targets in ([40003, 40001], [40003, 40003], [], [True]):
            with self.assertRaises(ValueError):
                await Rooms(self.runtime).change(targets, 291, True)
        self.assertEqual(self.transport.frames, [])

    async def test_failed_write_returns_recovery_and_does_not_try_next_light(self):
        self.transport.fail_after_write = True
        result = await Rooms(self.runtime).change([40003, 40002], 291, True)
        self.assertFalse(result['complete'])
        self.assertFalse(result['automatic_resume'])
        self.assertEqual([r['status'] for r in result['rows']], ['needs_readback', 'not_attempted'])
        row = result['rows'][0]
        self.assertEqual(row['recovery_action'], 'reconcile')
        self.assertEqual(row['transaction'], self.store.journals(pending=True)[0]['id'])
        self.assertEqual(self.group()['members'], [40002])
        self.runtime.capabilities['group_membership'] = False
        recovered = await Rooms(self.runtime).membership.reconcile(row['transaction'])
        self.assertTrue(recovered['desired_matched'])
        self.assertEqual(self.transport.mutations, [(40003, 291, True)])

    async def test_no_receipt_failure_is_not_presented_as_a_write(self):
        self.transport.missing_target = 40003
        result = await Rooms(self.runtime).change([40003, 40002], 291, True)
        self.assertEqual(result['rows'][0]['status'], 'not_verified')
        self.assertIsNone(result['rows'][0]['transaction'])
        self.assertIsNone(result['rows'][0]['recovery_action'])
        self.assertEqual(result['rows'][1]['status'], 'not_attempted')
        self.assertEqual(self.transport.mutations, [])

    async def test_not_applied_is_not_success_and_not_automatic_retry(self):
        self.transport.ignore_write = True
        result = await Rooms(self.runtime).change([40003, 40002], 291, True)
        self.assertFalse(result['physical_memberships_verified'])
        self.assertEqual(result['rows'][0]['status'], 'not_applied')
        self.assertTrue(result['rows'][0]['membership_verified'])
        self.assertEqual(result['rows'][1]['status'], 'not_attempted')
        self.assertEqual(len(self.transport.mutations), 1)

    async def test_remove_is_selected_only_and_preserves_other_room(self):
        self.transport.tables[40002] = [291, 293, 0, 0]
        result = await Rooms(self.runtime).change([40002], 291, False)
        self.assertTrue(result['complete'])
        self.assertEqual(self.transport.tables[40002], [0, 293, 0, 0])
        self.assertEqual(self.transport.mutations, [(40002, 291, False)])
