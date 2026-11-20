import copy
import unittest
from types import SimpleNamespace

from avionmesh_pi.controller_workflow import controller_workflow, controller_workflows
from avionmesh_pi.keypad import snapshot


def table(targets=(65535, 500, 501, 502)):
    rows = [bytes([1, 48, 0, i]) + target.to_bytes(2, 'big') +
            (b'\0\0' if target == 65535 else b'\x08\x80')
            for i, target in enumerate(targets, 1)]
    return snapshot(bytes([4, 48, 0, sum(t != 65535 for t in targets), 4]), rows)


class ControllerWorkflowTests(unittest.TestCase):
    def view(self, model=127, **kwargs):
        return controller_workflow({'device_id': 32900, 'product_id': model, 'exposed': True}, **kwargs)

    def test_noncontroller_omitted(self):
        self.assertIsNone(self.view(93))

    def test_default_off_writers_each_model(self):
        for model in (127, 126, 91, 81):
            with self.subTest(model=model):
                view = self.view(model, mesh_ready=True, table=table())
                for action in ('edit_assignment', 'setup_ha', 'assign_native_target', 'clear_native_assignment'):
                    self.assertFalse(view['actions'][action]['enabled'])
                self.assertEqual(view['actions']['read_assignments']['enabled'], model in (126, 127))

    def test_wired_table_presets_and_preservation(self):
        view = self.view(mesh_ready=True, table=table(), capabilities={'wired_editor': True, 'wired_onboarding': True, 'inventory_complete': True})
        self.assertTrue(view['actions']['edit_assignment']['enabled'])
        self.assertTrue(view['actions']['setup_ha']['enabled'])
        self.assertEqual([r['preserved_by_setup'] for r in view['slots']], [False, True, True, True])
        self.assertEqual(view['slots'][1]['preset'], 128)
        self.assertTrue(view['assignment_read_back'])

    def test_battery_never_inherits_wired_gates(self):
        view = self.view(126, mesh_ready=True, table=table(), capabilities={'wired_editor': True, 'wired_onboarding': True, 'inventory_complete': True})
        self.assertFalse(view['actions']['edit_assignment']['enabled'])
        self.assertFalse(view['actions']['setup_ha']['enabled'])
        self.assertIn('20-second cooldown', ' '.join(view['notes']))
        view = self.view(126, mesh_ready=True, table=table(), capabilities={'battery_editor': True})
        self.assertTrue(view['actions']['edit_assignment']['enabled'])

    def battery_setup_evidence(self):
        current = table((24575, 500, 501, 502))
        rows = [bytes.fromhex(row['raw']) for row in current['slots']]
        rows[0] = rows[0][:-1] + b'\xff'
        current = snapshot(bytes.fromhex(current['count']), rows)
        group = {'group_id': 24575, 'name': 'Private event destination', 'members': [],
                 'protected': True, 'exposed': False}
        receipt = {'id': 'battery-setup', 'kind': 'keypad_setup', 'status': 'verified', 'target': 32900,
                   'detail': {'product_id': 126, 'planned': [1], 'desired': copy.deepcopy(current),
                              'reservations': [copy.deepcopy(group)]}}
        return {'table': current, 'groups': [group], 'journals': [receipt]}

    def test_battery_setup_proof_matches_receipt_current_table_and_owned_slots(self):
        args = self.battery_setup_evidence()
        value = self.view(126, **args)
        self.assertEqual(value['keypad_ha_setup'], {'transaction': 'battery-setup',
            'slots': [1], 'revision': args['table']['revision']})
        self.assertEqual(value['slots'][0]['assignment_mode'], 'ha')
        self.assertFalse(value['slots'][0]['ha_available'], 'battery editing requires its capability and current table')
        self.assertEqual(value['slots'][1]['assignment_mode'], 'native')
        self.assertFalse(value['slots'][0]['ha_ready'], 'setup evidence does not imply MQTT readiness')

    def test_battery_owned_slots_can_switch_modes_with_own_editor_enabled(self):
        args = self.battery_setup_evidence()
        view = self.view(126, **args, mesh_ready=True,
            capabilities={'battery_editor': True, 'inventory_complete': True})
        self.assertTrue(view['slots'][0]['editable'])
        self.assertTrue(view['slots'][0]['ha_available'])
        self.assertTrue(view['slots'][1]['ha_available'])
        view = self.view(126, **args, mesh_ready=True,
            capabilities={'wired_editor': True, 'inventory_complete': True})
        self.assertFalse(view['slots'][0]['editable'])
        self.assertFalse(view['slots'][0]['ha_available'])

    def test_battery_setup_proof_rejects_stale_foreign_or_unowned_evidence(self):
        for defect in ('uncertain', 'wrong_model', 'wrong_owner', 'changed_table', 'no_table',
                       'changed_reservation', 'wrong_slot', 'duplicate_slot', 'no_planned'):
            with self.subTest(defect=defect):
                args = self.battery_setup_evidence()
                receipt = args['journals'][0]
                if defect == 'uncertain': receipt['status'] = 'uncertain'
                elif defect == 'wrong_model': receipt['detail']['product_id'] = 127
                elif defect == 'wrong_owner': receipt['target'] = 32901
                elif defect == 'changed_table': args['table'] = table()
                elif defect == 'no_table': args['table'] = None
                elif defect == 'changed_reservation': args['groups'][0]['name'] = 'Changed reservation'
                elif defect == 'wrong_slot': receipt['detail']['planned'] = [2]
                elif defect == 'duplicate_slot': receipt['detail']['planned'] = [1, 1]
                else: receipt['detail']['planned'] = []
                self.assertIsNone(self.view(126, **args)['keypad_ha_setup'])

    def test_protected_assignments_are_read_only_in_runtime_workflow(self):
        groups = [{'group_id': 291, 'protected': False}, {'group_id': 24575, 'protected': True}]
        binding = table((291, 65535, 24575, 500))
        for model, flag in ((127, 'wired_editor'), (126, 'battery_editor')):
            with self.subTest(model=model):
                devices = [{'device_id': 32900, 'product_id': model, 'exposed': True}]
                store = SimpleNamespace(journals=lambda: [],
                    inventory=lambda: {'devices': devices, 'groups': groups}, get=lambda k, default: default)
                router = SimpleNamespace(bindings={32900: binding}, local_modes=set(), baselines={})
                runtime = SimpleNamespace(store=store, core=SimpleNamespace(controllers=router),
                    capabilities={flag: True}, mesh_ready=True, controller_actions_busy=False,
                    native_observations={}, clock=lambda: 0, mqtt_enabled=False, mqtt_ready=False)
                view = controller_workflows(runtime)[0]
                self.assertTrue(view['assignment_read_back'])
                self.assertTrue(view['actions']['edit_assignment']['enabled'])
                self.assertEqual([row['editable'] for row in view['slots']], [True, True, False, True])
                self.assertEqual(view['slots'][2]['target'], 24575)
                self.assertTrue(view['slots'][2]['preserved_by_setup'])

    def test_fresh_table_required_and_invalid_tables_rejected(self):
        for value in (None, {}, {'slots': []}, dict(table(), revision='wrong')):
            view = self.view(mesh_ready=True, table=value, capabilities={'wired_editor': True})
            self.assertFalse(view['actions']['edit_assignment']['enabled'])
            self.assertFalse(view['assignment_read_back'])

    def test_unknown_and_duplicate_mappings_never_named_as_verified(self):
        t = table((500, 500, 501, 65535))
        view = self.view(table=t)
        self.assertEqual(view['slots'][0]['status'], 'ambiguous_target')
        rows = [bytes.fromhex(r['raw']) for r in t['slots']]
        rows[2] = rows[2][:-2] + b'\x09\x80'
        t = snapshot(bytes.fromhex(t['count']), rows)
        view = self.view(table=t, mesh_ready=True, capabilities={'wired_editor': True})
        self.assertEqual(view['slots'][2]['status'], 'unsupported_assignment')
        self.assertFalse(view['slots'][2]['editable'])
        self.assertTrue(view['slots'][2]['preserved_by_setup'])

    def test_preview_offline_busy_and_pending_block_physical_actions(self):
        j = {'id': 'x', 'kind': 'keypad_edit', 'target': 32901, 'status': 'uncertain'}
        for state in ({'preview': True}, {'mesh_ready': False}, {'busy': True}, {'journals': [j]}):
            args = dict(mesh_ready=True, table=table(), capabilities={'wired_editor': True, 'wired_onboarding': True, 'inventory_complete': True})
            args.update(state)
            view = self.view(**args)
            self.assertFalse(view['actions']['read_assignments']['enabled'])
            self.assertFalse(view['actions']['setup_ha']['enabled'])
            self.assertFalse(view['actions']['edit_assignment']['enabled'])

    def test_busy_preserves_drafts_only_for_structurally_editable_keypad_slots(self):
        for model, flag in ((127, 'wired_editor'), (126, 'battery_editor')):
            with self.subTest(model=model):
                args = self.battery_setup_evidence()
                rows = [bytes.fromhex(row['raw']) for row in args['table']['slots']]
                rows[3] = bytes.fromhex('01300004ffff0000')
                args['table'] = snapshot(bytes([4, 48, 0, 3, 4]), rows)
                args['journals'][0]['detail'].update(product_id=model, desired=copy.deepcopy(args['table']))
                args['groups'].append({'group_id': 501, 'protected': True})
                args.update(mesh_ready=True, capabilities={flag: True, 'inventory_complete': True})
                idle = self.view(model, **args)
                busy = self.view(model, **args, busy=True)
                self.assertTrue(busy['editor_available'])
                self.assertEqual([row['draft_editable'] for row in busy['slots']], [True, True, False, True])
                self.assertEqual([row['ha_draft_available'] for row in busy['slots']], [True, True, False, True])
                self.assertTrue(busy['slots'][0]['ha_owned'])
                self.assertEqual(busy['slots'][2]['assignment_mode'], 'unknown')
                self.assertFalse(busy['actions']['edit_assignment']['enabled'])
                self.assertEqual(busy['actions']['edit_assignment']['reason'], 'operation_in_progress')
                self.assertTrue(all(not row['editable'] and not row['ha_available'] for row in busy['slots']))
                self.assertTrue(idle['actions']['edit_assignment']['enabled'])
                self.assertEqual([row['editable'] for row in idle['slots']], [True, True, False, True])
                self.assertEqual([row['draft_editable'] for row in idle['slots']], [True, True, False, True])

    def test_draft_presentation_still_requires_current_model_table_and_safe_state(self):
        pending = {'id': 'pending', 'kind': 'keypad_edit', 'target': 32901, 'status': 'uncertain'}
        for model, flag, wrong in ((127, 'wired_editor', 'battery_editor'), (126, 'battery_editor', 'wired_editor')):
            for blocked in ({'mesh_ready': False}, {'preview': True}, {'journals': [pending]},
                            {'table': None}, {'table': dict(table(), revision='invalid')},
                            {'capabilities': {wrong: True, 'inventory_complete': True}}):
                with self.subTest(model=model, blocked=blocked):
                    args = dict(mesh_ready=True, busy=True, table=table(),
                                capabilities={flag: True, 'inventory_complete': True})
                    args.update(blocked)
                    view = self.view(model, **args)
                    self.assertFalse(view['editor_available'])
                    self.assertTrue(all(not row['draft_editable'] and not row['ha_draft_available']
                                        for row in view['slots']))
        for model in (81, 91):
            view = self.view(model, mesh_ready=True, table=table(), capabilities={'battery_editor': True, 'wired_editor': True})
            self.assertFalse(view['editor_available'])

    def test_draft_ha_choice_still_requires_authoritative_inventory(self):
        for model, flag in ((127, 'wired_editor'), (126, 'battery_editor')):
            with self.subTest(model=model):
                args = self.battery_setup_evidence()
                args['journals'][0]['detail']['product_id'] = model
                view = self.view(model, **args, mesh_ready=True, busy=True, capabilities={flag: True})
                self.assertTrue(view['editor_available'])
                self.assertFalse(view['slots'][0]['draft_editable'], 'protected owned slots require inventory authority')
                self.assertTrue(view['slots'][1]['draft_editable'])
                self.assertTrue(all(not row['ha_draft_available'] for row in view['slots']))

    def test_setup_inventory_and_accessory_fresh_claim_admission(self):
        caps = {'accessory_onboarding': True, 'inventory_complete': True}
        claim = {'id': 'c', 'kind': 'claim', 'target': 32900, 'status': 'verified', 'detail': {'product_id': 91}}
        self.assertFalse(self.view(91, mesh_ready=True, capabilities=caps)['actions']['setup_ha']['enabled'])
        self.assertTrue(self.view(91, mesh_ready=True, capabilities=caps, journals=[claim])['actions']['setup_ha']['enabled'])
        self.assertFalse(self.view(91, mesh_ready=True, capabilities=caps, journals=[claim], retired_claims=['c'])['actions']['setup_ha']['enabled'])
        done = {'id': 'a', 'kind': 'accessory_setup', 'target': 32900, 'status': 'verified'}
        self.assertFalse(self.view(91, mesh_ready=True, capabilities=caps, journals=[claim, done])['actions']['setup_ha']['enabled'])
        self.assertFalse(self.view(mesh_ready=True, capabilities={'wired_onboarding': True})['actions']['setup_ha']['enabled'])

    def test_accessory_local_input_is_decoder_only(self):
        view = self.view(91, local_mode=True)
        self.assertTrue(view['actions']['local_input']['enabled'])
        self.assertEqual(view['input_status'], 'waiting_for_baseline')
        self.assertIn('does not read, clear or change', ' '.join(view['notes']))
        self.assertFalse(view['assignment_read_back'])
        self.assertFalse(self.view(81)['actions']['local_input']['enabled'])

    def accessory_setup_evidence(self):
        group = {'group_id': 24574, 'name': 'HA dimmer target: 32900',
                 'members': [], 'protected': True, 'exposed': False}
        claim = {'id': 'claim', 'kind': 'claim', 'target': 32900, 'status': 'verified',
                 'detail': {'product_id': 91}}
        setup = {'id': 'setup', 'kind': 'accessory_setup', 'target': 32900, 'status': 'verified',
                 'detail': {'claim': 'claim', 'reservation': copy.deepcopy(group)}}
        return dict(groups=[group], journals=[claim, setup])

    def test_accessory_setup_display_survives_session_without_claiming_fresh_input(self):
        args = self.accessory_setup_evidence()
        value = self.view(91, **args)['accessory_ha']
        self.assertEqual(value['state'], 'configured')
        self.assertEqual(value['target'], 24574)
        self.assertFalse(value['observed_in_session'])
        self.assertFalse(value['assignment_read_back'])
        args.update(native_observation=({'target': 24574}, 100), now=200)
        self.assertTrue(self.view(91, **args)['accessory_ha']['observed_in_session'])
        args['native_observation'] = ({'target': 500}, 100)
        self.assertFalse(self.view(91, **args)['accessory_ha']['observed_in_session'])

    def test_accessory_group_label_or_local_mode_is_not_setup_proof(self):
        args = self.accessory_setup_evidence()
        args['journals'] = []
        value = self.view(91, local_mode=True, **args)['accessory_ha']
        self.assertEqual(value['state'], 'not_configured')
        self.assertIsNone(value['target'])

    def test_accessory_display_requires_complete_retained_ownership(self):
        for defect in ('uncertain', 'retired', 'missing_claim', 'changed_group', 'duplicate_group',
                       'duplicate_setup', 'wrong_model', 'nonprivate', 'member_added'):
            with self.subTest(defect=defect):
                args = self.accessory_setup_evidence()
                if defect == 'uncertain': args['journals'][1]['status'] = 'uncertain'
                elif defect == 'retired': args['retired_claims'] = ['claim']
                elif defect == 'missing_claim': args['journals'].pop(0)
                elif defect == 'changed_group': args['groups'][0]['group_id'] = 24573
                elif defect == 'duplicate_group': args['groups'].append(copy.deepcopy(args['groups'][0]))
                elif defect == 'duplicate_setup': args['journals'].append(copy.deepcopy(args['journals'][1]))
                elif defect == 'wrong_model': args['journals'][0]['detail']['product_id'] = 81
                else:
                    key, value = ('protected', False) if defect == 'nonprivate' else ('members', [40001])
                    args['groups'][0][key] = value
                    args['journals'][1]['detail']['reservation'][key] = value
                value = self.view(91, **args)['accessory_ha']
                self.assertEqual(value['state'], 'needs_review')
                self.assertIsNone(value['target'])

    def anyplace_setup_evidence(self):
        args = self.accessory_setup_evidence()
        args['journals'][0]['detail']['product_id'] = 81
        args['journals'][1]['kind'] = 'anyplace_setup'
        args['journals'][1]['detail'].update(product_id=81, assignment_attempted=True,
                                             original_assignment='unknown')
        args['target_sources'] = {'24574': [32900]}
        return args

    def test_anyplace_setup_requires_own_gate_inventory_and_one_fresh_claim(self):
        args = self.anyplace_setup_evidence()
        claim = args['journals'][0]
        caps = {'anyplace_onboarding': True, 'inventory_complete': True}
        allowed = dict(mesh_ready=True, capabilities=caps, journals=[claim])
        self.assertTrue(self.view(81, **allowed)['actions']['setup_ha']['enabled'])
        self.assertFalse(self.view(91, **allowed)['actions']['setup_ha']['enabled'])
        for changes in ({'capabilities': {'accessory_onboarding': True, 'inventory_complete': True}},
                        {'capabilities': {'anyplace_onboarding': True}}, {'journals': []},
                        {'journals': [claim, dict(claim, id='another')]}, {'retired_claims': ['claim']},
                        {'mesh_ready': False}, {'preview': True}, {'busy': True}):
            with self.subTest(changes=changes):
                self.assertFalse(self.view(81, **dict(allowed, **changes))['actions']['setup_ha']['enabled'])
        for kind in ('anyplace_setup', 'accessory_setup', 'keypad_setup', 'keypad_edit', 'dimmer_edit'):
            for state in ('cancelled', 'verified', 'uncertain'):
                receipt = {'id': 'previous', 'kind': kind, 'target': 32900, 'status': state}
                self.assertFalse(self.view(81, **dict(allowed, journals=[claim, receipt]))['actions']['setup_ha']['enabled'])

    def cancelled_anyplace_evidence(self):
        args = self.anyplace_setup_evidence()
        claim, cancelled = args['journals']
        group = {'group_id': 24575, 'name': 'Abandoned private target', 'members': [],
                 'protected': True, 'exposed': False}
        cancelled.update(id='cancelled', status='cancelled')
        cancelled['detail'].update(claim_receipt=copy.deepcopy(claim), token=1,
                                  dispatch_not_started=True, assignment_attempted=False,
                                  reservation=copy.deepcopy(group))
        args.update(groups=[group], known_targets=[24575], target_sources={}, dimmer_token=1)
        return args

    def test_anyplace_proven_no_dispatch_allows_new_review_then_exact_new_setup_proof(self):
        args = self.cancelled_anyplace_evidence()
        args.update(mesh_ready=True, capabilities={'anyplace_onboarding': True, 'inventory_complete': True})
        value = self.view(81, **args)
        self.assertTrue(value['actions']['setup_ha']['enabled'])
        self.assertEqual(value['anyplace_ha']['state'], 'not_configured')
        current = self.anyplace_setup_evidence()
        args['journals'].append(current['journals'][1])
        args['groups'].extend(current['groups'])
        args['known_targets'].append(24574)
        args['dimmer_token'] = 2
        args['target_sources'] = current['target_sources']
        value = self.view(81, **args)
        self.assertFalse(value['actions']['setup_ha']['enabled'])
        self.assertEqual(value['anyplace_ha']['state'], 'configured')
        self.assertEqual(value['anyplace_ha']['transaction'], 'setup')
        self.assertEqual(value['anyplace_ha']['target'], 24574)

    def test_anyplace_false_cancellation_proof_stays_blocked_and_cannot_be_ignored(self):
        for defect in ('missing_proof', 'wrong_claim_snapshot', 'missing_burn', 'changed_reservation',
                       'observed', 'source_seen', 'attempted', 'uncertain', 'invalid_token',
                       'token_floor', 'duplicate_token', 'duplicate_target', 'foreign_claim'):
            with self.subTest(defect=defect):
                args = self.cancelled_anyplace_evidence()
                cancelled = args['journals'][1]
                detail = cancelled['detail']
                if defect == 'missing_proof': detail.pop('dispatch_not_started')
                elif defect == 'wrong_claim_snapshot': detail['claim_receipt']['detail']['product_id'] = 91
                elif defect == 'missing_burn': args['known_targets'] = []
                elif defect == 'changed_reservation': args['groups'][0]['name'] = 'Changed reservation'
                elif defect == 'observed': detail['observed'] = {'sequence': 1}
                elif defect == 'source_seen': args['target_sources'] = {'24575': [32900]}
                elif defect in ('attempted', 'uncertain'): cancelled['status'] = defect
                elif defect == 'invalid_token': detail['token'] = 0
                elif defect == 'token_floor': args['dimmer_token'] = 0
                elif defect == 'foreign_claim':
                    args['journals'].append({'id': 'foreign', 'kind': 'claim', 'target': 32900,
                                            'status': 'verified', 'detail': {'product_id': 91}})
                else:
                    duplicate = copy.deepcopy(cancelled)
                    duplicate['id'] = 'another-cancelled'
                    if defect == 'duplicate_token':
                        duplicate['detail']['reservation']['group_id'] = 24573
                        args['groups'].append(copy.deepcopy(duplicate['detail']['reservation']))
                        args['known_targets'].append(24573)
                    else:
                        duplicate['detail']['token'] = 2
                        args['dimmer_token'] = 2
                    args['journals'].append(duplicate)
                args.update(mesh_ready=True, capabilities={'anyplace_onboarding': True, 'inventory_complete': True})
                value = self.view(81, **args)
                self.assertFalse(value['actions']['setup_ha']['enabled'])
                self.assertEqual(value['anyplace_ha']['state'], 'needs_review')
                current = self.anyplace_setup_evidence()
                args['journals'].append(current['journals'][1])
                args['groups'].extend(current['groups'])
                value = self.view(81, **args)
                self.assertEqual(value['anyplace_ha']['state'], 'needs_review')
                self.assertIsNone(value['anyplace_ha']['transaction'])

    def test_anyplace_receipt_survives_reconnect_without_claiming_fresh_input_or_isolation(self):
        args = self.anyplace_setup_evidence()
        value = self.view(81, **args)['anyplace_ha']
        self.assertEqual(value['state'], 'configured')
        self.assertEqual((value['target'], value['transaction']), (24574, 'setup'))
        for field in ('observed_in_session', 'assignment_read_back', 'ha_receipt_verified', 'physical_isolation_verified'):
            self.assertIs(value[field], False)
        self.assertTrue(value['experimental'])
        self.assertEqual(value['original_assignment'], 'unknown')
        args.update(native_observation=({'target': 24574}, 100), now=200)
        self.assertTrue(self.view(81, **args)['anyplace_ha']['observed_in_session'])
        for target, when in ((500, 100), (24574, -20000), (24574, 201)):
            args['native_observation'] = ({'target': target}, when)
            self.assertFalse(self.view(81, **args)['anyplace_ha']['observed_in_session'])
        self.assertIsNone(self.view(91, **args)['anyplace_ha'])
        self.assertIsNone(self.view(81, **args)['accessory_ha'])

    def test_anyplace_display_requires_exact_receipt_claim_and_exclusive_private_reservation(self):
        defects = ('uncertain', 'cancelled', 'retired', 'missing_claim', 'wrong_claim_model', 'wrong_claim_id',
                   'duplicate_claim', 'changed_group', 'duplicate_group', 'duplicate_setup', 'wrong_model',
                   'not_attempted', 'original_not_unknown', 'nonprivate', 'member_added', 'exposed', 'foreign_source')
        for defect in defects:
            with self.subTest(defect=defect):
                args = self.anyplace_setup_evidence()
                detail = args['journals'][1]['detail']
                if defect in ('uncertain', 'cancelled'): args['journals'][1]['status'] = defect
                elif defect == 'retired': args['retired_claims'] = ['claim']
                elif defect == 'missing_claim': args['journals'].pop(0)
                elif defect == 'wrong_claim_model': args['journals'][0]['detail']['product_id'] = 91
                elif defect == 'wrong_claim_id': detail['claim'] = 'other'
                elif defect == 'duplicate_claim': args['journals'].append(dict(args['journals'][0], id='other'))
                elif defect == 'changed_group': args['groups'][0]['name'] = 'Changed name'
                elif defect == 'duplicate_group': args['groups'].append(copy.deepcopy(args['groups'][0]))
                elif defect == 'duplicate_setup': args['journals'].append(copy.deepcopy(args['journals'][1]))
                elif defect == 'wrong_model': detail['product_id'] = 91
                elif defect == 'not_attempted': detail['assignment_attempted'] = False
                elif defect == 'original_not_unknown': detail['original_assignment'] = 500
                elif defect == 'foreign_source': args['target_sources']['24574'].append(32901)
                else:
                    key, value = {'nonprivate': ('protected', False), 'member_added': ('members', [40001]),
                                  'exposed': ('exposed', True)}[defect]
                    args['groups'][0][key] = value
                    detail['reservation'][key] = value
                status = self.view(81, **args)['anyplace_ha']
                self.assertEqual(status['state'], 'needs_review')
                self.assertIsNone(status['transaction'])
        args = self.anyplace_setup_evidence()
        args['journals'] = []
        self.assertEqual(self.view(81, local_mode=True, **args)['anyplace_ha']['state'], 'not_configured')

    def test_observed_target_never_assignment_readback(self):
        for model in (81, 91):
            view = self.view(model, native_observation=({'target': 500}, 100), now=200, table=table(), capabilities={'dimmer_editor': True})
            self.assertEqual(view['observed_target']['target'], 500)
            self.assertFalse(view['assignment_read_back'])
            self.assertFalse(view['observed_target']['assignment_read_back'])
            self.assertFalse(view['actions']['assign_native_target']['enabled'])
            for when in (-20000, 201):
                self.assertIsNone(self.view(model, native_observation=({'target': 500}, when), now=200)['observed_target'])

    def test_native_review_requires_each_admission_precondition(self):
        groups = [{'group_id': 501, 'protected': False}]
        args = dict(mesh_ready=True, groups=groups,
                    native_observation=({'target': 500, 'target_generation': 3}, 100), now=200)
        for model in (81, 91):
            flag, other = ('anyplace_editor', 'dimmer_editor') if model == 81 else ('dimmer_editor', 'anyplace_editor')
            args['capabilities'] = {flag: True}
            self.assertFalse(self.view(model, **dict(args, capabilities={other: True}))['actions']['assign_native_target']['enabled'])
            view = self.view(model, **args)
            self.assertTrue(view['actions']['assign_native_target']['enabled'])
            self.assertEqual(view['observed_target']['baseline'],
                             {'target': 500, 'observed_ms': 100, 'target_generation': 3})
            self.assertEqual(view['observed_target']['valid_for_ms'], 14900)
            self.assertFalse(view['assignment_read_back'])
            for state in ({'mesh_ready': False}, {'busy': True}, {'preview': True},
                          {'groups': []}, {'groups': [{'group_id': 500, 'protected': False}]},
                          {'groups': groups + [{'group_id': 500, 'protected': True}]},
                          {'now': 15101}, {'now': 99}, {'native_observation': None},
                          {'journals': [{'id': 'x', 'kind': 'dimmer_edit', 'target': 32900, 'status': 'uncertain'}]}):
                with self.subTest(model=model, state=state):
                    self.assertFalse(self.view(model, **dict(args, **state))['actions']['assign_native_target']['enabled'])

    def test_latest_event_is_sanitized_and_never_infers_button(self):
        event = {'source': 32900, 'event_type': 'on', 'target': 500, 'brightness': 128,
                 'payload': 'never expose raw captures', 'button': 2, 'activation': True}
        view = self.view(table=table(), last_event=(event, 100), now=200)
        self.assertEqual(view['last_event']['age_ms'], 100)
        self.assertEqual(view['last_event']['observed_ms'], 100)
        self.assertNotIn('payload', view['last_event'])
        self.assertNotIn('button', view['last_event'])
        event['button_mapping'] = 'verified_table'
        view = self.view(table=table(), last_event=(event, 100), now=200)
        self.assertEqual(view['last_event']['button'], 2)
        self.assertTrue(view['last_event']['activation'])
        self.assertFalse(view['last_event']['assignment_read_back'])
        self.assertNotIn('button', self.view(last_event=(event, 100), now=200)['last_event'])
        self.assertNotIn('button', self.view(table=table((500, 500, 501, 65535)), last_event=(event, 100), now=200)['last_event'])
        self.assertIsNone(self.view(last_event=(event, 100), now=99)['last_event'])
        local = self.view(91, last_event=({'event_type': 'brightness', 'brightness': 19,
                         'input_mode': 'local_level_reports'}, 100), now=200)['last_event']
        self.assertEqual(local['input_mode'], 'local_level_reports')
        self.assertNotIn('target', local)

    def test_recovery_is_explicit_and_distinguishes_writes(self):
        for kind, mode in (('keypad_setup', 'explicit_guarded_continuation'), ('keypad_edit', 'readback_only'), ('accessory_setup', 'observation_only'), ('anyplace_setup', 'observation_only'), ('dimmer_edit', 'observation_only')):
            j = {'id': 'x', 'kind': kind, 'target': 32900, 'status': 'uncertain'}
            recovery = self.view(journals=[j])['recovery'][0]
            self.assertEqual(recovery['mode'], mode)
            self.assertFalse(recovery['automatic_resume'])
            if kind == 'keypad_setup':
                self.assertIn('can write', recovery['warning'])

    def test_ha_state_is_distinct_from_mesh_or_assignment(self):
        self.assertEqual(self.view()['ha_status'], 'disabled')
        self.assertEqual(self.view(mqtt_enabled=True)['ha_status'], 'disconnected')
        self.assertEqual(self.view(mqtt_enabled=True, mqtt_ready=True)['ha_status'], 'publishing_enabled')

    def test_inputs_not_mutated(self):
        t = table()
        original = copy.deepcopy(t)
        view = self.view(table=t)
        view['slots'][0]['target'] = 1234
        self.assertEqual(t, original)

    def test_adapter_uses_current_router_only_and_reads_no_transport(self):
        devices = [{'device_id': 32900, 'product_id': 127}, {'device_id': 32901, 'product_id': 93}]
        store = SimpleNamespace(journals=lambda: [], inventory=lambda: {'devices': devices}, get=lambda k, default: default)
        router = SimpleNamespace(bindings={}, local_modes=set(), baselines={})
        runtime = SimpleNamespace(store=store, core=SimpleNamespace(controllers=router), capabilities={}, mesh_ready=True,
            controller_actions_busy=False, native_observations={}, clock=lambda: 0, mqtt_enabled=False, mqtt_ready=False)
        result = controller_workflows(runtime, read_only=True)
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]['actions']['read_assignments']['enabled'])
        self.assertFalse(result[0]['assignment_read_back'])


if __name__ == '__main__':
    unittest.main()
