"""Per-device mesh hop counts and recent commanded levels, for display only. Synthetic."""
import tempfile
import unittest

import test_http as http

from avionmesh_pi.mesh_reports import LIGHT_ORIGIN_HOP_COUNT, MeshReports
from avionmesh_pi.protocol import encrypt_mcp
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.timing import CommandTimings
from support import synthetic_store
from test_service import KEY

RELAY = '02:00:00:00:00:A1'
OTHER = '02:00:00:00:00:A2'
DEVICES = {32902: 93, 32903: 134, 32904: 127}


class MeshReportTests(unittest.TestCase):
    def setUp(self):
        self.reports = MeshReports(capacity=3)

    def view(self, now=1000, relay=RELAY):
        return self.reports.view(DEVICES, now, relay)

    def test_light_hops_count_down_from_fifty(self):
        self.reports.observe(32902, 1, 50, 900, RELAY)
        self.reports.observe(32903, 2, 47, 950, RELAY)
        view = self.view()
        self.assertEqual(LIGHT_ORIGIN_HOP_COUNT, 50)
        self.assertEqual((view['32902']['hops'], view['32903']['hops']), (0, 3))
        self.assertEqual((view['32903']['hop_count'], view['32903']['observed_ms'], view['32903']['age_ms']), (47, 950, 50))
        self.assertIs(view['32903']['via_current_relay'], True)

    def test_copies_of_one_message_keep_the_shortest_path(self):
        self.reports.observe(32903, 7, 46, 900, RELAY)
        self.reports.observe(32903, 7, 48, 901, RELAY)
        self.reports.observe(32903, 7, 45, 902, RELAY)
        self.assertEqual(self.view()['32903']['hops'], 2)
        # A new message is a new reading, even if it took a longer path.
        self.reports.observe(32903, 8, 45, 903, RELAY)
        self.assertEqual(self.view()['32903']['hops'], 5)

    def test_controllers_unknown_devices_and_impossible_counts_show_no_hops(self):
        self.reports.observe(32904, 1, 19, 900, RELAY)
        self.reports.observe(39999, 1, 49, 900, RELAY)
        self.reports.observe(32902, 1, 51, 900, RELAY)
        view = self.view()
        self.assertEqual((view['32904']['hops'], view['32904']['hop_count']), (None, 19))
        self.assertNotIn('39999', view)
        self.assertIsNone(view['32902']['hops'])

    def test_readings_through_an_earlier_relay_are_marked(self):
        self.reports.observe(32902, 1, 49, 900, OTHER)
        self.assertIs(self.view()['32902']['via_current_relay'], False)
        self.assertIs(self.view(relay=None)['32902']['via_current_relay'], False)
        # The same sequence through a different relay is a different reading.
        self.reports.observe(32902, 1, 47, 950, RELAY)
        self.assertEqual((self.view()['32902']['hops'], self.view()['32902']['via_current_relay']), (3, True))

    def test_bad_input_is_ignored_and_capacity_is_bounded(self):
        for args in ((None, 1, 49, 900, RELAY), (32902, '1', 49, 900, RELAY), (32902, 1, 256, 900, RELAY),
                     (32902, 1, -1, 900, RELAY), (32902, 1, True, 900, RELAY), (32902, 1, 49, 9.5, RELAY)):
            self.reports.observe(*args)
        self.assertEqual(self.reports.latest, {})
        for source, at in ((1, 10), (2, 20), (3, 30), (4, 40)):
            self.reports.observe(source, 1, 49, at, RELAY)
        self.assertEqual(sorted(self.reports.latest), [2, 3, 4])


class LatestLevelTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.t = CommandTimings(lambda: self.now, 'avionmesh_pi/test')

    def command(self, target=258, level=255, source='mqtt', send=True, members=(32902, 32903)):
        identifier = self.t.start(target, members, {'brightness': level}, source=source, received_ms=self.now)
        self.t.dispatch(identifier)
        if send:
            self.t.sent(identifier)
        return identifier

    def test_newest_sent_level_per_target_within_window(self):
        self.command(level=0)
        self.now = 1100
        self.command(level=255)
        self.command(target=32902, level=128, members=(32902,))
        latest = self.t.latest_levels()
        self.assertEqual(latest['258'], {'brightness': 255, 'sent_ms': 1100, 'age_ms': 0, 'source': 'mqtt',
                                         'members': [32902, 32903]})
        self.assertEqual(latest['32902']['brightness'], 128)
        self.now = 1100 + 15001
        self.assertEqual(self.t.latest_levels(), {})

    def test_failed_unstarted_unknown_level_and_old_session_commands_are_excluded(self):
        failed = self.command(send=False)
        self.t.failed(failed)
        self.t.start(259, (32902,), {'brightness': 255}, source='ui', received_ms=self.now)  # never dispatched
        identifier = self.t.start(260, (32902,), {'brightness': None}, source='native', received_ms=self.now)
        self.t.dispatch(identifier)
        self.t.sent(identifier)
        self.assertEqual(self.t.latest_levels(), {})
        self.command(target=261)
        self.t.boundary()
        self.assertEqual(self.t.latest_levels(), {})


class RuntimeTransport:
    ready = True
    address = RELAY

    async def send(self, *_args, **_kwargs):
        return True


class RuntimeMeshReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = synthetic_store(self.temp.name, KEY)
        self.store.import_inventory({'devices': [
            {'device_id': 32902, 'product_id': 93, 'name': 'Relay light', 'exposed': False},
            {'device_id': 32903, 'product_id': 93, 'name': 'Far light', 'exposed': False}], 'groups': []})
        self.runtime = Runtime(self.store, KEY, RuntimeTransport(), clock=lambda: 10)
        self.runtime.mesh_ready = True

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def frame(self, source, sequence, hop):
        return encrypt_mcp(b'\x00\x80\x73\x00\x0a\x00\x80', KEY, sequence, source)[:-1] + bytes([hop])

    def test_authenticated_messages_are_recorded_with_their_relay(self):
        self.runtime.observe(self.frame(32902, 1, 50), 10)
        self.runtime.observe(self.frame(32903, 2, 48), 10)
        self.runtime.observe(b'\x00' * 20, 10)  # fails authentication: never recorded
        view = self.runtime.mesh_reports.view({32902: 93, 32903: 93}, 10, RELAY)
        self.assertEqual((view['32902']['hops'], view['32903']['hops']), (0, 2))
        self.assertEqual(view['32903']['relay_address'], RELAY)
        self.assertEqual(set(self.runtime.mesh_reports.latest), {32902, 32903})

    def test_a_failing_recorder_never_disturbs_receiving(self):
        def broken(*_args):
            raise RuntimeError('synthetic')
        self.runtime.mesh_reports.observe = broken
        before = self.runtime.receive_count
        self.runtime.observe(self.frame(32902, 1, 50), 10)
        self.assertEqual(self.runtime.receive_count, before + 1)
        self.assertEqual(self.runtime.core.counts['mesh_report_failed'], 1)


@unittest.skipIf(http.create_app is None, 'HTTP dependencies are tested in the container')
class SnapshotFieldTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = http.HttpTests.asyncSetUp
    asyncTearDown = http.HttpTests.asyncTearDown

    async def test_snapshot_carries_display_hints(self):
        response = await self.client.get('/api/snapshot', headers=self.headers)
        data = await response.json()
        self.assertEqual(data['recent_commands'], {})
        self.assertEqual(data['mesh_reports'], {})


if __name__ == '__main__':
    unittest.main()
