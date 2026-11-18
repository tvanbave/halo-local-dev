"""Activation has constant radio work; verification is an explicit operation."""
import copy
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from avionmesh_pi.protocol import Packet
from avionmesh_pi.scene_engine import SceneEngine, KEY, PROOFS
import test_scene_engine as fixtures


class SceneActivationFastTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SceneEngineTests.setUp
    tearDown = fixtures.SceneEngineTests.tearDown
    writes = fixtures.SceneEngineTests.writes

    def reboot(self):
        self.engine = SceneEngine(self.r)
        self.engine.gap = self.engine.pair_gap = 0
        self.r.generation += 1

    async def test_single_triple_and_32_member_recall_each_send_one_frame_after_restart(self):
        inventory = {'devices': [dict(device_id=40001+i, product_id=134,
            name='Synthetic '+str(i), exposed=False) for i in range(40)], 'groups': []}
        self.store.import_inventory(inventory)
        self.r.core.devices = {d['device_id']: SimpleNamespace(device_id=d['device_id'],
            product_id=d['product_id']) for d in inventory['devices']}
        for size in (1, 3, 32):
            with self.subTest(members=size):
                members = [dict(target=40001+i, brightness=128, kelvin=4000) for i in range(size)]
                row = dict(id='a'*32, reference=1, name='Synthetic', members=members,
                    exposed=False, owner=self.engine._owner())
                self.store.set(KEY, [row])
                self.r.tables.clear()
                for member in members:
                    self.r.tables[(member['target'], 1)] = self.engine._values(member)
                await self.engine.verify(row['id'])
                self.reboot()
                self.r.frames.clear()
                with patch.object(self.engine, '_survey', side_effect=AssertionError('Recall must not verify')):
                    result = await self.engine.activate(row['id'])
                self.assertEqual(len(self.r.frames), 1)
                self.assertEqual(self.r.frames[0], bytes.fromhex('00007300220001000101000000'))
                self.assertEqual(result['verification_reads'], 0)
                self.assertEqual(result['recall_commands'], 1)

    async def test_real_store_reopen_retains_verified_preset(self):
        from support import synthetic_store
        row = (await self.engine.create('Persistent', self.members))['scene']
        self.store.close()
        self.store = synthetic_store(self.tmp.name, bytes(range(16)))
        self.r.store = self.store
        self.reboot()
        self.r.frames.clear()
        await self.engine.activate(row['id'])
        self.assertEqual(len(self.r.frames), 1)

    async def test_labels_groups_rooms_and_exposure_do_not_trigger_a_survey(self):
        row = (await self.engine.create('Persistent', self.members))['scene']
        inventory = copy.deepcopy(self.inventory)
        inventory['devices'][0].update(name='Renamed', exposed=True)
        inventory['groups'] = [dict(group_id=300, name='Room', members=[40001], exposed=False, protected=False)]
        self.store.set('inventory', inventory)
        self.store.set('rooms', {'40001':'New room'})
        self.engine.set_exposed(row['id'], True)
        self.reboot()
        self.r.frames.clear()
        await self.engine.activate(row['id'])
        self.assertEqual(len(self.r.frames), 1)

    async def test_added_nonmember_blocks_without_radio_until_explicit_verification(self):
        row = (await self.engine.create('Scope', [self.members[0]]))['scene']
        inventory = copy.deepcopy(self.inventory)
        inventory['devices'].append(dict(device_id=40003, product_id=134, name='New', exposed=False))
        self.store.set('inventory', inventory)
        self.r.core.devices[40003] = SimpleNamespace(device_id=40003, product_id=134)
        self.r.frames.clear()
        with self.assertRaisesRegex(RuntimeError, 'Verify saved settings'):
            await self.engine.activate(row['id'])
        self.assertEqual(self.r.frames, [])
        await self.engine.verify(row['id'])
        self.r.frames.clear()
        await self.engine.activate(row['id'])
        self.assertEqual(len(self.r.frames), 1)

    async def test_missing_or_corrupt_upgrade_proof_never_auto_reads_or_recalls(self):
        row = (await self.engine.create('Upgrade', self.members))['scene']
        for proof in ({}, [], {row['id']:'bad'}):
            self.store.set(PROOFS, proof)
            self.reboot(); self.r.frames.clear()
            with self.assertRaises(RuntimeError): await self.engine.activate(row['id'])
            self.assertEqual(self.r.frames, [])

    async def test_observed_external_mutation_remains_revoked_after_restart(self):
        row = (await self.engine.create('External', self.members))['scene']
        self.engine.observe(Packet(100,40001,32768,115,bytes.fromhex('022200009901020bb800')))
        self.reboot(); self.r.frames.clear()
        with self.assertRaises(RuntimeError): await self.engine.activate(row['id'])
        self.assertEqual(self.r.frames, [])

    async def test_unmatched_reply_revokes_all_and_known_write_preserves_other_scene(self):
        first = (await self.engine.create('First', [self.members[0]]))['scene']
        second = (await self.engine.create('Second', [self.members[1]]))['scene']
        from avionmesh_pi.scene_protocol import candidate_colour_insert
        command = candidate_colour_insert(40002, second['reference'], 3500, 231)
        self.engine._track_mutation(command)
        self.engine.observe(Packet(10000,40002,32768,115,bytes.fromhex('0222e705')))
        self.assertTrue(self.engine._leased(first))
        self.assertFalse(self.engine._leased(second))
        self.engine.observe(Packet(10001,40001,32768,115,bytes.fromhex('0222e805')))
        self.assertFalse(self.engine._leased(first))
        self.reboot()
        self.assertFalse(self.engine._leased(first))

    async def test_expired_or_old_session_mutation_correlation_cannot_preserve_proof(self):
        from avionmesh_pi.scene_protocol import candidate_delete
        row = (await self.engine.create('First', self.members))['scene']
        for mode in ('expired', 'session', 'stale_sequence'):
            await self.engine.verify(row['id'])
            self.engine._track_mutation(candidate_delete(40001, 2, 231))
            sent = self.engine.sent_mutations[(40001, 5, 231)]
            if mode == 'expired': sent['when'] -= 31
            if mode == 'session': self.r.generation += 1
            sequence = sent['floor'] if mode == 'stale_sequence' else 10000
            self.engine.observe(Packet(sequence,40001,32768,115,bytes.fromhex('0522e701')))
            self.assertFalse(self.engine._leased(row))

    async def test_failed_explicit_verification_does_not_restore_saved_proof(self):
        row = (await self.engine.create('Read failure', self.members))['scene']
        self.r.drop_query = True
        with self.assertRaises(asyncio.TimeoutError): await self.engine.verify(row['id'])
        self.reboot(); self.r.frames.clear()
        with self.assertRaises(RuntimeError): await self.engine.activate(row['id'])
        self.assertEqual(self.r.frames, [])

    async def test_activation_timing_separates_checks_frame_preparation_and_submission(self):
        row = (await self.engine.create('Timing', self.members))['scene']
        self.r.frames.clear()
        ticks = iter([0, .010, .040, .050, .080, .081])
        with patch('avionmesh_pi.scene_engine.time', SimpleNamespace(monotonic=lambda: next(ticks))):
            result = await self.engine.activate(row['id'])
        self.assertEqual(result['timing'], {
            'checks_ms': 10, 'prepare_frame_ms': 30, 'dispatch_checks_ms': 10,
            'submission_ms': 30, 'handler_to_submission_ms': 80,
            'after_submission_ms': 1, 'physical_response_measured': False})
        self.assertEqual(result['dispatch_ms'], 81)
        self.assertEqual(len(self.r.frames), 1)
        self.assertEqual(self.r.frames[0], bytes.fromhex('00007300220001000101000000'))
        self.assertEqual(result['verification_reads'], 0)
