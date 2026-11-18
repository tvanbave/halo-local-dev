import asyncio
import copy
import tempfile
import unittest
from types import SimpleNamespace

from avionmesh_pi.scene_engine import SceneEngine, EMPTY, KEY, TOKENS, FLOORS
from avionmesh_pi.protocol import Packet
from support import synthetic_store


class Radio:
    def __init__(self, store):
        self.store = store
        self.generation = 1
        self.operation = asyncio.Lock()
        self.mesh_ready = True
        self.transport = SimpleNamespace(ready=True)
        self.capabilities = {'inventory_complete': True}
        self.core = SimpleNamespace(devices={40001: SimpleNamespace(device_id=40001, product_id=134),
                                            40002: SimpleNamespace(device_id=40002, product_id=93)})
        self.tables = {}; self.frames = []; self.sequence = {}; self.notifications = 0
        self.fail_after_write = False; self.drop_query = False; self.fence_query = False
        self.fail_before_write = False; self.on_send = None; self.validate_bad_replies = False

    def scene_changed(self): self.notifications += 1

    async def send(self, frame, *, generation=None, admission=None):
        if admission: admission()
        if self.on_send: self.on_send(frame)
        if self.fail_before_write: raise RuntimeError('Synthetic transport failure before dispatch')
        self.frames.append(frame)
        p=frame[3:]; target=int.from_bytes(frame[:2], 'little')
        if p[0] == 2:
            ref=p[5]&31; row=self.tables.setdefault((target, ref), dict(EMPTY))
            row['brightness' if p[5]&32 else 'colour']=p[6:].hex()
        elif p[0] == 5:
            self.tables[(target,p[5])]=dict(EMPTY)
        if self.fail_after_write and p[0] in (2,5):
            self.drop_query=True; self.fail_after_write=False

    async def exchange(self, frame, predicate, timeout=3, *, generation=None):
        self.frames.append(frame)
        if self.drop_query: raise asyncio.TimeoutError('Synthetic lost readback')
        p=frame[3:]; target=int.from_bytes(frame[:2],'little'); selector=p[5]
        row=self.tables.get((target,selector&31), EMPTY)
        raw=bytes.fromhex(row['brightness' if selector&32 else 'colour'])
        self.sequence[target]=self.sequence.get(target,0)+1
        reply=Packet(self.sequence[target],target,32768,115,bytes([1,34,p[4],selector])+raw)
        if self.validate_bad_replies:
            for bad in [Packet(reply.sequence,target+1,32768,115,reply.payload),
                        Packet(reply.sequence,target,32768,115,bytes([1,34,0,selector])+raw),
                        Packet(0,target,32768,115,reply.payload),
                        Packet(reply.sequence,target,32768,115,reply.payload[:-1])]:
                assert not predicate(bad)
        if not predicate(reply): raise RuntimeError('Unexpected synthetic reply rejection')
        if self.fence_query: self.generation+=1; self.fence_query=False
        return reply


class SceneEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=synthetic_store(self.tmp.name,bytes(range(16)))
        self.inventory={'devices':[{'device_id':40001,'product_id':134,'name':'Synthetic bulb','exposed':False},
                                   {'device_id':40002,'product_id':93,'name':'Synthetic downlight','exposed':False}], 'groups':[]}
        self.store.import_inventory(self.inventory)
        self.r=Radio(self.store); self.engine=SceneEngine(self.r); self.engine.gap=0; self.engine.pair_gap=0
        self.members=[{'target':40001,'brightness':80,'kelvin':4000},
                      {'target':40002,'brightness':150,'kelvin':3000}]
    def tearDown(self): self.store.close(); self.tmp.cleanup()
    def writes(self): return [f for f in self.r.frames if f[3] in (2,5)]

    async def test_multi_create_edit_member_remove_and_single_broadcast(self):
        created=await self.engine.create('Evening',self.members,True)
        self.assertTrue(created['verified']); row=created['scene']; sid=row['id']
        self.assertEqual(len(self.writes()),4)
        self.assertTrue(self.engine.status()['scenes'][0]['experimental'])
        before=len(self.r.frames); await self.engine.activate(sid)
        self.assertEqual(len(self.r.frames),before+1)
        self.assertEqual(self.r.frames[-1],bytes.fromhex('00007300220001000101000000'))
        desired=[dict(self.members[1],brightness=200,kelvin=4500)]
        changed=await self.engine.edit(sid,'Desk',desired); self.assertTrue(changed['verified'])
        self.assertEqual(self.r.tables[(40001,1)],EMPTY)
        self.assertEqual(self.r.tables[(40002,1)],self.engine._values(desired[0]))
        removed=await self.engine.remove(sid); self.assertTrue(removed['verified'])
        self.assertEqual(self.r.tables[(40002,1)],EMPTY); self.assertEqual(self.engine.status()['scenes'],[])

    async def test_existing_and_keypad_references_are_not_overwritten(self):
        self.r.tables[(40002,1)]={'colour':'020bb800','brightness':'40000000'}
        self.store.save_binding(40003,{'slots':[{'target':2,'flags':32,'level':255}]})
        result=await self.engine.create('Isolated',[self.members[0]])
        self.assertTrue(result['verified']); self.assertEqual(result['scene']['reference'],3)
        self.assertEqual(self.r.tables[(40002,1)]['brightness'],'40000000')
        self.assertTrue(all(f[8]&31==3 for f in self.writes()))

    async def test_intent_precedes_each_write_and_uncertain_never_replayed(self):
        def journal_before_write(frame):
            if frame[3] not in (2,5):return
            pending=self.store.journals(pending=True); self.assertEqual(len(pending),1)
            self.assertEqual(pending[0]['status'],'attempted')
            step=next(s for s in pending[0]['detail']['steps'] if s['status']=='attempted')
            phase='remove' if frame[3]==5 else 'brightness' if frame[8]&32 else 'colour'
            receipt=next(c for c in step['attempts'][-1]['commands'] if c['property']==phase)
            self.assertEqual(receipt['status'],'attempted')
            self.assertEqual(receipt['request_token'],frame[7])
        self.r.on_send=journal_before_write; self.r.fail_after_write=True
        result=await self.engine.create('Interrupted',self.members)
        self.assertFalse(result['verified']); self.assertEqual(len(self.writes()),2)
        with self.assertRaises(RuntimeError): await self.engine.create('Other',self.members)
        self.assertEqual(len(self.writes()),2)
        self.store.close(); self.store=synthetic_store(self.tmp.name,bytes(range(16))); self.r.store=self.store
        self.engine=SceneEngine(self.r); self.engine.gap=0; self.engine.pair_gap=0; self.r.drop_query=False
        recovered=await self.engine.recover(result['transaction'])
        self.assertTrue(recovered['resume_available']); self.assertEqual(len(self.writes()),2)
        self.assertEqual(len(self.engine.status()['operations']),1)
        resumed=await self.engine.resume(result['transaction']); self.assertTrue(resumed['verified'])
        self.assertEqual(len(self.writes()),4)
        self.assertEqual(sum(f[3]==2 and int.from_bytes(f[:2],'little')==40001 and f[8]==1 for f in self.writes()),1)

    async def test_failed_first_dispatch_empty_reads_preserve_staging_ambiguity(self):
        self.r.fail_before_write=True
        result=await self.engine.create('Ambiguous dispatch',[self.members[0]])
        self.r.fail_before_write=False
        recovered=await self.engine.recover(result['transaction'])
        self.assertEqual(recovered['recovery_mode'],'continue_unattempted')
        self.assertEqual(len(self.writes()),0)
        self.assertEqual(self.engine.status()['scenes'][0]['status'],'needs_review')
        self.assertEqual(len(self.store.journals(pending=True)),1)
        with self.assertRaises(RuntimeError):await self.engine.create('Other',[self.members[0]])

    async def test_delete_uncertain_recovery_verifies_absence_without_second_delete(self):
        row=(await self.engine.create('Delete test',[self.members[0]]))['scene']; self.r.frames.clear()
        self.r.fail_after_write=True
        result=await self.engine.remove(row['id']); self.assertFalse(result['verified'])
        self.r.drop_query=False
        recovered=await self.engine.recover(result['transaction']); self.assertTrue(recovered['verified'])
        self.assertEqual(len(self.writes()),1); self.assertEqual(self.engine.status()['scenes'],[])

    async def test_epoch_and_inventory_change_block_dispatch_and_recovery(self):
        self.r.fence_query=True
        with self.assertRaises(RuntimeError): await self.engine.create('Epoch',self.members)
        self.assertEqual(self.writes(),[])
        self.r.fail_after_write=True; result=await self.engine.create('Pending',self.members)
        self.r.drop_query=False
        inv=copy.deepcopy(self.inventory); inv['devices'][0]['name']='Changed'; self.store.set('inventory',inv)
        with self.assertRaises(RuntimeError): await self.engine.recover(result['transaction'])
        self.assertEqual(len(self.writes()),2)

    async def test_external_change_after_reboot_requires_explicit_verification(self):
        sid=(await self.engine.create('Scoped',[self.members[0]]))['scene']['id']
        self.engine=SceneEngine(self.r); self.engine.gap=0; self.engine.pair_gap=0
        self.r.tables[(40002,1)]={'colour':'020bb800','brightness':'40000000'}
        self.engine.observe(Packet(100,40002,32768,115,bytes.fromhex('02229905')))
        self.r.frames.clear()
        with self.assertRaises(RuntimeError): await self.engine.activate(sid)
        self.assertEqual(self.r.frames,[])
        with self.assertRaises(RuntimeError): await self.engine.verify(sid)
        self.assertFalse(any(f[3]==0 for f in self.r.frames))

    async def test_more_than255_queries_and_corrupt_or_stale_replies(self):
        self.r.validate_bad_replies=True
        self.store.set(FLOORS,{'40001':1}); self.r.sequence[40001]=1
        for _ in range(70):
            self.assertEqual(await self.engine._read(40001,1,1,self.engine._identity()),EMPTY)
        self.assertEqual(self.store.get(TOKENS)['40001'],280)
        self.assertEqual(self.store.get(FLOORS)['40001'],281)
        self.assertEqual(len(self.r.frames),280)

    async def test_admission_is_checked_at_activation_dispatch(self):
        sid=(await self.engine.create('Admission',[self.members[0]]))['scene']['id']; self.r.frames.clear()
        def expired(): raise RuntimeError('Broker command expired')
        with self.assertRaises(RuntimeError): await self.engine.activate(sid,admission=expired)
        self.assertEqual(self.r.frames,[])

    async def test_exposure_does_not_send_and_busy_or_invalid_input_refuses(self):
        sid=(await self.engine.create('Share',[self.members[0]]))['scene']['id']; self.r.frames.clear()
        self.engine.set_exposed(sid,True); self.assertEqual(self.r.frames,[])
        self.assertTrue(self.engine.status()['scenes'][0]['exposed'])
        for bad in [[dict(self.members[0],brightness=True)], [self.members[0],self.members[0]], []]:
            with self.assertRaises(ValueError): await self.engine.create('Bad',bad)
        self.r.capabilities['native_scenes']=False
        with self.assertRaises(ValueError): await self.engine.activate(sid)

    async def test_owner_change_blocks_scene_adoption_and_lease_status_is_correct(self):
        sid=(await self.engine.create('Owner',[self.members[0]]))['scene']['id']
        self.assertTrue(self.engine.status()['scenes'][0]['activation_ready'])
        self.engine.leases.clear(); await self.engine.verify(sid)
        self.assertTrue(self.engine.status()['scenes'][0]['activation_ready'])
        self.store.set('mesh_fingerprint','new synthetic mesh')
        self.assertFalse(self.engine.status()['scenes'][0]['ownership_valid'])
        self.assertEqual(self.engine.status()['scenes'][0]['status'],'needs_review')
        with self.assertRaises(RuntimeError): await self.engine.activate(sid)
        with self.assertRaises(RuntimeError): await self.engine.edit(sid,'New',self.members)

    async def test_partial_pair_requires_explicit_new_attempt_with_fresh_readback(self):
        def fail_second(frame):
            if frame[3]==2 and len(self.writes())==1:self.r.fail_before_write=True
        self.r.on_send=fail_second
        result=await self.engine.create('Partial',self.members)
        self.assertFalse(result['verified']); self.assertEqual(len(self.writes()),1)
        self.r.fail_before_write=False; self.r.on_send=None
        recovered=await self.engine.recover(result['transaction'])
        self.assertTrue(recovered['resume_available'])
        self.assertTrue(recovered['retry_requires_explicit_action'])
        self.assertEqual(recovered['recovery_mode'],'retry_pair')
        self.assertEqual(len(self.writes()),1)
        resumed=await self.engine.resume(result['transaction']); self.assertTrue(resumed['verified'])
        self.assertEqual(len(self.writes()),5)
        j=next(j for j in self.store.journals() if j['id']==result['transaction'])
        attempts=j['detail']['steps'][0]['attempts']
        self.assertEqual(len(attempts),2)
        self.assertEqual(attempts[0]['readback_outcome'],'partial_readable_state')
        self.assertEqual(attempts[1]['before'],{'colour':'020fa000','brightness':'ffffffff'})
        self.assertNotEqual(attempts[0]['commands'][0]['request_token'],attempts[1]['commands'][0]['request_token'])

    async def test_external_mutation_revokes_lease_and_prevents_wrong_recall(self):
        sid=(await self.engine.create('Foreign',[self.members[0]]))['scene']['id']
        self.engine.observe(Packet(100,40001,32768,115,bytes.fromhex('022200009901020bb800')))
        self.assertFalse(self.engine.status()['scenes'][0]['activation_ready'])
        self.r.tables[(40001,1)]['colour']='020bb800'
        self.r.frames.clear()
        with self.assertRaises(RuntimeError): await self.engine.activate(sid)
        self.assertFalse(any(f[3]==0 for f in self.r.frames))

    async def test_incomplete_inventory_never_sends_any_scene_command(self):
        self.r.capabilities['inventory_complete']=False
        with self.assertRaises(RuntimeError): await self.engine.create('Incomplete',self.members)
        self.assertEqual(self.r.frames,[])

    async def test_mutation_during_survey_cannot_restore_lease(self):
        sid=(await self.engine.create('Race',[self.members[0]]))['scene']['id']
        self.engine.leases.clear(); real_exchange=self.r.exchange; fired=False
        async def mutate(frame,predicate,*args,**kwargs):
            nonlocal fired
            reply=await real_exchange(frame,predicate,*args,**kwargs)
            if int.from_bytes(frame[:2],'little')==40002 and not fired:
                fired=True
                self.engine.observe(Packet(100,40001,32768,115,bytes.fromhex('022200009901020bb800')))
            return reply
        self.r.exchange=mutate; self.r.frames.clear()
        with self.assertRaises(RuntimeError): await self.engine.verify(sid)
        self.assertFalse(self.engine.status()['scenes'][0]['activation_ready'])
        self.assertFalse(any(f[3]==0 for f in self.r.frames))

    async def test_corrupted_journal_never_dispatches_queries_or_writes(self):
        import json
        self.r.fail_after_write=True
        result=await self.engine.create('Corruption',self.members)
        self.r.drop_query=False
        journal=next(j for j in self.store.journals() if j['id']==result['transaction'])
        original=journal['detail']
        for change in ['target','value','final','ready','status']:
            bad=copy.deepcopy(original)
            if change=='target':bad['steps'][0]['target']=40002
            if change=='value':bad['steps'][0]['value']='020a8c00'
            if change=='final':bad['final']['40001']['brightness']='ff000000'
            if change=='ready':bad['recovery_ready']=True
            if change=='status':bad['steps'][0]['status']='verified'
            self.store.db.execute('UPDATE journals SET detail=? WHERE id=?',(json.dumps(bad),result['transaction']))
            self.r.frames.clear()
            with self.assertRaises(RuntimeError):await self.engine.recover(result['transaction'])
            self.assertEqual(self.r.frames,[])
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?',(json.dumps(original),result['transaction']))

    async def test_mismatched_complete_pair_is_retained_without_more_writes(self):
        real_send=self.r.send
        async def wrong_colour(frame,**kwargs):
            await real_send(frame,**kwargs)
            if frame[3]==2 and frame[8]==33:self.r.tables[(40001,1)]['colour']='020bb800'
        self.r.send=wrong_colour
        result=await self.engine.create('Foreign value',self.members)
        self.assertFalse(result['verified']);self.assertEqual(len(self.writes()),2)
        journal=next(j for j in self.store.journals() if j['id']==result['transaction'])
        attempt=journal['detail']['steps'][0]['attempts'][0]
        self.assertEqual(attempt['readback'],{'colour':'020bb800','brightness':'50000000'})
        self.assertEqual(attempt['readback_rounds'],2)
        self.assertFalse(attempt['readback_matches_expected'])
        self.assertEqual(journal['detail']['steps'][1]['status'],'not_started')
        self.assertEqual(journal['detail']['observed']['40001'],EMPTY)
        with self.assertRaises(RuntimeError):await self.engine.recover(result['transaction'])
        with self.assertRaises(RuntimeError):await self.engine.resume(result['transaction'])
        self.assertEqual(len(self.writes()),2)

    async def test_unchanged_readback_diagnostic_is_not_trusted_by_recovery(self):
        import json
        async def ignored_setter(frame,**kwargs):self.r.frames.append(frame)
        self.r.send=ignored_setter
        result=await self.engine.create('No readable change',[self.members[0]])
        self.assertFalse(result['verified']);self.assertEqual(len(self.writes()),2)
        journal=next(j for j in self.store.journals() if j['id']==result['transaction'])
        attempt=journal['detail']['steps'][0]['attempts'][0]
        self.assertEqual(attempt['readback'],EMPTY)
        attempt['readback']=copy.deepcopy(attempt['expected'])
        attempt['readback_matches_expected']=True
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?',
                             (json.dumps(journal['detail']),result['transaction']))
        recovered=await self.engine.recover(result['transaction'])
        self.assertEqual(recovered['recovery_mode'],'retry_pair')
        self.assertFalse(recovered['verified']);self.assertEqual(len(self.writes()),2)
        self.assertEqual(len(self.store.journals(pending=True)),1)

    async def test_no_read_between_paired_writes_including_brightness_only_edit(self):
        created=await self.engine.create('Paired',[self.members[0]])
        self.assertTrue(created['verified'])
        writes=[i for i,f in enumerate(self.r.frames) if f[3]==2]
        self.assertEqual(writes[1],writes[0]+1)
        self.r.frames.clear()
        result=await self.engine.edit(created['scene_id'],'Paired',[dict(self.members[0],brightness=90)])
        self.assertTrue(result['verified']);self.assertEqual(len(self.writes()),2)
        writes=[i for i,f in enumerate(self.r.frames) if f[3]==2]
        self.assertEqual(writes[1],writes[0]+1)

    async def test_session_change_between_pair_commands_never_sends_brightness(self):
        real_send=self.r.send
        async def change_session(frame,**kwargs):
            await real_send(frame,**kwargs)
            if frame[3]==2:self.r.generation+=1
        self.r.send=change_session
        result=await self.engine.create('Session change',[self.members[0]])
        self.assertFalse(result['verified']);self.assertEqual(len(self.writes()),1)
        journal=self.store.journals(pending=True)[0]
        commands=journal['detail']['steps'][0]['attempts'][0]['commands']
        self.assertEqual(commands[0]['status'],'transport_accepted')
        self.assertEqual(commands[1]['status'],'not_started')

    async def test_final_readable_pair_does_not_skip_untouched_brightness_commit(self):
        created=await self.engine.create('Initial',[self.members[0]])
        self.r.frames.clear();real_send=self.r.send
        async def interrupt_after_colour(frame,**kwargs):
            await real_send(frame,**kwargs)
            if frame[3]==2 and not frame[8]&32:raise RuntimeError('Synthetic transport exception after colour')
        self.r.send=interrupt_after_colour
        result=await self.engine.edit(created['scene_id'],'Changed',[dict(self.members[0],kelvin=3000)])
        self.assertFalse(result['verified']);self.assertEqual(len(self.writes()),1)
        self.r.send=real_send
        recovered=await self.engine.recover(result['transaction'])
        self.assertFalse(recovered['verified'])
        self.assertEqual(recovered['recovery_mode'],'continue_unattempted')
        self.assertEqual(len(self.writes()),1)
        result=await self.engine.resume(result['transaction'])
        self.assertTrue(result['verified']);self.assertEqual(len(self.writes()),2)
        self.assertEqual(self.writes()[-1][8],33)
