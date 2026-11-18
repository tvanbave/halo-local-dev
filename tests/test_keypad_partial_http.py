"""Synthetic HTTP admission and cross-tab progress for read-only setup closure."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
import test_http as http
from avionmesh_pi.management import recovery_advice

RESULT = dict(partial_resolution=True, resolved=True, applied_slots=[1],
    unchanged_attempted_slots=[2], unattempted_slots=[3,4], assignment_read_back=True,
    write_retried=False, setup_automatically_resumed=False, exposure_changed=False,
    reservations_retained=True)

class PartialAdviceTests(unittest.TestCase):
    def test_only_uncertain_matching_slot_shape_offers_read_only_resolution(self):
        row = dict(kind='keypad_setup', status='uncertain', target=40001,
                   detail=dict(product_id=126,planned=[1,2,3,4],attempted=[1,2]))
        self.assertEqual(recovery_advice(row)['actions'], ['resolve_keypad_setup','reconcile'])
        for field,value in [('attempted',[]),('attempted',[True]),('attempted',[5]),
                            ('attempted',[1,1]),('planned',[3,4]),('product_id',81)]:
            bad = dict(row,detail=dict(row['detail'],**{field:value}))
            self.assertNotIn('resolve_keypad_setup',recovery_advice(bad)['actions'])
        row['status']='cancelled'
        self.assertEqual(recovery_advice(row)['actions'],[])

@unittest.skipIf(http.create_app is None, 'HTTP dependencies run in isolated container')
class PartialSetupHttpTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = http.HttpTests.asyncSetUp
    asyncTearDown = http.HttpTests.asyncTearDown

    def prepare(self, model=126):
        from avionmesh_pi.lifecycle import reload_inventory
        data=self.store.inventory();data['devices'][0]['product_id']=model
        self.store.import_inventory(data);reload_inventory(self.runtime)
        self.runtime.capabilities.update(battery_onboarding=True,wired_onboarding=True,inventory_complete=True)
        identifier=self.store.begin('keypad_setup',40001,dict(product_id=model,planned=[1,2,3,4],attempted=[1,2]))
        self.store.transition(identifier,'prepared','attempted');self.store.transition(identifier,'attempted','uncertain')
        return identifier

    async def post(self, identifier, headers=None):
        return await self.client.post('/api/keypad/resolve-setup',headers=self.headers if headers is None else headers,
            json={'transaction':identifier})

    async def test_resolution_job_owns_receipt_and_phase_across_observers(self):
        from avionmesh_pi.server import KEYPAD_SERVICE_KEY
        identifier=self.prepare();keypad=self.client.server.app[KEYPAD_SERVICE_KEY]
        entered,release=asyncio.Event(),asyncio.Event()
        async def resolve(receipt):
            self.assertEqual(receipt,identifier)
            keypad.progress[40001]='waiting_for_wake';entered.set();await release.wait()
            return RESULT
        with patch('avionmesh_pi.server.KeypadOnboarding.resolve_partial',side_effect=resolve) as resolved, \
                patch('avionmesh_pi.server.KeypadOnboarding.resume',new=AsyncMock()) as resumed:
            response=await self.post(identifier);self.assertEqual(response.status,202)
            accepted=await response.json();await asyncio.wait_for(entered.wait(),1)
            for _ in range(2):
                status=await (await self.client.get('/api/status',headers=self.headers)).json()
                p=status['jobs']['40001']['progress']
                self.assertEqual(p,dict(phase='waiting_for_wake',job_id=accepted['job_id'],target=40001,
                    product_id=126,operation='keypad_recovery',instance=status['instance'],transaction=identifier))
            duplicate=await self.post(identifier);self.assertEqual(duplicate.status,409)
            release.set();await asyncio.sleep(0)
            status=await (await self.client.get('/api/status',headers=self.headers)).json()
            self.assertEqual(status['jobs']['40001']['result'],RESULT)
            self.assertIsNone(status['jobs']['40001']['progress'])
            resolved.assert_awaited_once_with(identifier);resumed.assert_not_awaited()
        self.assertEqual(self.transport.frames,[])

    async def test_refused_auth_disabled_inventory_offline_busy_and_other_pending(self):
        identifier=self.prepare()
        with patch('avionmesh_pi.server.KeypadOnboarding.resolve_partial',new=AsyncMock()) as resolve:
            self.assertEqual((await self.post(identifier,{})).status,401)
            self.runtime.capabilities['battery_onboarding']=False
            self.assertEqual((await self.post(identifier)).status,501)
            self.runtime.capabilities['battery_onboarding']=True
            self.runtime.capabilities['inventory_complete']=False
            self.assertEqual((await self.post(identifier)).status,409)
            self.runtime.capabilities['inventory_complete']=True
            self.runtime.mesh_ready=False;self.assertEqual((await self.post(identifier)).status,409)
            self.runtime.mesh_ready=True
            await self.runtime.operation.acquire()
            try:self.assertEqual((await self.post(identifier)).status,409)
            finally:self.runtime.operation.release()
            self.store.begin('keypad_edit',40002,{})
            self.assertEqual((await self.post(identifier)).status,409)
            resolve.assert_not_awaited()
        self.assertEqual(self.transport.frames,[])

    async def test_wrong_receipt_model_unknown_and_malformed_never_start(self):
        identifier=self.prepare()
        with patch('avionmesh_pi.server.KeypadOnboarding.resolve_partial',new=AsyncMock()) as resolve:
            for body in [None,[],{'transaction':4},{'transaction':identifier,'resume':True},{'transaction':'missing'}]:
                response=await self.client.post('/api/keypad/resolve-setup',headers=dict(self.headers, **{'Content-Type':'application/json'}),data=json.dumps(body))
                self.assertEqual(response.status,400)
            from avionmesh_pi.lifecycle import reload_inventory
            data=self.store.inventory();data['devices'][0]['product_id']=127
            self.store.set('inventory',data);reload_inventory(self.runtime)
            self.assertEqual((await self.post(identifier)).status,400)
            resolve.assert_not_awaited()

    async def test_wired_recovery_uses_wired_gate_and_same_read_only_service(self):
        identifier=self.prepare(127)
        self.runtime.capabilities['battery_onboarding']=False
        with patch('avionmesh_pi.server.KeypadOnboarding.resolve_partial',new=AsyncMock(return_value=RESULT)) as resolve:
            self.assertEqual((await self.post(identifier)).status,202)
            await asyncio.sleep(0);resolve.assert_awaited_once_with(identifier)
        self.assertEqual(self.transport.frames,[])
