"""A19 public-key-only recovery; synthetic ECDH peer, no radio or live identifiers."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import test_claim_development as base
from test_association import Peer
from avionmesh_pi.association import open_envelope, uuid_hash
from avionmesh_pi.commissioning import Commissioning
from avionmesh_pi.identity import a19_public_key_claim_profile
from avionmesh_pi.lifecycle import Lifecycle
from avionmesh_pi.management import recovery_advice
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store

A19='0011223344561400000a000000000001'
OTHER='1021324354661400000a000000000002'
FINGERPRINT=uuid_hash(bytes.fromhex(A19))


def receipt():
    return {'id':'synthetic-a19','kind':'claim','status':'uncertain','target':40003,
        'detail':{'uuid':A19,'uuid_hash':FINGERPRINT,'product_id':134,'name':'A19 retry',
            'inventory_complete':True,'association_complete':False,'setup_requested':False,
            'association_phase':'public_key','submissions_attempted':5,'submissions_accepted':5}}


class A19ProfileTests(unittest.TestCase):
    def test_only_consistent_public_key_counts_2_through_7(self):
        for count in range(2,8):
            item=receipt();item['detail'].update(submissions_attempted=count,submissions_accepted=count)
            self.assertTrue(a19_public_key_claim_profile(item))
        for count in (0,1,8,9,10,11,12,True,5.0,'5',None):
            item=receipt();item['detail'].update(submissions_attempted=count,submissions_accepted=count)
            self.assertFalse(a19_public_key_claim_profile(item))
    def test_missing_ambiguous_or_later_phase_evidence_is_rejected(self):
        defects={'association_phase':['association','confirm','random','device_id','network_key','complete',None],
            'association_complete':[True,0,None],'setup_requested':[True,0,None],
            'inventory_complete':[False,1,None],'submissions_accepted':[4,6,True,None],
            'uuid_hash':[FINGERPRINT+1,True,None], 'uuid':[OTHER,base.IDENTITY,'00'*16,None],
            'product_id':[93,127,91,134.0,True,None]}
        for key,values in defects.items():
            for value in values:
                with self.subTest(key=key,value=value):
                    item=receipt();item['detail'][key]=value
                    self.assertFalse(a19_public_key_claim_profile(item))
            item=receipt();item['detail'].pop(key)
            self.assertFalse(a19_public_key_claim_profile(item))
    def test_recovery_advice_is_read_only_and_not_reset(self):
        advice=recovery_advice(receipt())
        self.assertEqual(advice['actions'],['retire_pre_key_claim'])
        self.assertFalse(advice['retry_allowed']);self.assertFalse(advice['physical_reset_verified'])


class A19RetirementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base.ClaimDevelopmentTests.asyncSetUp(self)
        self.runtime.capabilities.update(ordinary_claim=True,removal=True)
        self.transport.ads=[A19]
        self.transport.peers={FINGERPRINT:Peer()}
    asyncTearDown=base.ClaimDevelopmentTests.asyncTearDown

    async def old_completed_and_removed(self):
        await self.claims.discover()
        accepted=await self.claims.claim(A19,'Previously paired A19',134,inventory_complete=True)
        target=accepted['device_id']
        old=next(j for j in self.store.journals() if j['kind']=='claim')
        result=await Lifecycle(self.runtime,self.claims).remove_and_verify(target)
        self.assertTrue(result['removed'])
        proof=next(j for j in self.store.journals() if j['kind']=='remove')
        self.assertEqual(old['detail']['submissions_attempted'],12)
        self.assertEqual(old['detail']['submissions_accepted'],12)
        self.assertEqual(old['status'],'verified')
        self.assertNotIn(target,self.runtime.core.devices)
        return old,proof

    async def failed(self):
        self.transport.peers[FINGERPRINT]=Peer()
        await self.claims.discover()
        send=self.transport.send;wait_for=asyncio.wait_for
        async def lose_chunk(frame):
            p=open_envelope(frame)
            if p[0]==4 and p[5]==3:
                self.transport.frames.append(frame)
                self.transport.peers[FINGERPRINT].respond(frame)
                return
            await send(frame)
        async def bounded(awaitable,timeout):
            return await wait_for(awaitable,min(timeout,.02 if timeout==5 else 1))
        with patch.object(self.transport,'send',side_effect=lose_chunk),patch('avionmesh_pi.commissioning.asyncio.wait_for',side_effect=bounded):
            with self.assertRaises(asyncio.TimeoutError):
                await self.claims.claim(A19,'Retry A19',134,inventory_complete=True)
        item,=self.store.journals(pending=True)
        self.assertEqual(item['detail']['association_phase'],'public_key')
        self.assertEqual(item['detail']['submissions_attempted'],5)
        self.assertEqual(item['detail']['submissions_accepted'],5)
        self.assertIsNone(self.transport.peers[FINGERPRINT].target)
        self.assertFalse(self.transport.peers[FINGERPRINT].netkey)
        self.runtime.mesh_ready=self.transport.ready=True
        return item

    def replace(self,item):
        self.store.db.execute('UPDATE journals SET kind=?,target=?,status=?,detail=? WHERE id=?',
            (item['kind'],item['target'],item['status'],json.dumps(item['detail']),item['id']))

    async def assert_blocked(self,item):
        before=len(self.transport.frames)
        with self.assertRaises((ValueError,RuntimeError)):await self.claims.reconcile_pre_key(item['id'])
        self.assertEqual(len(self.transport.frames),before)
        self.assertEqual(next(j for j in self.store.journals() if j['id']==item['id'])['status'],'uncertain')

    async def test_new_a19_checkpoint_retires_with_one_scan_and_no_setter(self):
        item=await self.failed();before=len(self.transport.frames)
        result=await self.claims.reconcile_pre_key(item['id'])
        self.assertTrue(result['pre_key_retired']);self.assertFalse(result['association_retried'])
        self.assertFalse(result['reset_verified']);self.assertFalse(result['automatic_reclaim'])
        self.assertEqual([open_envelope(frame)[0] for frame in self.transport.frames[before:]],[0])
        proof=next(j for j in self.store.journals() if j['kind']=='claim_pre_key')
        self.assertEqual(proof['detail']['dispatch_profile'],'a19-public-key-2-through-7')
        self.assertFalse(proof['detail']['network_key_dispatched'])
        self.assertFalse(proof['detail']['device_id_dispatched'])

    async def test_exact_verified_previous_removal_allows_new_checkpoint(self):
        old,proof=await self.old_completed_and_removed();item=await self.failed()
        before=len(self.transport.frames);starts=list(self.transport.starts);inventory=self.store.inventory()
        result=await self.claims.reconcile_pre_key(item['id'])
        self.assertTrue(result['retired']);self.assertEqual(self.transport.starts,starts)
        self.assertEqual([open_envelope(frame)[0] for frame in self.transport.frames[before:]],[0])
        self.assertEqual(self.store.inventory(),inventory)
        self.assertEqual(next(j for j in self.store.journals() if j['id']==old['id']),old)
        self.assertEqual(next(j for j in self.store.journals() if j['id']==proof['id']),proof)

    async def test_prior_completed_claim_needs_full_exact_removal_receipt(self):
        old,proof=await self.old_completed_and_removed();item=await self.failed()
        for field,value in (('kind','external_reset'),('status','uncertain'),('target',proof['target']+20)):
            modified=copy.deepcopy(proof);modified[field]=value;self.replace(modified)
            await self.assert_blocked(item);self.replace(proof)
        for field,value in (('claim','wrong'),('uuid',OTHER),('uuid_hash',FINGERPRINT+1),('product_id',93),
                ('identity_source','cloud_import'),('verification_proof',{'kind':'owner_confirmed_observed_unpaired'}),
                ('backup',{}),('backup',{'devices':[]})):
            with self.subTest(field=field):
                modified=copy.deepcopy(proof);modified['detail'][field]=value;self.replace(modified)
                await self.assert_blocked(item);self.replace(proof)
        self.store.db.execute('DELETE FROM journals WHERE id=?',(proof['id'],))
        await self.assert_blocked(item)

    async def test_prior_completed_receipt_and_retirement_marks_cannot_be_ambiguous(self):
        old,proof=await self.old_completed_and_removed();item=await self.failed()
        for field,value in (('association_complete',False),('submissions_attempted',11),('submissions_accepted',11),
                ('inventory_complete',False),('association_phase','device_id'),('uuid',OTHER),('product_id',93)):
            modified=copy.deepcopy(old);modified['detail'][field]=value;self.replace(modified)
            await self.assert_blocked(item);self.replace(old)
        claims=self.store.get('retired_claims');self.store.set('retired_claims',[])
        await self.assert_blocked(item);self.store.set('retired_claims',claims)
        addresses=self.store.get('retired_addresses');self.store.set('retired_addresses',[])
        await self.assert_blocked(item);self.store.set('retired_addresses',addresses)
        inventory=self.store.inventory()
        altered=copy.deepcopy(inventory);altered['devices'].append(proof['detail']['backup']['devices'][0])
        self.store.set('inventory',altered);await self.assert_blocked(item)

    async def test_current_later_phase_or_bad_counts_never_retires(self):
        item=await self.failed()
        for phase,count in (('association',1),('confirm',8),('random',9),('device_id',10),('network_key',11),('network_key',12)):
            modified=copy.deepcopy(item);modified['detail'].update(association_phase=phase,submissions_attempted=count,submissions_accepted=count)
            self.replace(modified);await self.assert_blocked(item);self.replace(item)
        modified=copy.deepcopy(item);modified['detail']['submissions_accepted']=4
        self.replace(modified);await self.assert_blocked(item)

    async def test_history_hash_collision_never_retires(self):
        item=await self.failed();self.store.set('unpaired_hash_conflicts',[FINGERPRINT]);before=len(self.transport.frames)
        result=await self.claims.reconcile_pre_key(item['id'])
        self.assertFalse(result['retired']);self.assertEqual([open_envelope(f)[0] for f in self.transport.frames[before:]],[0])

    async def test_restart_preserves_receipt_and_requires_new_exact_scan(self):
        old,proof=await self.old_completed_and_removed();item=await self.failed()
        self.store.close();self.store=Store(self.temp.name,base.KEY)
        self.transport.ready=False
        self.runtime=Runtime(self.store,base.KEY,self.transport,clock=lambda:10)
        self.transport.runtime=self.runtime
        self.runtime.capabilities['ordinary_claim']=True
        self.runtime.mesh_ready=self.transport.ready=True
        self.claims=Commissioning(self.runtime)
        original=self.claims.discover
        async def fast(seconds=5):return await original(seconds=0)
        self.claims.discover=fast
        self.assertIn(item['id'],self.runtime.claim_isolated)
        before=len(self.transport.frames)
        result=await self.claims.reconcile_pre_key(item['id'])
        self.assertTrue(result['retired']);self.assertEqual([open_envelope(f)[0] for f in self.transport.frames[before:]],[0])

    async def test_subsequent_prekey_attempt_requires_its_own_retirement_proof_too(self):
        old,proof=await self.old_completed_and_removed();first=await self.failed()
        await self.claims.reconcile_pre_key(first['id'])
        second=await self.failed()
        self.assertNotEqual(first['target'],second['target'])
        result=await self.claims.reconcile_pre_key(second['id'])
        self.assertTrue(result['retired'])
        self.assertIn(first['target'],self.store.get('retired_addresses'))
        self.assertIn(second['target'],self.store.get('retired_addresses'))

    async def test_missing_fresh_observation_does_not_retire_or_repeat_pairing(self):
        item=await self.failed();self.transport.ads=[];before=len(self.transport.frames)
        result=await self.claims.recover_safe_pending(item['id'])
        self.assertEqual(result['outcome'],'unpaired_not_observed');self.assertEqual(result['ping_attempts'],0)
        self.assertEqual([open_envelope(f)[0] for f in self.transport.frames[before:]],[0])
        self.assertEqual(self.store.journals(pending=True)[0]['id'],item['id'])

    async def test_proof_removed_during_scan_cannot_commit(self):
        old,proof=await self.old_completed_and_removed();item=await self.failed()
        def changed(scan):
            self.store.db.execute('DELETE FROM journals WHERE id=?',(proof['id'],))
            return [A19]
        self.transport.scan_rows=changed
        with self.assertRaises(ValueError):await self.claims.reconcile_pre_key(item['id'])
        self.assertEqual(self.store.journals(pending=True)[0]['id'],item['id'])
