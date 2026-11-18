"""Offline replacement restore with synthetic SQLite bytes and no service access."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.migration import canonical_digest, status
from avionmesh_pi.restore_state import (prepare_restored_database, validate_restored_database,
    RestoreStateError, RESTORE_ADMISSION, RESTORE_HISTORY, SCENES, SCENE_PROOFS)
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.storage import Store
from support import synthetic_store

DIGEST = hashlib.sha256(b'synthetic full encrypted backup').hexdigest()
CREATED = '2026-01-02T03:04:05+00:00'
KEY = bytes(range(16))


class RestoreStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = synthetic_store(self.root/'source', KEY)
        self.store.import_inventory({'devices': [
            {'device_id':40001,'product_id':134,'name':'Bulb one','exposed':True},
            {'device_id':40002,'product_id':126,'name':'Battery keypad','exposed':True}],
            'groups':[{'group_id':300,'name':'Example','members':[40001],'protected':False,'exposed':True}]})
        self.store.set('device_rooms', {'40001':'Example','40002':'Example'})
        self.store.set('sequence', 123)
        self.store.set('private_future_setting', {'credential':'synthetic-private-credential'})
        self.store.set('battery_mapping_cache_v1', {'40002':{'state':'cached','marker':'preserve-exact'}})
        self.store.save_binding(40002, {'count':'synthetic','slots':[{'target':301}]})
        runtime = Runtime(self.store, KEY, type('Peer', (), {'ready':False})())
        self.scene = {'id':'synthetic-scene','reference':1,'name':'Reading',
            'members':[{'target':40001,'brightness':128,'kelvin':4000}],
            'exposed':True,'owner':runtime.scene_engine._owner()}
        self.store.set(SCENES, [self.scene])
        self.store.set(SCENE_PROOFS, {'synthetic-scene':runtime.scene_engine._stamp(self.scene)})
        self.receipt = self.store.begin('group_membership',40001, {'group':300,'private':'synthetic-receipt-detail'})
        self.store.transition(self.receipt,'prepared','attempted')
        self.store.close()
        self.original = (self.root/'source/bridge.sqlite3').read_bytes()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def read(self, raw):
        path=self.root/'inspect.sqlite3';path.write_bytes(raw)
        with sqlite3.connect(path) as db:
            metadata={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM metadata')}
            journals={i:(kind,target,state,json.loads(detail)) for i,kind,target,state,detail in db.execute('SELECT * FROM journals')}
            bindings={t:(json.loads(s),v) for t,s,v in db.execute('SELECT * FROM bindings')}
        return metadata,journals,bindings

    def changed(self, raw=None, *, metadata=None, sql=None):
        path=self.root/'change.sqlite3';path.write_bytes(self.original if raw is None else raw)
        with sqlite3.connect(path) as db:
            for k,v in (metadata or {}).items():
                db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',(k,json.dumps(v)))
            if sql:sql(db)
        return path.read_bytes()

    def prepare(self, raw=None, **kwargs):
        return prepare_restored_database(self.original if raw is None else raw, backup_digest=DIGEST, created_at=CREATED, **kwargs)

    def refuse(self, code, raw=None, **kwargs):
        with self.assertRaises(RestoreStateError) as raised:self.prepare(raw, **kwargs)
        self.assertEqual(raised.exception.code,code)

    def test_preview_has_no_store_network_or_input_mutation(self):
        before=hashlib.sha256(self.original).digest()
        with patch('avionmesh_pi.storage.Store',side_effect=AssertionError('No Store')), patch('socket.socket',side_effect=AssertionError('No network')):
            preview=validate_restored_database(self.original,backup_digest=DIGEST,created_at=CREATED)
            prepared,report=self.prepare()
        self.assertEqual(preview,report)
        self.assertEqual(hashlib.sha256(self.original).digest(),before)
        self.assertIsInstance(prepared,bytes)
        self.assertFalse(report['history_complete'])
        self.assertEqual(report['automatically_retried_operations'],0)

    def test_replacement_preserves_credentials_identities_receipts_and_cache(self):
        restored,report=self.prepare()
        before,before_j,before_b=self.read(self.original);after,after_j,after_b=self.read(restored)
        for key in ('state_id','mesh_fingerprint','tx_admission','inventory','device_rooms','known_targets','private_future_setting','battery_mapping_cache_v1',SCENES):
            self.assertEqual(before[key],after[key],key)
        self.assertEqual(after['sequence'],123)
        self.assertEqual(after_j[self.receipt],('group_membership',40001,'uncertain',before_j[self.receipt][3]))
        self.assertEqual(after_b[40002],(before_b[40002][0],0))
        self.assertEqual(after[SCENE_PROOFS],{})
        self.assertEqual(report['pending_operations'],1)
        self.assertFalse(report['current_state_preserved'])
        self.assertTrue(report['scene_readback_required'])
        receipt=after[RESTORE_ADMISSION]
        self.assertEqual(receipt['id'],canonical_digest(receipt['document']))
        self.assertEqual(receipt['document']['original_admission_id'],before['tx_admission']['id'])
        self.assertEqual(receipt['document']['mode'],'backup_compatible_restore')
        self.assertFalse(receipt['document']['history_complete'])
        self.assertFalse(receipt['document']['previous_writers_verified_by_bridge'])

    def retired_claim_history(self):
        """Terminal records left by claim recovery and verified device removal."""
        abandoned={'id':'retired-claim','kind':'claim','target':40003,'status':'uncertain',
            'detail':{'uuid':'00112233445566778899aabbccddeeff','uuid_hash':'synthetic-retired-uuid',
                'name':'Removed A19','product_id':134,'association_complete':False,
                'association_phase':'public_key','submissions_attempted':3}}
        proof={'claim':'retired-claim','original_journal':abandoned,
            'recovery_schema':'claim-public-key-retirement-v1','dispatch_profile':'a19-public-key-2-through-7',
            'device_id_dispatched':False,'network_key_dispatched':False,'isolated_previous_worker':True}
        rows=[('retired-claim','claim',40003,'retired',abandoned['detail']),
            ('retirement-proof','claim_pre_key',40003,'verified',proof),
            ('cancelled-claim','claim',40004,'cancelled',{'product_id':134,'name':'Not dispatched',
                'cancelled_before_dispatch':True,'submissions_attempted':0,'submissions_accepted':0}),
            ('removed-claim','claim',40005,'verified',{'product_id':134,'name':'Former bulb',
                'uuid':'102132435465768798a9bacbdcedfe0f','association_complete':True}),
            ('removal-proof','device_remove',40005,'verified',{'claim':'removed-claim',
                'identity_source':'local_claim','reset_verified':True})]
        def edit(db):
            db.execute("UPDATE journals SET status='verified'")
            db.executemany('INSERT INTO journals VALUES (?,?,?,?,?)',
                [(i,k,t,status,json.dumps(detail)) for i,k,t,status,detail in rows])
        return self.changed(metadata={'retired_claims':['retired-claim','removed-claim'],
            'retired_addresses':[40003,40005],'known_targets':[300,301,399],
            # Older removals may retain cosmetic room labels for absent IDs.
            'device_rooms':{'40001':'Example','40002':'Example','40003':'Former room','49999':'Old room'}},sql=edit)

    def test_replacement_preserves_retired_cancelled_claims_and_removed_room_residue(self):
        candidate=self.retired_claim_history();before,before_j,_=self.read(candidate)
        restored,report=self.prepare(candidate);after,after_j,_=self.read(restored)
        self.assertEqual(after_j,before_j)
        for key in ('retired_claims','retired_addresses','known_targets','device_rooms'):
            self.assertEqual(after[key],before[key],key)
        self.assertEqual(report['pending_operations'],0)
        self.assertEqual(report['automatically_retried_operations'],0)
        folder=self.root/'retired-restored';folder.mkdir(mode=0o700)
        path=folder/'bridge.sqlite3';path.write_bytes(restored);path.chmod(0o600)
        with self.open_store(folder) as store:
            runtime=Runtime(store,KEY,type('Peer',(),{'ready':False})())
            self.assertEqual(set(runtime.core.devices),{40001,40002})
            self.assertEqual(store.journals(pending=True),[])
            for target in (40003,40005):
                proposed=copy.deepcopy(store.inventory())
                proposed['devices'].append({'device_id':target,'product_id':134,'name':'Reused address','exposed':False})
                with self.assertRaisesRegex(ValueError,'Retired mesh addresses'):
                    store.import_inventory(proposed)
            self.assertEqual({j['id']:j['status'] for j in store.journals()},
                {identifier:row[2] for identifier,row in before_j.items()})

    def test_same_lineage_preserves_terminal_claim_receipts_and_unions_reservations(self):
        candidate=self.retired_claim_history();before,before_j,_=self.read(candidate)
        current=self.changed(candidate,metadata={'sequence':321,'retired_addresses':[40003,40005,49999],
            'retired_claims':['retired-claim','removed-claim','older-import-retirement'],
            'known_targets':[300,301,399,500],'device_rooms':{'40001':'Renamed room'}})
        restored,report=self.prepare(candidate,current_bytes=current);after,after_j,_=self.read(restored)
        self.assertEqual(after_j,before_j)
        self.assertEqual(after['sequence'],321)
        self.assertEqual(after['retired_addresses'],[40003,40005,49999])
        self.assertEqual(set(after['retired_claims']),{'retired-claim','removed-claim','older-import-retirement'})
        self.assertEqual(after['known_targets'],[300,301,399,500])
        self.assertEqual(after['device_rooms'],before['device_rooms'])
        self.assertTrue(report['current_state_preserved']);self.assertEqual(report['pending_operations'],0)
        repeated,_=self.prepare(candidate,current_bytes=restored)
        self.assertEqual(self.read(repeated)[1],before_j)
        self.assertEqual(self.read(repeated)[0]['retired_addresses'],after['retired_addresses'])
        # A backup from before retirement cannot erase the newer terminal proof.
        older=self.changed(candidate,sql=lambda db:db.execute(
            "UPDATE journals SET status='uncertain' WHERE id='retired-claim'"))
        self.refuse('operation_history_changed',older,current_bytes=current)

    def test_runtime_scene_owner_and_truthful_admission_survive(self):
        restored,_=self.prepare();folder=self.root/'destination';folder.mkdir(mode=0o700)
        p=folder/'bridge.sqlite3';p.write_bytes(restored);p.chmod(0o600)
        with self.open_store(folder) as store:
            runtime=Runtime(store,KEY,type('Peer',(),{'ready':False})())
            self.assertEqual(runtime.scene_engine._require(self.scene['id']),self.scene)
            self.assertEqual(runtime.scene_engine._owner(),self.scene['owner'])
            self.assertFalse(runtime.scene_engine._leased(self.scene))
            view=status(store)
            self.assertEqual(view['admission_mode'],'backup_compatible_restore')
            self.assertFalse(view['historical_floor_known'])
            self.assertFalse(view['history_verified_by_bridge'])
            self.assertEqual(store.allocate_sequence(),124)
            self.assertEqual(store.bindings(),{})

    def open_store(self,folder):
        from contextlib import contextmanager
        @contextmanager
        def opened():
            store=Store(folder,KEY)
            try:yield store
            finally:store.close()
        return opened()

    def test_same_install_preserves_current_counter_history_and_new_reservations(self):
        old,_,_=self.read(self.original)
        inventory=copy.deepcopy(old['inventory']);inventory['devices'][0]['name']='New label'
        current=self.changed(metadata={'sequence':321,'inventory':inventory,'known_targets':[300,301,399],
            'retired_addresses':[49999],'retired_claims':['new-retirement'],
            'device_rooms':{'40001':'Changed'},'private_future_setting':{'credential':'newer-private-credential'}})
        restored,report=self.prepare(current_bytes=current)
        after,_,_=self.read(restored)
        self.assertEqual(after['sequence'],321)
        self.assertEqual(after['inventory'],old['inventory'])
        self.assertEqual(after['device_rooms'],old['device_rooms'])
        self.assertEqual(after['known_targets'],[300,301,399])
        self.assertEqual(after['retired_addresses'],[49999])
        self.assertEqual(after['retired_claims'],['new-retirement'])
        self.assertEqual(after['private_future_setting']['credential'],'newer-private-credential')
        self.assertTrue(report['current_state_preserved'])
        self.assertEqual(report['restored_sequence'],321)

    def test_repeated_old_backup_never_reduces_counter_or_removes_restore_audit(self):
        first,_=self.prepare()
        current=self.changed(first,metadata={'sequence':400})
        second,_=self.prepare(current_bytes=current)
        metadata,_,_=self.read(second)
        self.assertEqual(metadata['sequence'],400)
        self.assertEqual(len(metadata[RESTORE_HISTORY]),2)
        third,_=self.prepare(current_bytes=second)
        third_metadata,_,_=self.read(third)
        self.assertEqual(third_metadata['sequence'],400)
        self.assertEqual(third_metadata[RESTORE_HISTORY],metadata[RESTORE_HISTORY])
        self.assertEqual(third_metadata['tx_admission'],metadata['tx_admission'])

    def test_same_install_accepts_attempted_to_uncertain_only(self):
        current=self.changed(sql=lambda db:db.execute("UPDATE journals SET status='uncertain'"))
        restored,_=self.prepare(current_bytes=current)
        self.assertEqual(self.read(restored)[1][self.receipt][2],'uncertain')

    def test_new_or_resolved_operations_refuse_without_touching_inputs(self):
        for sql in (lambda db:db.execute("UPDATE journals SET status='verified'"),
                    lambda db:db.execute("INSERT INTO journals VALUES ('later','claim',40003,'uncertain','{}')"),
                    lambda db:db.execute('DELETE FROM journals')):
            with self.subTest(sql=sql):
                current=self.changed(sql=sql);saved=bytes(current)
                self.refuse('operation_history_changed',current_bytes=current)
                self.assertEqual(current,saved)

    def test_changed_physical_inventory_members_or_model_refuse(self):
        original=self.read(self.original)[0]['inventory']
        for variant in ('add','members','model','protected'):
            inventory=copy.deepcopy(original)
            if variant=='add':inventory['devices'].append(dict(device_id=40003,product_id=134,name='Later bulb',exposed=False))
            elif variant=='members':inventory['groups'][0]['members']=[]
            elif variant=='model':inventory['devices'][0]['product_id']=93
            else:inventory['groups'][0].update(protected=True,members=[],exposed=False)
            with self.subTest(variant=variant):self.refuse('physical_inventory_changed',current_bytes=self.changed(metadata={'inventory':inventory}))

    def test_changed_scene_presets_refuse_but_names_can_restore(self):
        row=copy.deepcopy(self.scene);row['members'][0]['brightness']=1
        self.refuse('physical_inventory_changed',current_bytes=self.changed(metadata={SCENES:[row]}))
        row=copy.deepcopy(self.scene);row['name']='New scene label';row['exposed']=False
        restored,_=self.prepare(current_bytes=self.changed(metadata={SCENES:[row]}))
        self.assertEqual(self.read(restored)[0][SCENES],[self.scene])

    def test_retired_or_superseded_candidate_and_current_are_never_reactivated(self):
        for key in ('tx_retirement','installation_superseded_v1'):
            for value in (None,False,{'retired':True}):
                with self.subTest(key=key,value=value):
                    raw=self.changed(metadata={key:value})
                    code='retired_transmitter' if key=='tx_retirement' else 'superseded_installation'
                    self.refuse(code,raw);self.refuse(code,current_bytes=raw)

    def test_missing_malformed_and_exhausted_admission_refuse(self):
        self.refuse('invalid_admission',self.changed(metadata={'tx_admission':None}))
        self.refuse('sequence_exhausted',self.changed(metadata={'sequence':0xffffff}))
        admission=copy.deepcopy(self.read(self.original)[0]['tx_admission']);admission['id']='0'*64
        self.refuse('invalid_admission',self.changed(metadata={'tx_admission':admission}))
        self.refuse('invalid_database',self.changed(metadata={'sequence':True}))

    def test_different_current_mesh_or_lineage_refuse(self):
        before=self.read(self.original)[0]
        for key,code,value in [('mesh_fingerprint','different_mesh','1'*64),('state_id','different_installation','2'*32)]:
            admission=copy.deepcopy(before['tx_admission']);admission['document'][key]=value;admission['id']=canonical_digest(admission['document'])
            self.refuse(code,current_bytes=self.changed(metadata={key:value,'tx_admission':admission}))

    def test_corrupt_truncated_or_untrusted_schema_refuse(self):
        for raw in (b'',b'not sqlite',self.original[:100],self.original[:100]+b'\x00'*(len(self.original)-100)):
            with self.subTest(size=len(raw)):self.refuse('invalid_database',raw)
        raw=self.changed(sql=lambda db:db.execute('CREATE TABLE extra(value TEXT)'))
        self.refuse('invalid_database',raw)
        raw=self.changed(sql=lambda db:db.execute('CREATE TRIGGER attack AFTER INSERT ON metadata BEGIN DELETE FROM journals; END'))
        self.refuse('invalid_database',raw)

    def test_invalid_reservations_and_retired_live_device_refuse(self):
        for value in ([True],[70000],[300,300],{}):
            self.refuse('invalid_reservations',self.changed(metadata={'known_targets':value}))
        self.refuse('retired_device_in_inventory',self.changed(metadata={'retired_addresses':[40001]}))

    def test_invalid_scene_content_and_restore_audit_refuse(self):
        row=copy.deepcopy(self.scene);row['members'][0]['brightness']=999
        self.refuse('invalid_scenes',self.changed(metadata={SCENES:[row]}))
        self.refuse('invalid_restore_history',self.changed(metadata={RESTORE_ADMISSION:{}}))

    def test_backup_identity_and_time_are_bounded(self):
        for digest,created in [('x',CREATED),(DIGEST,'not-a-date'),(DIGEST,'2026-01-01')]:
            with self.assertRaises(RestoreStateError):
                prepare_restored_database(self.original,backup_digest=digest,created_at=created)

    def test_original_cloud_import_policy_remains_preserved(self):
        metadata=self.read(self.original)[0]
        document={'format':1,'state_id':metadata['state_id'],'mesh_fingerprint':metadata['mesh_fingerprint'],
            'source':0x8000,'mode':'esp_compatible_import','fresh_mesh':False,'history_complete':False,
            'historical_high_water_mark':None,'location_id':'synthetic-location','inventory_digest':'3'*64}
        admission={'id':canonical_digest(document),'document':document,'history_verified_by_bridge':False}
        candidate=self.changed(metadata={'tx_admission':admission})
        restored,_=self.prepare(candidate)
        self.assertEqual(self.read(restored)[0]['tx_admission'],admission)

    def test_boolean_format_and_extra_admission_fields_are_rejected(self):
        for change in ({'format':True},{'unexpected':'field'}):
            admission=copy.deepcopy(self.read(self.original)[0]['tx_admission'])
            admission['document'].update(change);admission['id']=canonical_digest(admission['document'])
            self.refuse('invalid_admission',self.changed(metadata={'tx_admission':admission}))

    def test_preview_report_contains_no_credentials_or_private_identity(self):
        _,report=self.prepare();encoded=json.dumps(report);metadata=self.read(self.original)[0]
        for secret in ('synthetic-private-credential','synthetic-receipt-detail',metadata['state_id'],metadata['mesh_fingerprint']):
            self.assertNotIn(secret,encoded)


# Exercise real HA cache context after restoring bytes, not merely presence of a key.
import test_battery_mapping_recovery as battery
class RestoredBatteryMappingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=battery.BatteryMappingRecoveryTests.asyncSetUp
    asyncTearDown=battery.BatteryMappingRecoveryTests.asyncTearDown
    save=battery.BatteryMappingRecoveryTests.save
    sleep=battery.BatteryMappingRecoveryTests.sleep
    prepared=battery.BatteryMappingRecoveryTests.prepared
    restart=battery.BatteryMappingRecoveryTests.restart
    press=battery.BatteryMappingRecoveryTests.press
    activations=battery.BatteryMappingRecoveryTests.activations

    async def test_verified_ha_cache_routes_after_replacement_without_wake_or_programming(self):
        await self.prepared()
        self.store.close()
        path=Path(self.temp.name)/'bridge.sqlite3'
        restored,_=prepare_restored_database(path.read_bytes(),backup_digest=DIGEST,created_at=CREATED)
        path.write_bytes(restored)
        self.restart();self.press()
        self.assertEqual([e['button'] for e in self.activations()],[1])
        self.assertEqual(self.activations()[0]['button_mapping'],'cached_table')
        self.assertEqual(self.store.bindings(),{})
        self.transport.send.assert_not_awaited()
