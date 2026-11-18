import unittest
from avionmesh_pi.ha_readiness import assess


class ReadinessTests(unittest.TestCase):
    def snapshot(self, **status):
        return {'status': dict(dict(mqtt_enabled=True, mqtt_ready=True, mesh_ready=True,
                               pending_transactions=0), **status), 'inventory': {'devices': [
            {'device_id': 40002, 'product_id': 93, 'exposed': True},
            {'device_id': 40003, 'product_id': 93, 'exposed': False}]}}

    def test_connected_is_not_ha_acceptance(self):
        result = assess(self.snapshot())
        self.assertEqual(result['mqtt'], 'connected')
        self.assertEqual(result['ha_acceptance'], 'not_established_by_snapshot')
        self.assertEqual(result['supported_exposed_devices'], 1)
        self.assertEqual(result['hidden_devices'], 1)

    def test_disconnected_broker_does_not_disable_local_controls(self):
        snapshot = self.snapshot()
        snapshot['status']['mqtt_ready'] = False
        result = assess(snapshot)
        self.assertEqual(result['mqtt'], 'disconnected')
        self.assertEqual(result['local_control'], 'potentially_available')

    def test_disabled_pending_and_mesh_off_are_separate(self):
        snapshot = self.snapshot()
        snapshot['status'].update(mqtt_enabled=False, mqtt_ready=False, mesh_ready=False, pending_transactions=1)
        result = assess(snapshot)
        self.assertEqual((result['mqtt'], result['mesh'], result['recovery']),
                         ('disabled', 'disconnected', 'review_required'))

    def test_unknown_recovery_fails_closed_and_secrets_never_echo(self):
        snapshot = self.snapshot()
        snapshot['status'].pop('pending_transactions')
        snapshot['status']['last_error'] = 'password=synthetic-secret'
        snapshot['inventory']['devices'][0]['private_key'] = 'synthetic-secret'
        result = assess(snapshot)
        self.assertEqual(result['recovery'], 'review_required')
        self.assertNotIn('synthetic-secret', str(result))

    def test_invalid_snapshot_rejected(self):
        for snapshot in (None, [], {}, {'status': {}, 'inventory': []},
                         {'status': {}, 'inventory': {'devices': [None]}}):
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                assess(snapshot)

    def test_unknown_or_boolean_recovery_count_is_not_clear(self):
        for pending in (None, False, True, -1, '0', 0.0):
            with self.subTest(pending=pending):
                self.assertEqual(assess(self.snapshot(pending_transactions=pending))['recovery'], 'review_required')

    def test_result_is_an_allowlisted_copy_not_raw_diagnostics(self):
        snapshot = self.snapshot()
        snapshot['inventory']['groups'] = [{'protected': True, 'key': 'synthetic-secret'}]
        snapshot['status']['diagnostics'] = {'raw': 'synthetic-secret'}
        before = repr(snapshot)
        result = assess(snapshot)
        self.assertNotIn('synthetic-secret', repr(result))
        self.assertEqual(repr(snapshot), before)
