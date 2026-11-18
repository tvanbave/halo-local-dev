import copy
import tempfile
import unittest
from avionmesh_pi.management import revision,edit_metadata,cancel_unattempted,recovery_advice
from support import synthetic_store
from test_service import KEY,INVENTORY

class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=synthetic_store(self.temp.name,KEY)
        self.store.import_inventory(INVENTORY)
    def tearDown(self):
        self.store.close();self.temp.cleanup()
    def test_metadata_revision_and_protected_guard_without_physical_writes(self):
        request={'operation':'update','revision':revision(self.store),'kind':'device',
                 'target':40002,'name':'Updated','exposed':False}
        result=edit_metadata(self.store,request)
        self.assertFalse(result['physical_memberships_changed'])
        with self.assertRaises(ValueError):edit_metadata(self.store,request)
        request.update(revision=revision(self.store),kind='group',target=292)
        with self.assertRaises(ValueError):edit_metadata(self.store,request)
    def test_group_ids_are_not_reused_after_forgetting_metadata(self):
        def create():return edit_metadata(self.store,{'operation':'create_group','revision':revision(self.store),'name':'New'})
        create();target=self.store.inventory()['groups'][-1]['group_id']
        edit_metadata(self.store,{'operation':'forget_empty_group','revision':revision(self.store),'target':target})
        create();self.assertNotEqual(self.store.inventory()['groups'][-1]['group_id'],target)
        self.assertIn(target,self.store.get('known_targets'))
    def test_prepared_cancel_keeps_reservations_and_never_erases_receipt(self):
        identifier=self.store.begin('keypad_setup',40001,{'stages':[],'attempted':[]})
        self.store.reserve_targets([24500])
        cancel_unattempted(self.store,identifier)
        self.assertEqual(self.store.journals()[0]['status'],'cancelled')
        self.assertIn(24500,self.store.get('known_targets'))
        with self.assertRaises(ValueError):cancel_unattempted(self.store,identifier)
    def test_attempted_and_inconsistent_prepared_receipts_cannot_cancel(self):
        identifier=self.store.begin('keypad_setup',40001,{'stages':['1/primary']})
        with self.assertRaises(ValueError):cancel_unattempted(self.store,identifier)
        self.store.transition(identifier,'prepared','attempted')
        with self.assertRaises(ValueError):cancel_unattempted(self.store,identifier)
    def test_unsupported_claim_and_membership_recovery_stays_guarded(self):
        for kind in ('claim','group_membership'):
            result=recovery_advice({'kind':kind,'status':'uncertain','detail':{},'target':40001})
            self.assertEqual(result['actions'],[])
        self.assertEqual(recovery_advice({'kind':'claim','status':'uncertain','target':40001,
            'detail':{'association_complete':True,'inventory_complete':True}})['actions'],['reconcile_claim'])
