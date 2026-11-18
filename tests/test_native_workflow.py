"""Native Accessory UI claims require the exact retained setup evidence."""
import copy
import unittest

from avionmesh_pi.association import uuid_hash
from avionmesh_pi.controller_workflow import controller_workflow
from avionmesh_pi.management import recovery_advice


class NativeWorkflowTests(unittest.TestCase):
    def evidence(self, *, completed=False):
        claim = {'id': 'synthetic-claim', 'kind': 'claim', 'target': 40001, 'status': 'verified',
                 'detail': {'uuid': '01'*16, 'uuid_hash': uuid_hash(bytes.fromhex('01'*16)),
                            'product_id': 91, 'association_complete': True}}
        group = {'group_id': 260, 'name': 'Synthetic room', 'members': [40002],
                 'protected': False, 'exposed': False}
        args = {'journals': [claim], 'groups': [group], 'mesh_ready': True,
                'capabilities': {'accessory_onboarding': True, 'inventory_complete': True}}
        if completed:
            args['journals'].append({'id': 'synthetic-native', 'kind': 'accessory_native_setup',
                'target': 40001, 'status': 'verified', 'detail': {
                    'mode': 'native', 'product_id': 91, 'claim': claim['id'],
                    'claim_receipt': copy.deepcopy(claim), 'reservation': copy.deepcopy(group),
                    'members': [{'device_id': 40002, 'product_id': 93}],
                    'token': 1, 'original_assignment': 'unknown', 'ping_attempted': True,
                    'assignment_attempted': True, 'assignment_read_back': False,
                    'baseline': {'sequence': 1, 'payload': '06ff8101000000'},
                    'observed': {'sequence': 3, 'payload': '000a01040100ff000000'}}})
        return args

    def view(self, args=None, *, model=91, **overrides):
        return controller_workflow({'device_id': 40001, 'product_id': model,
                                    'name': 'Synthetic dimmer', 'exposed': False},
                                   **((args or self.evidence()) | overrides))

    def test_fresh_native_setup_available_without_mqtt_or_native_baseline(self):
        view = self.view(mqtt_enabled=False, mqtt_ready=False)
        self.assertEqual(view['actions']['setup_native'], {'enabled': True, 'reason': 'available'})
        self.assertFalse(view['actions']['assign_native_target']['enabled'])
        self.assertFalse(view['assignment_read_back'])
        self.assertEqual(view['accessory_native']['state'], 'not_configured')

    def test_change_form_requires_identity_and_editor_but_not_fresh_input_or_mqtt(self):
        for model, flag in ((91, 'dimmer_editor'), (81, 'anyplace_editor')):
            args = self.evidence()
            args['journals'][0]['detail']['product_id'] = model
            args['capabilities'][flag] = True
            view = self.view(args, model=model, mqtt_enabled=False, mqtt_ready=False)
            self.assertTrue(view['actions']['configure_control']['enabled'])
            self.assertIsNone(view['observed_target'])
            self.assertFalse(view['control_configuration']['configured'])
            for overrides in ({'busy': True}, {'preview': True}, {'mesh_ready': False},
                              {'retired_addresses': [40001]}, {'retired_claims': ['synthetic-claim']}):
                self.assertFalse(self.view(args, model=model, **overrides)['actions']['configure_control']['enabled'])
            args['capabilities'][flag] = False
            self.assertFalse(self.view(args, model=model)['actions']['configure_control']['enabled'])

    def test_change_form_reports_retained_native_assignment_without_new_input(self):
        args = self.evidence(completed=True)
        args['capabilities']['dimmer_editor'] = True
        view = self.view(args)
        self.assertEqual(view['control_configuration'], {'configured': True, 'state': 'configured',
            'mode': 'native', 'group': 260, 'transaction': 'synthetic-native', 'assignment_read_back': False})
        self.assertTrue(view['actions']['configure_control']['enabled'])
        args['journals'][-1]['status'] = 'uncertain'
        view = self.view(args)
        self.assertEqual(view['control_configuration']['state'], 'needs_review')
        self.assertFalse(view['actions']['configure_control']['enabled'])

    def test_default_off_and_model_isolation(self):
        self.assertFalse(self.view(capabilities={})['actions']['setup_native']['enabled'])
        for model in (81, 126, 127):
            with self.subTest(model=model):
                self.assertFalse(self.view(model=model)['actions']['setup_native']['enabled'])
                self.assertIsNone(self.view(model=model)['accessory_native'])

    def test_fresh_setup_blocks_preview_busy_offline_and_pending(self):
        for patch, reason in (({'preview': True}, 'read_only_preview'),
                              ({'busy': True}, 'operation_in_progress'),
                              ({'mesh_ready': False}, 'mesh_offline')):
            view = self.view(**patch)
            self.assertEqual(view['actions']['setup_native'], {'enabled': False, 'reason': reason})
        args = self.evidence()
        args['journals'].append({'id': 'other', 'kind': 'remove', 'target': 40002,
                                 'status': 'uncertain', 'detail': {}})
        self.assertEqual(self.view(args)['actions']['setup_native']['reason'], 'resolve_pending_operations')

    def test_incomplete_wrong_or_retired_claim_disables_setup(self):
        for patch in ({'association_complete': False}, {'product_id': 81},
                      {'uuid': '00'*16}, {'uuid_hash': 0}):
            with self.subTest(patch=patch):
                args = self.evidence()
                args['journals'][0]['detail'].update(patch)
                self.assertFalse(self.view(args)['actions']['setup_native']['enabled'])
        for patch in ({'retired_claims': ['synthetic-claim']}, {'retired_addresses': [40001]}):
            self.assertFalse(self.view(**patch)['actions']['setup_native']['enabled'])

    def test_only_ordinary_nonempty_group_enables_setup(self):
        for patch in ({'protected': True}, {'members': []}, {'group_id': 0}, {'group_id': True}):
            args = self.evidence()
            args['groups'][0].update(patch)
            self.assertEqual(self.view(args)['actions']['setup_native']['reason'], 'ordinary_destination_required')
        self.assertFalse(self.view(groups=[])['actions']['setup_native']['enabled'])

    def test_verified_native_receipt_blocks_fresh_native_and_ha_overwrite(self):
        view = self.view(self.evidence(completed=True))
        for action in ('setup_native', 'setup_ha'):
            self.assertFalse(view['actions'][action]['enabled'])
            self.assertEqual(view['actions'][action]['reason'], 'fresh_retained_claim_required')
        self.assertEqual(view['accessory_native'], {'state': 'configured', 'target': 260,
            'transaction': 'synthetic-native', 'observed_in_session': False, 'assignment_read_back': False})
        self.assertEqual(view['accessory_ha']['state'], 'not_configured')
        self.assertFalse(view['assignment_read_back'])

    def test_verified_receipt_does_not_imply_current_session_observation(self):
        args = self.evidence(completed=True)
        observed = ({'target': 260, 'sequence': 4, 'target_generation': 0}, 100)
        self.assertTrue(self.view(args, native_observation=observed, now=101)
                        ['accessory_native']['observed_in_session'])
        self.assertFalse(self.view(args, native_observation=observed, now=15101)
                         ['accessory_native']['observed_in_session'])

    def test_uncertain_setup_needs_observation_only_recovery(self):
        args = self.evidence(completed=True)
        args['journals'][-1]['status'] = 'uncertain'
        view = self.view(args)
        self.assertEqual(view['accessory_native']['state'], 'needs_review')
        self.assertEqual(view['recovery'][0]['mode'], 'observation_only')
        self.assertFalse(view['recovery'][0]['automatic_resume'])
        advice = recovery_advice(args['journals'][-1])
        self.assertEqual(advice['actions'], ['reconcile'])
        self.assertNotIn('observe_owner_reset', advice['actions'])

    def test_corrupt_or_missing_proof_cannot_label_native_configured(self):
        patches = ({'mode': 'ha'}, {'product_id': 81}, {'assignment_attempted': False},
                   {'assignment_read_back': True}, {'claim': 'another-claim'},
                   {'claim_receipt': {}}, {'observed': {}},
                   {'observed': {'sequence': 3, 'payload': 'bad'}},
                   {'observed': {'sequence': 3, 'payload': '000a01050100ff000000'}},
                   {'observed': {'sequence': 1, 'payload': '000a01040100ff000000'}})
        for patch in patches:
            with self.subTest(patch=patch):
                args = self.evidence(completed=True)
                args['journals'][-1]['detail'].update(patch)
                self.assertEqual(self.view(args)['accessory_native']['state'], 'needs_review')

    def test_changed_or_ambiguous_claim_cannot_label_native_configured(self):
        args = self.evidence(completed=True)
        args['journals'][0]['detail']['association_complete'] = False
        self.assertEqual(self.view(args)['accessory_native']['state'], 'needs_review')
        args = self.evidence(completed=True)
        other = copy.deepcopy(args['journals'][0])
        other['id'] = 'another-claim'
        args['journals'].append(other)
        self.assertEqual(self.view(args)['accessory_native']['state'], 'needs_review')
        self.assertEqual(self.view(self.evidence(completed=True), retired_addresses=[40001])
                         ['accessory_native']['state'], 'needs_review')

    def test_later_native_edit_supersedes_fresh_setup_status(self):
        args = self.evidence(completed=True)
        args['journals'].append({'id': 'later-edit', 'kind': 'dimmer_edit', 'target': 40001,
                                 'status': 'verified', 'detail': {}})
        self.assertEqual(self.view(args)['accessory_native']['state'], 'needs_review')

    def test_ordinary_group_metadata_changes_do_not_erase_verified_target(self):
        args = self.evidence(completed=True)
        args['groups'][0].update(name='Renamed room', members=[40003], exposed=True)
        self.assertEqual(self.view(args)['accessory_native']['state'], 'configured')
        args['groups'][0]['protected'] = True
        self.assertEqual(self.view(args)['accessory_native']['state'], 'needs_review')
        self.assertEqual(self.view(self.evidence(completed=True), groups=[])
                         ['accessory_native']['state'], 'needs_review')

    def test_workflow_never_mutates_journals_or_groups(self):
        args = self.evidence(completed=True)
        original = copy.deepcopy(args)
        self.view(args)
        self.assertEqual(args, original)
