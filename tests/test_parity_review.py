"""Independent synthetic presentation review. No radio, sockets, or private state."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from avionmesh_pi.rooms import Rooms


class RoomOutcomeReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_publication_failure_never_hides_unapplied_membership(self):
        runtime = SimpleNamespace(
            capabilities={'group_membership': True, 'group_membership_readback': True},
            store=SimpleNamespace(
                inventory=lambda: {'devices': [{'device_id': 40001, 'name': 'Synthetic light'}]},
                journals=lambda **kwargs: []))
        rooms = Rooms(runtime)
        rooms.membership.change_many = AsyncMock(return_value={
            'results': [{'target': 40001, 'membership_verified': True,
                         'desired_matched': False, 'publication_pending': True,
                         'requires_review': True, 'retry_allowed': False}],
            'remaining': [], 'complete': False, 'automatic_resume': False})
        result = await rooms.change([40001], 291, True)
        self.assertFalse(result['physical_memberships_verified'])
        self.assertEqual(result['rows'][0]['status'], 'not_applied')
        self.assertTrue(result['rows'][0]['publication_pending'])


if __name__ == '__main__':
    unittest.main()
