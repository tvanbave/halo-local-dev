"""Automatic bearer is transport only; the owner still chooses claim identity."""
import unittest
from types import SimpleNamespace
import test_claim_development as base
from avionmesh_pi.association import uuid_hash
from avionmesh_pi.service import automatic_bootstrap_mode,trusted_lan_mode

class AutomaticFirstClaimTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=base.ClaimDevelopmentTests.asyncSetUp
    asyncTearDown=base.ClaimDevelopmentTests.asyncTearDown

    def automatic(self):
        self.runtime.bootstrap=SimpleNamespace(automatic=True,selected_address='02:00:00:00:00:BE',needs_first_claim=True)

    async def test_user_selected_first_uuid_is_independent_of_automatic_bearer(self):
        self.automatic()
        await self.claims.discover()
        result=await self.claims.claim(base.IDENTITY,'First owner-selected RL4',93,inventory_complete=True)
        self.assertTrue(result['associated'])
        receipt,=self.store.journals()
        self.assertTrue(receipt['detail']['fresh_mesh_first_claim'])
        self.assertFalse(receipt['detail']['direct_bootstrap'])
        self.assertEqual(receipt['detail']['bootstrap_address'],':'.join(f'{x:02X}' for x in bytes.fromhex(base.IDENTITY)[:6][::-1]))
        self.assertEqual(self.transport.starts,[uuid_hash(bytes.fromhex(base.IDENTITY))])

    async def test_first_claim_reset_proof_keeps_target_identity_not_bearer_identity(self):
        self.automatic()
        self.transport.fail_hash=uuid_hash(bytes.fromhex(base.IDENTITY))
        await self.claims.discover()
        with self.assertRaises(RuntimeError):await self.claims.claim(base.IDENTITY,'First',93,inventory_complete=True)
        receipt,=self.store.journals()
        self.transport.ready=self.runtime.mesh_ready=True
        self.transport.fail_hash=None
        self.runtime.session()
        result=await self.claims.reconcile_owner_reset(receipt['id'],True)
        self.assertTrue(result['retired'])
        self.assertFalse(result['association_retried'])
        self.assertEqual(len(self.transport.starts),1)
        self.assertIn(receipt['target'],self.store.get('retired_addresses'))

    async def rejected_initial(self):
        from test_association import Peer
        self.automatic()
        self.transport.peers[uuid_hash(bytes.fromhex(base.IDENTITY))] = Peer(confirm_type=3)
        await self.claims.discover()
        with self.assertRaises(ValueError):
            await self.claims.claim(base.IDENTITY,'First',93,inventory_complete=True)
        receipt, = self.store.journals()
        self.transport.ready = self.runtime.mesh_ready = True
        self.runtime.session()
        return receipt

    async def test_pre_key_retirement_preserves_receipt_and_never_retries(self):
        receipt = await self.rejected_initial()
        starts = len(self.transport.starts)
        result = await self.claims.reconcile_pre_key(receipt['id'])
        self.assertTrue(result['pre_key_retired'])
        self.assertFalse(result['reset_verified'])
        self.assertFalse(result['association_retried'])
        self.assertEqual(len(self.transport.starts), starts)
        self.assertIn(receipt['target'],self.store.get('retired_addresses'))
        proof = [j for j in self.store.journals() if j['kind']=='claim_pre_key'][0]
        self.assertEqual(proof['detail']['original_receipt'],receipt)
        self.assertFalse(proof['detail']['owner_reset_declared'])
        self.assertFalse(self.store.inventory()['devices'])

    async def test_pre_key_requires_isolated_session_and_exact_early_receipt(self):
        import json
        receipt = await self.rejected_initial()
        self.runtime.claim_isolated.clear()
        with self.assertRaises(ValueError):await self.claims.reconcile_pre_key(receipt['id'])
        self.runtime.claim_isolated.add(receipt['id'])
        for key,value in [('submissions_attempted',True),('submissions_attempted',2),('submissions_accepted',0),('submissions_accepted',True),('submissions_accepted',None),('association_phase','public_key'),('association_complete',True)]:
            detail = dict(receipt['detail'], **{key:value})
            self.store.db.execute('UPDATE journals SET detail=? WHERE id=?',(json.dumps(detail),receipt['id']))
            with self.assertRaises(ValueError):await self.claims.reconcile_pre_key(receipt['id'])
        self.assertEqual(len(self.transport.starts),1)

    async def test_pre_key_rejects_observation_and_receipt_races(self):
        import json
        receipt = await self.rejected_initial()
        def changed(_):
            d = dict(receipt['detail'], submissions_attempted=2)
            self.store.db.execute('UPDATE journals SET detail=? WHERE id=?',(json.dumps(d),receipt['id']))
            return self.transport.ads
        self.transport.scan_rows = changed
        with self.assertRaises(ValueError):await self.claims.reconcile_pre_key(receipt['id'])
        self.assertNotIn(receipt['target'],self.store.get('retired_addresses',[]))

    async def test_pre_key_rejects_session_and_hash_conflict(self):
        receipt = await self.rejected_initial()
        self.store.set('unpaired_hash_conflicts',[uuid_hash(bytes.fromhex(base.IDENTITY))])
        result = await self.claims.reconcile_pre_key(receipt['id'])
        self.assertFalse(result['retired'])
        self.store.set('unpaired_hash_conflicts',[])
        def changed(_):
            self.runtime.session()
            return self.transport.ads
        self.transport.scan_rows = changed
        with self.assertRaises(RuntimeError):await self.claims.reconcile_pre_key(receipt['id'])

    async def test_pre_key_later_explicit_claim_uses_new_address(self):
        from test_association import Peer
        receipt = await self.rejected_initial()
        await self.claims.reconcile_pre_key(receipt['id'])
        self.assertEqual(len(self.transport.starts),1)
        self.transport.peers[uuid_hash(bytes.fromhex(base.IDENTITY))] = Peer(confirm_type=1)
        await self.claims.discover()
        result = await self.claims.claim(base.IDENTITY,'Owner retry',93,inventory_complete=True)
        self.assertGreater(result['device_id'],receipt['target'])
        self.assertTrue(result['associated'])

    async def test_pre_key_missing_observation_cannot_retire(self):
        receipt = await self.rejected_initial()
        self.transport.ads = []
        result = await self.claims.reconcile_pre_key(receipt['id'])
        self.assertFalse(result['retired'])
        self.assertEqual(self.store.journals()[0]['status'],'uncertain')

    async def test_automatic_mode_does_not_retroactively_authorize_ordinary_light_reset(self):
        await self.claims.discover()
        self.transport.fail_hash=uuid_hash(bytes.fromhex(base.IDENTITY))
        with self.assertRaises(RuntimeError):await self.claims.claim(base.IDENTITY,'Ordinary',93,inventory_complete=True)
        self.automatic()
        receipt,=self.store.journals()
        with self.assertRaises(ValueError):await self.claims.reconcile_owner_reset(receipt['id'],True)

class AutomaticModeConfigTests(unittest.TestCase):
    def test_automatic_requires_explicit_bootstrap_and_null_relay(self):
        config={'automatic_bootstrap':True,'first_device_bootstrap':True,'commissioning_only':True,'relay_address':None}
        self.assertTrue(automatic_bootstrap_mode(config))
        for change in ({'automatic_bootstrap':'true'},{'first_device_bootstrap':False},{'relay_address':'00:11:22:33:44:55'}):
            with self.assertRaises(ValueError):automatic_bootstrap_mode(dict(config,**change))

    def test_no_auth_requires_explicit_private_lan_opt_in(self):
        config={'trusted_lan_no_auth':True,'commissioning_only':True,'allow_lan_http':True,'http_host':'192.168.50.20'}
        self.assertTrue(trusted_lan_mode(config))
        self.assertTrue(trusted_lan_mode(dict(config, commissioning_only=False)))
        for change in ({'trusted_lan_no_auth':'true'},{'allow_lan_http':False},{'http_host':'0.0.0.0'},{'http_host':'8.8.8.8'},{'http_host':'127.0.0.1'}):
            with self.assertRaises(ValueError):trusted_lan_mode(dict(config,**change))

class StartupIsolationTests(unittest.TestCase):
    def test_only_disconnected_runtime_can_isolate_retained_receipt(self):
        from avionmesh_pi.runtime import Runtime
        from unittest.mock import MagicMock
        for ready, connected, expected in [(False,False,True),(True,False,False),(False,True,False)]:
            store = MagicMock()
            store.inventory.return_value = {'devices':[],'groups':[]}
            store.get.side_effect = lambda name, default=None: default
            store.journals.return_value = [{'id':'retained'}]
            transport = SimpleNamespace(ready=ready,client=SimpleNamespace(is_connected=connected))
            runtime = Runtime(store,bytes(16),transport)
            self.assertEqual('retained' in runtime.claim_isolated,expected)
