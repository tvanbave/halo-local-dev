"""Fresh discovery, serial RL4 claims and wired-controller reset recovery."""
import asyncio
import copy
import unittest
from unittest.mock import patch

import test_association as base
from avionmesh_pi.association import envelope, open_envelope, uuid_hash
from avionmesh_pi.commissioning import Commissioning
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.management import recovery_advice
from support import synthetic_store as Store

KEY = base.KEY
IDENTITY = '000102030405140000040a0b0c0d0e0f'
SECOND = '010102030405140000040a0b0c0d0e0f'
THIRD = '020102030405140000040a0b0c0d0e0f'
WIRED = '0301020304051a0000040a0b0c0d0e0f'
ACCESSORY = '040102030405140000070a0b0c0d0e0f'


class MultiTransport:
    ready = True
    dropped = 0
    def __init__(self, runtime):
        self.runtime = runtime
        self.ads = [IDENTITY, SECOND, THIRD, WIRED, ACCESSORY]
        self.scans = 0
        self.scan_rows = None
        self.peers = {uuid_hash(bytes.fromhex(identity)): base.Peer() for identity in self.ads}
        self.starts = []
        self.frames = []
        self.fail_hash = None
        self.fail_opcode = 10
        self.on_send = None
        self.disconnects = 0
    async def disconnect(self):
        self.disconnects += 1
        self.ready = False
    async def send(self, frame):
        self.frames.append(frame)
        if self.on_send:
            self.on_send(frame)
        try:
            p = open_envelope(frame)
        except ValueError:
            command = decrypt_mcp(frame, KEY)
            reply = encrypt_mcp(b'\x00\x80\x73\x06\xff' + bytes([command.payload[4]]) + b'\x01\0\0\0',
                                KEY, len(self.frames), command.mcp_address)
            self.runtime.observe(reply, self.runtime.clock())
            return
        if p[0] == 0:
            self.scans += 1
            rows = self.scan_rows(self.scans) if self.scan_rows else self.ads
            for identity in rows:
                self.runtime.observe(envelope(b'\x01' + bytes.fromhex(identity)), self.runtime.clock())
            return
        fingerprint = int.from_bytes(p[1:5], 'little')
        if p[0] == 2:
            self.starts.append(fingerprint)
        if self.fail_hash == fingerprint and p[0] == self.fail_opcode:
            raise RuntimeError('synthetic GATT rejection')
        reply = self.peers[fingerprint].respond(frame)
        self.runtime.observe(reply, self.runtime.clock())


class ClaimDevelopmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base.ClaimTests.asyncSetUp(self)
        self.transport = MultiTransport(self.runtime)
        self.runtime.transport = self.transport
        self.runtime.capabilities.update(bulk_claim=True, claim_reset_recovery=True)
        original = self.claims.discover
        async def fast(seconds=5):
            return await original(seconds=0)
        self.fast = fast
        self.claims.discover = fast
    asyncTearDown = base.ClaimTests.asyncTearDown

    def ready_again(self):
        self.runtime.mesh_ready = self.transport.ready = True

    def rows(self, identities=(IDENTITY, SECOND)):
        return [{'uuid':identity,'name':'RL4 '+str(index),'product_id':93} for index,identity in enumerate(identities)]

    async def test_cumulative_candidates_preserve_missing_rows_but_mark_them_stale(self):
        self.transport.scan_rows = lambda scan: [IDENTITY] if scan == 1 else [SECOND]
        await self.claims.discover()
        await self.claims.discover()
        candidates = {row['uuid']:row for row in self.claims.candidate_snapshot()}
        self.assertEqual(set(candidates), {IDENTITY, SECOND})
        self.assertFalse(candidates[IDENTITY]['fresh'])
        self.assertTrue(candidates[SECOND]['fresh'])
        with self.assertRaises(ValueError):
            await self.claims.claim(IDENTITY, 'Missing', 93, inventory_complete=True)
        self.assertEqual(self.transport.starts, [])

    async def test_expired_selection_is_refreshed_before_claim(self):
        now = [10]
        self.runtime.clock = lambda: now[0]
        await self.claims.discover()
        now[0] += 60000
        result = await self.claims.claim(IDENTITY,'Named after delay',93,inventory_complete=True,refresh_identity=True)
        self.assertTrue(result['associated'])
        self.assertEqual(self.transport.scans, 2)

    async def test_discovery_session_change_invalidates_partial_results(self):
        self.transport.on_send = lambda frame: self.runtime.session()
        with self.assertRaises(RuntimeError):
            await self.claims.discover()
        self.assertEqual(self.claims.discovered, {})
        self.assertEqual(self.runtime.raw_observers, [])

    async def test_truncated_scan_cannot_admit_claim(self):
        self.transport.ads = [i.to_bytes(16,'big').hex() for i in range(1,131)]
        found = await self.claims.discover()
        self.assertEqual(len(found),128)
        self.assertTrue(self.claims.scan_truncated)
        with self.assertRaises(ValueError):
            await self.claims.claim(found[0]['uuid'],'Truncated',93,inventory_complete=True)
        self.assertEqual(self.transport.starts, [])

    async def test_discovery_bounds_and_mutable_ttl(self):
        for seconds in (True, -1, 31, float('nan')):
            with self.assertRaises(ValueError):
                await Commissioning.discover(self.claims, seconds=seconds)
        packet = envelope(b'\x01' + bytes.fromhex(IDENTITY))
        for ttl in (0, 1, 54, 55, 255):
            self.assertEqual(open_envelope(packet[:-1] + bytes([ttl])), b'\x01' + bytes.fromhex(IDENTITY))
        corrupted = bytearray(packet); corrupted[-2] ^= 1
        with self.assertRaises(ValueError):
            open_envelope(bytes(corrupted))
        self.assertEqual(self.transport.frames, [])

    async def test_claim_refresh_retries_discovery_only_until_exact_target(self):
        self.transport.scan_rows = lambda scan: [SECOND] if scan < 3 else [IDENTITY]
        result = await self.claims.claim(IDENTITY,'Late reply',93,inventory_complete=True,refresh_identity=True)
        self.assertTrue(result['associated'])
        self.assertEqual(self.transport.scans,3)
        self.assertEqual(len(self.transport.starts),1)

    async def test_missing_target_after_three_scans_never_starts_association(self):
        from avionmesh_pi.commissioning import ClaimValidationError
        self.transport.scan_rows = lambda scan: [SECOND]
        with self.assertRaisesRegex(ClaimValidationError,'No pairing was started'):
            await self.claims.claim(IDENTITY,'Absent',93,inventory_complete=True,refresh_identity=True)
        self.assertEqual(self.transport.scans,3)
        self.assertFalse(self.transport.starts)
        self.assertFalse(self.store.journals())

    async def test_batch_claims_selected_devices_only_with_per_device_scans(self):
        result = await self.claims.claim_many(self.rows(),inventory_complete=True)
        self.assertTrue(result['complete'])
        self.assertEqual(self.transport.scans,2)
        self.assertEqual(self.transport.starts,[uuid_hash(bytes.fromhex(IDENTITY)),uuid_hash(bytes.fromhex(SECOND))])
        self.assertEqual(len(self.store.inventory()['devices']),2)
        self.assertTrue(all(j['status']=='verified' for j in self.store.journals()))
        self.assertFalse(result['group_setup_complete'])
        self.assertIsNone(self.claims.batch_owner)

    async def test_batch_failure_keeps_first_success_and_does_not_start_third(self):
        self.transport.fail_hash = uuid_hash(bytes.fromhex(SECOND))
        result = await self.claims.claim_many(self.rows((IDENTITY,SECOND,THIRD)),inventory_complete=True)
        self.assertFalse(result['complete'])
        self.assertEqual(result['remaining'],[THIRD])
        self.assertTrue(result['results'][0]['associated'])
        self.assertFalse(result['results'][1]['associated'])
        self.assertEqual([j['status'] for j in self.store.journals()],['verified','uncertain'])
        self.assertEqual(len(self.transport.starts),2)
        self.assertEqual(len(self.store.inventory()['devices']),1)

    async def test_batch_prevalidates_identity_metadata_and_capability(self):
        cases=[[],self.rows((IDENTITY,IDENTITY)),[dict(self.rows()[0],product_id=127)],
               [dict(self.rows()[0],uuid='00'*16)], [dict(self.rows()[0],name='')]]
        for rows in cases:
            with self.assertRaises(ValueError):
                await self.claims.claim_many(rows,inventory_complete=True)
        self.runtime.capabilities['bulk_claim']=False
        with self.assertRaises(ValueError):
            await self.claims.claim_many(self.rows(),inventory_complete=True)
        self.assertEqual(self.transport.frames,[])

    async def test_pending_claim_blocks_fresh_scan_before_any_radio(self):
        self.store.begin('claim',40001,{'uuid':THIRD})
        with self.assertRaises(ValueError):
            await self.claims.claim(IDENTITY,'No I/O',93,inventory_complete=True,refresh_identity=True)
        self.assertEqual(self.transport.frames,[])

    async def test_gatt_failure_records_phase_and_acceptance_separately(self):
        await self.claims.discover()
        self.transport.fail_hash=uuid_hash(bytes.fromhex(IDENTITY))
        with self.assertRaises(RuntimeError):
            await self.claims.claim(IDENTITY,'Wired',127,inventory_complete=True)
        journal=self.store.journals()[0]
        self.assertEqual(journal['status'],'uncertain')
        self.assertEqual(journal['detail']['association_phase'],'device_id')
        self.assertEqual(journal['detail']['submissions_attempted'],journal['detail']['submissions_accepted']+1)
        self.assertEqual(len(self.transport.starts),1)

    async def uncertain(self, product=127):
        await self.claims.discover()
        identity = WIRED if product == 127 else ACCESSORY if product == 91 else IDENTITY
        self.transport.fail_hash=uuid_hash(bytes.fromhex(identity))
        with self.assertRaises(RuntimeError):
            await self.claims.claim(identity,'Wired',product,inventory_complete=True)
        self.transport.fail_hash=None
        self.ready_again()
        return self.store.journals(pending=True)[0]

    async def test_wired_owner_reset_retires_address_then_fresh_claim_uses_new_one(self):
        original=await self.uncertain()
        self.assertIn('verify_claim_owner_reset',recovery_advice(original)['actions'])
        count=len(self.transport.starts)
        result=await self.claims.reconcile_owner_reset(original['id'],True)
        self.assertTrue(result['retired'])
        self.assertFalse(result['automatic_reclaim'])
        self.assertEqual(len(self.transport.starts),count)
        self.assertIn(original['target'],self.store.get('retired_addresses'))
        proof=next(j for j in self.store.journals() if j['kind']=='claim_reset')
        self.assertEqual(proof['detail']['original_receipt'],original)
        self.transport.peers[uuid_hash(bytes.fromhex(WIRED))]=base.Peer()
        claimed=await self.claims.claim(WIRED,'Reclaimed wired',127,inventory_complete=True,refresh_identity=True)
        self.assertGreater(claimed['device_id'],original['target'])
        self.assertEqual(len(self.store.inventory()['devices']),1)

    async def test_reset_recovery_requires_owner_declaration_and_capability(self):
        original=await self.uncertain()
        count=len(self.transport.frames)
        for value in (False,1,None):
            with self.assertRaises(ValueError):
                await self.claims.reconcile_owner_reset(original['id'],value)
        self.runtime.capabilities['claim_reset_recovery']=False
        with self.assertRaises(ValueError):
            await self.claims.reconcile_owner_reset(original['id'],True)
        self.assertEqual(len(self.transport.frames),count)

    async def test_missing_reset_identity_preserves_pending_claim(self):
        original=await self.uncertain()
        self.transport.ads=[SECOND]
        result=await self.claims.reconcile_owner_reset(original['id'],True)
        self.assertFalse(result['retired'])
        self.assertEqual(self.store.journals(pending=True)[0],original)

    async def test_light_claim_reset_remains_unsupported(self):
        original=await self.uncertain(93)
        before=len(self.transport.frames)
        with self.assertRaises(ValueError):
            await self.claims.reconcile_owner_reset(original['id'],True)
        self.assertEqual(len(self.transport.frames),before)

    async def test_reset_proof_never_survives_session_change(self):
        original=await self.uncertain()
        self.transport.on_send=lambda frame:self.runtime.session()
        with self.assertRaises(RuntimeError):
            await self.claims.reconcile_owner_reset(original['id'],True)
        self.assertEqual(self.store.journals(pending=True)[0],original)

    async def test_changed_receipt_during_scan_cannot_be_retired(self):
        original=await self.uncertain()
        def mutate(frame):
            self.store.db.execute("UPDATE journals SET status='attempted' WHERE id=?",(original['id'],))
        self.transport.on_send=mutate
        with self.assertRaises(ValueError):
            await self.claims.reconcile_owner_reset(original['id'],True)
        self.assertEqual(self.store.get('retired_claims',[]),[])

    async def test_restart_reset_recovery_requires_new_scan(self):
        original=await self.uncertain()
        self.store.close()
        self.store=Store(self.temp.name,KEY)
        self.runtime.store=self.store
        before=self.transport.scans
        result=await self.claims.reconcile_owner_reset(original['id'],True)
        self.assertTrue(result['retired'])
        self.assertEqual(self.transport.scans,before+1)

    async def test_claim_session_change_during_dispatch_stops_association(self):
        await self.claims.discover()
        def boundary(frame):
            p=open_envelope(frame)
            if p[0]==4:
                self.runtime.session()
        self.transport.on_send=boundary
        with self.assertRaises(RuntimeError):
            await self.claims.claim(IDENTITY,'Interrupted',127,inventory_complete=True)
        self.assertEqual(self.store.journals(pending=True)[0]['status'],'uncertain')
        self.assertEqual(len(self.transport.starts),1)
        self.assertEqual(self.store.inventory()['devices'],[])

    async def test_known_hash_collision_survives_later_scan_and_candidate_eviction(self):
        self.transport.scan_rows=lambda scan:[IDENTITY] if scan==1 else [SECOND]
        with patch('avionmesh_pi.commissioning.uuid_hash', return_value=123):
            await self.claims.discover()
            await self.claims.discover()
            self.claims.candidates.pop(IDENTITY)
            with self.assertRaises(ValueError):
                await self.claims.claim(SECOND,'Collision',93,inventory_complete=True)
            self.assertEqual(self.store.get('unpaired_hash_conflicts'),[123])
            self.assertEqual(self.transport.starts,[])
            replacement=Commissioning(self.runtime)
            await replacement.discover(seconds=0)
            with self.assertRaises(ValueError):
                await replacement.claim(SECOND,'Collision after restart',93,inventory_complete=True)

    async def test_identity_history_capacity_refuses_claim(self):
        self.store.set('unpaired_identity_hashes',{str(i):'ff'*16 for i in range(512)})
        await self.claims.discover()
        self.assertTrue(self.store.get('unpaired_history_full'))
        with self.assertRaises(ValueError):
            await self.claims.claim(IDENTITY,'Unrecorded',93,inventory_complete=True)
        self.assertEqual(self.transport.starts,[])

    async def test_selected_wired_label_does_not_qualify_light_uuid_for_reset(self):
        for product in (127,91):
            identifier=self.store.begin('claim',40000+product,{
                'uuid':IDENTITY,'uuid_hash':uuid_hash(bytes.fromhex(IDENTITY)),
                'product_id':product,'inventory_complete':True,'association_complete':False})
            self.store.transition(identifier,'prepared','attempted')
            self.store.transition(identifier,'attempted','uncertain')
            with self.assertRaises(ValueError):
                await self.claims.reconcile_owner_reset(identifier,True)
            journal=next(j for j in self.store.journals() if j['id']==identifier)
            self.assertNotIn('verify_claim_owner_reset',recovery_advice(journal)['actions'])
        self.assertEqual(self.transport.frames,[])

    async def test_accessory_uuid_profile_allows_declared_reset_proof(self):
        original=await self.uncertain(91)
        result=await self.claims.reconcile_owner_reset(original['id'],True)
        self.assertTrue(result['retired'])

    async def test_bulk_light_label_does_not_admit_wired_uuid(self):
        with self.assertRaises(ValueError):
            await self.claims.claim_many(self.rows((WIRED,)),inventory_complete=True)
        self.assertEqual(self.transport.frames,[])

    async def test_publication_failure_after_durable_claim_reports_associated(self):
        await self.claims.discover()
        with patch.object(self.runtime, 'session', side_effect=RuntimeError('synthetic publication failure')):
            result = await self.claims.claim(IDENTITY,'Durable',93,inventory_complete=True)
        self.assertTrue(result['associated'])
        self.assertTrue(result['requires_review'])
        self.assertEqual(self.store.journals()[0]['status'],'verified')
        self.assertEqual(len(self.store.inventory()['devices']),1)
        self.assertFalse(self.runtime.mesh_ready)
        self.assertEqual(len(self.transport.starts),1)

    async def test_batch_stops_but_preserves_durable_success_after_publication_failure(self):
        with patch.object(self.runtime, 'session', side_effect=RuntimeError('synthetic publication failure')):
            result=await self.claims.claim_many(self.rows(),inventory_complete=True)
        self.assertFalse(result['complete'])
        self.assertTrue(result['results'][0]['associated'])
        self.assertEqual(result['remaining'],[SECOND])
        self.assertEqual(len(self.transport.starts),1)

    def test_wired_catalog_hint_remains_advisory(self):
        from avionmesh_pi.identity import advertised_identity
        hint=advertised_identity(WIRED)
        self.assertEqual(hint['candidate_model'],127)
        self.assertFalse(hint['model_verified'])

    async def test_disconnect_failure_cannot_mask_durable_claim(self):
        await self.claims.discover()
        with patch.object(self.runtime, 'session', side_effect=RuntimeError('publication')), patch.object(self.transport, 'disconnect', side_effect=RuntimeError('disconnect')):
            result = await self.claims.claim(IDENTITY,'Durable',93,inventory_complete=True)
        self.assertTrue(result['associated'])
        self.assertTrue(result['requires_review'])
        self.assertEqual(self.store.journals()[0]['status'],'verified')
        self.assertFalse(self.runtime.mesh_ready)

    async def test_reset_publication_failure_preserves_retirement(self):
        original=await self.uncertain(127)
        with patch.object(self.runtime,'session',side_effect=RuntimeError('publication')):
            result=await self.claims.reconcile_owner_reset(original['id'],True)
        self.assertTrue(result['retired'])
        self.assertTrue(result['requires_review'])
        self.assertIn(original['id'],self.store.get('retired_claims'))
