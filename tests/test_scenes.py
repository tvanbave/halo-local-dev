import copy
from dataclasses import FrozenInstanceError
import tempfile
import unittest

from avionmesh_pi.scenes import SceneJournal, SceneMember, ScenePlan, ScenePreset, plan_create, plan_remove
from avionmesh_pi.storage import Store


class ScenePlanTests(unittest.TestCase):
    def test_explicit_presets_and_members_are_copied_into_frozen_intent(self):
        presets = [ScenePreset(40001, 77, 3200), ScenePreset(40002, 180, 4300)]
        plan = plan_create(3, 'Reading', presets)
        presets.clear()
        self.assertEqual([(s.target, s.property, s.value) for s in plan.steps], [
            (40001, 'colour', 3200), (40001, 'brightness', 77),
            (40002, 'colour', 4300), (40002, 'brightness', 180)])
        with self.assertRaises(FrozenInstanceError):
            plan.name = 'Changed'
        exported = plan.as_dict()
        exported['members'][0]['brightness'] = 1
        self.assertEqual(plan.members[0].brightness, 77)
        removal = plan_remove(3, 'Reading', [SceneMember(40002)])
        self.assertEqual([(s.target, s.property, s.value) for s in removal.steps],
                         [(40002, 'remove_reference', None)])

    def test_candidate_commands_follow_explicit_plan_without_allocating_tokens(self):
        plan = plan_create(3, 'Reading', [ScenePreset(40001, 77, 3200)])
        self.assertEqual(plan.candidate_command(0, 0x42).hex(), '419c73022200004203020c8000')
        self.assertEqual(plan.candidate_command(1, 0x43).hex(), '419c730222000043234d000000')
        removal = plan_remove(3, 'Reading', [SceneMember(40001)])
        self.assertEqual(removal.candidate_command(0, 0x44).hex(), '419c7305220000440300000000')
        for index, token in [(True, 5), (-1, 5), (2, 5), (0, True), (0, 0)]:
            with self.subTest(index=index, token=token), self.assertRaises(ValueError):
                plan.candidate_command(index, token)

    def test_invalid_and_unqualified_inputs_fail_closed(self):
        for args in [(True, 77, 3200), (40001, True, 3200), (40001, 77, True),
                     (40001, 256, 3200), (40001, 77, 5001), (40001, 77, 3200, 93)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                ScenePreset(*args)
        p = ScenePreset(40001, 77, 3200)
        for reference, name, members in [(True, 'Reading', [p]), (21, 'Reading', [p]),
                (1, '', [p]), (1, '  ', [p]), (1, 'Bad\nname', [p]), (1, 'x' * 129, [p]),
                (1, 'Reading', []), (1, 'Reading', [p, p]), (1, 'Reading', [SceneMember(40001)])]:
            with self.subTest(reference=reference, name=name), self.assertRaises(ValueError):
                plan_create(reference, name, members)
        with self.assertRaises(ValueError):
            ScenePlan('create', 1, 'Reading', [p])


class SceneJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name, b'\x91' * 16)
        self.store.set('unrelated', {'preserved': True})
        self.plan = plan_create(3, 'Reading', [ScenePreset(40001, 77, 3200)])
        self.journal = SceneJournal(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_restart_preserves_plan_and_quarantines_attempt(self):
        identifier = self.journal.prepare(self.plan)
        fingerprint = self.journal.snapshot(identifier)['fingerprint']
        self.journal.mark_attempted(identifier, 0)
        self.store.close()
        self.store = Store(self.directory.name, b'\x91' * 16)
        self.journal = SceneJournal(self.store)
        snapshot = self.journal.snapshot(identifier)
        self.assertEqual(snapshot['fingerprint'], fingerprint)
        self.assertEqual([s['state'] for s in snapshot['steps']], ['uncertain', 'not_started'])
        self.assertEqual(snapshot['status'], 'verification_unavailable')
        self.assertTrue(snapshot['requires_review'])
        self.assertFalse(snapshot['physical_verified'])
        self.assertFalse(snapshot['programming_supported'])
        for index in (0, 1):
            with self.assertRaises(ValueError):
                self.journal.mark_attempted(identifier, index)
        with self.assertRaises(ValueError):
            self.journal.prepare(self.plan)
        self.assertEqual(self.store.get('unrelated'), {'preserved': True})
        self.assertEqual(self.store.get('sequence'), 0)
        self.assertEqual(self.store.journals(), [])

    def test_raw_reply_is_not_success_and_cannot_unlock_next_write(self):
        identifier = self.journal.prepare(self.plan)
        with self.assertRaises(ValueError):
            self.journal.record_reply(identifier, 0, bytes.fromhex('02229e01'))
        self.journal.mark_attempted(identifier, 0)
        self.journal.record_reply(identifier, 0, bytes.fromhex('02229e01'))
        snapshot = self.journal.snapshot(identifier)
        self.assertEqual(snapshot['status'], 'verification_unavailable')
        self.assertEqual(snapshot['steps'][0]['reply'], '02229e01')
        self.assertFalse(snapshot['retry_allowed'])
        with self.assertRaises(ValueError):
            self.journal.mark_attempted(identifier, 1)
        with self.assertRaises(ValueError):
            self.journal.cancel(identifier)

    def test_only_untouched_plan_can_cancel_and_snapshot_is_detached(self):
        identifier = self.journal.prepare(self.plan)
        snapshot = self.journal.snapshot(identifier)
        snapshot['steps'][0]['state'] = 'verified'
        snapshot['plan']['members'][0]['brightness'] = 0
        self.assertEqual(self.journal.snapshot(identifier)['steps'][0]['state'], 'not_started')
        self.journal.cancel(identifier)
        self.assertEqual(self.journal.snapshot(identifier)['status'], 'cancelled')
        with self.assertRaises(ValueError):
            self.journal.mark_attempted(identifier, 0)
        self.journal.prepare(self.plan)

    def test_corrupted_or_forged_ledger_is_rejected(self):
        self.journal.prepare(self.plan)
        valid = self.store.get('scene_research_operations_v1')
        mutations = [lambda d: d.update(format=True),
            lambda d: d['receipts'][0]['plan']['members'][0].update(brightness=10),
            lambda d: d['receipts'][0]['states'].__setitem__(0, 'verified'),
            lambda d: d['receipts'][0]['states'].__setitem__(1, 'attempted'),
            lambda d: d['receipts'][0].update(replies=['ff', None]),
            lambda d: d['receipts'][0].update(cancelled='false'),
            lambda d: d['receipts'].append(dict(copy.deepcopy(d['receipts'][0]), id='e' * 32))]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bad = copy.deepcopy(valid)
                mutate(bad)
                self.store.set('scene_research_operations_v1', bad)
                with self.assertRaises(ValueError):
                    SceneJournal(self.store)
        self.store.set('scene_research_operations_v1', valid)

    def test_remove_plan_records_attempt_without_claiming_absence(self):
        plan = plan_remove(5, 'Reading', [SceneMember(40002)])
        identifier = self.journal.prepare(plan)
        self.journal.mark_attempted(identifier, 0)
        self.journal.record_reply(identifier, 0, bytes.fromhex('0522b201'))
        snapshot = self.journal.snapshot(identifier)
        self.assertEqual(snapshot['status'], 'verification_unavailable')
        self.assertFalse(snapshot['physical_verified'])
        self.assertEqual(self.store.inventory(), {'devices': [], 'groups': []})
