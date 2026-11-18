"""Synthetic imported-light removal; no cloud, Bluetooth or physical reset."""
import copy
import json
import tempfile
import unittest

from avionmesh_pi.association import uuid_hash
from avionmesh_pi.cloud_provenance import retain_cloud_devices
from avionmesh_pi.errors import OperationNotStarted
from avionmesh_pi.lifecycle import Lifecycle
from avionmesh_pi.migration import admit_cloud_import, canonical_digest
from avionmesh_pi.protocol import Packet, decrypt_mcp
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.storage import Store
from test_service import KEY, INVENTORY, FakeTransport

A19 = '0011223344561400000a000000000001'
OTHER = '1021324354661400000a000000000002'
RL4 = '20314253647614000004000000000003'


def observation(identity):
    return {'uuid':identity,'uuid_hash':uuid_hash(bytes.fromhex(identity))}


class Scan:
    scan_truncated = False
    def __init__(self):
        self.results=[];self.calls=0;self.error=None
    async def discover(self):
        self.calls+=1
        if self.error:raise self.error
        return copy.deepcopy(self.results)


class ImportedRemovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name,KEY)
        admit_cloud_import(self.store,location_id='synthetic-home',inventory_digest=canonical_digest(INVENTORY))
        self.store.import_inventory(INVENTORY)
        self.store.set('cloud_location',{'id':'synthetic-home','name':'Synthetic home'})
        self.transport=FakeTransport()
        self.runtime=Runtime(self.store,KEY,self.transport,clock=lambda:10)
        self.runtime.mesh_ready=self.runtime.mqtt_ready=True
        self.runtime.capabilities['removal']=True
        self.runtime.session()
        self.pings=[]
        self.runtime.exchange=self.ping
        self.scan=Scan();self.lifecycle=Lifecycle(self.runtime,self.scan)
    async def asyncTearDown(self):
        self.store.close();self.temp.cleanup()
    async def ping(self,command,predicate,timeout=3,**kwargs):
        self.pings.append(command)
        reply=Packet(12,40002,32768,115,bytes([6,255,command[7],1,0,0,0,20,0,10]))
        self.assertTrue(predicate(reply))
        return reply
    async def remove(self):
        result=await self.lifecycle.remove(40002)
        self.assertEqual(result['identity_source'],'cloud_import')
        self.assertFalse(result['removed'])
        self.assertEqual(decrypt_mcp(self.transport.frames[-1],KEY).opcode,4)
        return result['transaction']
    async def offer(self,identifier,identities=(A19,)):
        self.scan.results=[observation(identity) for identity in identities]
        return await self.lifecycle.reconcile(identifier)

    async def test_legacy_rename_is_explicit_owner_selected_origin_and_preview_is_read_only(self):
        inventory=self.store.inventory();inventory['devices'][1].update(name='Living room',exposed=False)
        self.store.set('inventory',inventory)
        before=list(self.store.db.iterdump())
        preview=self.lifecycle.preview_remove(40002)
        self.assertEqual(before,list(self.store.db.iterdump()))
        self.assertTrue(preview['enabled']);self.assertTrue(preview['requires_owner_confirmation'])
        self.assertFalse(preview['requires_exact_uuid_scan']);self.assertFalse(preview['active_relay_identity_known'])
        identifier=await self.remove()
        receipt=next(j for j in self.store.journals() if j['id']==identifier)
        self.assertEqual(receipt['detail']['cloud_source']['authority_type'],'legacy_imported_inventory_owner_selected')
        self.assertNotIn('claim',receipt['detail']);self.assertNotIn('uuid',receipt['detail'])
        self.assertEqual([j['kind'] for j in self.store.journals()],['remove'])
        self.assertEqual(len(self.pings),1)

    async def test_removal_is_never_auto_confirmed_and_needs_new_fresh_owner_evidence(self):
        identifier=await self.remove()
        result=await self.offer(identifier)
        self.assertFalse(result['removed']);self.assertTrue(result['owner_confirmation_required'])
        self.assertEqual(result['removal_candidates'],[dict(observation(A19),product_id=134)])
        with self.assertRaises(ValueError):await self.lifecycle.confirm_imported_removal(identifier,A19,False)
        self.scan.results=[]
        with self.assertRaises(ValueError):await self.lifecycle.confirm_imported_removal(identifier,A19,True)
        self.scan.results=[observation(A19)]
        result=await self.lifecycle.confirm_imported_removal(identifier,A19,True)
        self.assertTrue(result['removed']);self.assertNotIn(40002,self.runtime.core.devices)
        receipt=next(j for j in self.store.journals() if j['id']==identifier)
        self.assertEqual(receipt['detail']['verification_proof'],{
            'kind':'owner_confirmed_observed_unpaired','owner_confirmed':True,'prior_uuid_known':False})
        self.assertEqual(self.store.get('retired_addresses'),[40002])
        self.assertFalse(self.store.get('retired_claims',[]));self.assertEqual(len(self.transport.frames),1)
        self.assertEqual(self.store.inventory()['groups'][0]['members'],[])
        self.assertTrue((await self.lifecycle.confirm_imported_removal(identifier,A19,True))['removed'])
        self.assertEqual(len(self.transport.frames),1)

    async def test_matching_candidate_must_have_been_offered_by_recovery(self):
        identifier=await self.remove();self.scan.results=[observation(A19)]
        with self.assertRaisesRegex(ValueError,'Read fresh'):await self.lifecycle.confirm_imported_removal(identifier,A19,True)
        self.assertIn(40002,self.runtime.core.devices)

    async def test_multiple_new_candidates_remain_pending(self):
        identifier=await self.remove()
        result=await self.offer(identifier,(A19,OTHER))
        self.assertEqual(len(result['removal_candidates']),2)
        with self.assertRaisesRegex(ValueError,'Exactly one'):await self.lifecycle.confirm_imported_removal(identifier,A19,True)
        self.assertIn(40002,self.runtime.core.devices)

    async def test_preexisting_unpaired_or_wrong_model_or_conflicting_hash_cannot_complete(self):
        self.scan.results=[observation(A19)]
        identifier=await self.remove()
        result=await self.offer(identifier,(A19,RL4))
        self.assertEqual(result['removal_candidates'],[])
        self.store.set('unpaired_hash_conflicts',[uuid_hash(bytes.fromhex(OTHER))])
        result=await self.offer(identifier,(OTHER,))
        self.assertEqual(result['removal_candidates'],[])

    async def test_read_only_recovery_and_confirmation_survive_restart_without_setter_retry(self):
        identifier=await self.remove();await self.offer(identifier)
        self.store.close();self.store=Store(self.temp.name,KEY)
        self.runtime=Runtime(self.store,KEY,self.transport,clock=lambda:10)
        self.runtime.mesh_ready=self.runtime.mqtt_ready=True;self.runtime.capabilities['removal']=True
        self.lifecycle=Lifecycle(self.runtime,self.scan)
        result=await self.lifecycle.reconcile(identifier)
        self.assertFalse(result['removed']);self.assertEqual(len(self.transport.frames),1)
        self.assertTrue((await self.lifecycle.confirm_imported_removal(identifier,A19,True))['removed'])
        self.assertEqual(len(self.transport.frames),1)

    async def test_postwrite_disconnect_preserves_receipt_and_never_replays(self):
        self.transport.fail=True
        with self.assertRaises(RuntimeError):await self.remove()
        receipt=self.store.journals()[0]
        self.assertEqual(receipt['kind'],'remove');self.assertEqual(receipt['status'],'uncertain')
        self.assertFalse(self.runtime.mesh_ready)
        with self.assertRaises(RuntimeError):await self.lifecycle.remove(40002)
        self.assertEqual(len(self.store.journals()),1)

    async def test_baseline_failure_or_truncation_stops_before_reset(self):
        self.scan.error=RuntimeError('offline synthetic scan')
        with self.assertRaises(OperationNotStarted) as caught:await self.lifecycle.remove(40002)
        self.assertEqual(caught.exception.code,'removal_baseline_unavailable')
        self.assertEqual(self.store.journals(),[]);self.assertEqual(self.transport.frames,[])
        self.scan.error=None;self.scan.scan_truncated=True
        with self.assertRaises(OperationNotStarted):await self.lifecycle.remove(40002)
        self.assertEqual(self.store.journals(),[])

    async def test_unresponsive_address_stops_before_reset_and_journal(self):
        async def fail(*args,**kwargs):raise TimeoutError()
        self.runtime.exchange=fail
        with self.assertRaises(OperationNotStarted) as caught:await self.lifecycle.remove(40002)
        self.assertEqual(caught.exception.code,'removal_target_unresponsive')
        self.assertEqual(self.store.journals(),[]);self.assertEqual(self.transport.frames,[])

    async def test_revision_changes_during_read_refuse_before_reset(self):
        original=self.runtime.exchange
        async def change(*args,**kwargs):
            result=await original(*args,**kwargs)
            data=self.store.inventory();data['devices'][1]['name']='Changed'
            self.store.set('inventory',data)
            return result
        self.runtime.exchange=change
        with self.assertRaises(OperationNotStarted) as caught:await self.lifecycle.remove(40002)
        self.assertEqual(caught.exception.code,'removal_review_changed')
        self.assertEqual(self.store.journals(),[]);self.assertEqual(self.transport.frames,[])

    async def test_conflicting_claim_or_invalid_admission_cannot_fall_back_to_cloud(self):
        self.store.begin('claim',40002,{'uuid':A19,'product_id':134})
        with self.assertRaises(ValueError):self.lifecycle.preview_remove(40002)
        self.store.db.execute('DELETE FROM journals')
        admission=self.store.get('tx_admission');admission['document']['state_id']='foreign'
        self.store.set('tx_admission',admission)
        with self.assertRaises(ValueError):self.lifecycle.preview_remove(40002)
        self.assertEqual(self.transport.frames,[])

    async def test_native_dependencies_and_unknown_controller_removal_stay_blocked(self):
        self.store.save_binding(40001,{'slots':[{'slot':1,'target':40002}]})
        self.assertIn('native_controller_targets_device',self.lifecycle.preview_remove(40002)['blockers'])
        with self.assertRaises(ValueError):await self.lifecycle.remove(40002)
        with self.assertRaises(ValueError):self.lifecycle.preview_remove(40001)
        self.assertEqual(self.transport.frames,[])

    async def test_cloud_provenance_preserves_reviewed_source_after_renaming(self):
        retain_cloud_devices(self.store,self.store.inventory()['devices'],'synthetic-home')
        data=self.store.inventory();data['devices'][1]['name']='Renamed';self.store.set('inventory',data)
        identifier=await self.remove()
        receipt=next(j for j in self.store.journals() if j['id']==identifier)
        self.assertEqual(receipt['detail']['cloud_source']['authority_type'],'reviewed_cloud_inventory')

    async def test_changed_authority_or_multiple_scan_identity_blocks_confirmation(self):
        identifier=await self.remove();await self.offer(identifier)
        sources=self.store.get('cloud_device_sources');sources['40002']['product_id']=93
        self.store.set('cloud_device_sources',sources)
        with self.assertRaises(ValueError):await self.lifecycle.confirm_imported_removal(identifier,A19,True)
        self.assertEqual(len(self.transport.frames),1)

    async def test_remove_and_verify_returns_pending_candidate_review(self):
        original=self.scan.discover
        async def staged():
            if self.scan.calls:self.scan.results=[observation(A19)]
            return await original()
        self.scan.discover=staged
        result=await self.lifecycle.remove_and_verify(40002)
        self.assertFalse(result['removed']);self.assertEqual(result['verification'],'owner_confirmation_required')
        self.assertEqual(len(result['removal_candidates']),1);self.assertEqual(len(self.transport.frames),1)
