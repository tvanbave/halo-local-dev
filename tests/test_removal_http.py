"""Removal admission and owner-assisted import confirmation HTTP contracts."""
import unittest
from unittest.mock import AsyncMock, patch
from test_http import HttpTests, create_app


@unittest.skipIf(create_app is None, "HTTP dependencies are tested in the container")
class RemovalHttpTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = HttpTests.asyncSetUp
    asyncTearDown = HttpTests.asyncTearDown

    def review(self):
        return {'target':40002,'eligible':True,'enabled':True,'revision':'a'*64,'blockers':[]}

    async def test_missing_pairing_identity_is_actionable_without_dispatch(self):
        response = await self.client.post('/api/unclaim/preview',headers=self.headers,json={'target':40002})
        body = await response.json()
        self.assertEqual(response.status,409)
        self.assertEqual(body['error'],'removal_identity_unavailable')
        self.assertFalse(body['operation_started'])
        self.assertIn('cloud-import record',body['message'])
        self.assertEqual(self.transport.frames,[])
        self.assertEqual(self.store.journals(),[])

    async def test_preview_redacts_internal_errors(self):
        with patch('avionmesh_pi.lifecycle.Lifecycle.preview_remove',side_effect=ValueError('private synthetic secret')):
            response = await self.client.post('/api/unclaim/preview',headers=self.headers,json={'target':40002})
        self.assertNotIn('private synthetic secret',await response.text())
        self.assertFalse((await response.json())['operation_started'])

    async def test_disabled_unknown_changed_and_blocked_removal_are_explicit(self):
        cases=[('removal_disabled',False,40002,self.review()),
               ('removal_target_unknown',True,49999,self.review()),
               ('removal_review_changed',True,40002,{**self.review(),'revision':'b'*64}),
               ('removal_active_relay',True,40002,{**self.review(),'blockers':['active_relay_requires_reconnection']}),
               ('removal_native_dependency',True,40002,{**self.review(),'blockers':['native_controller_targets_device']})]
        for code,cap,target,review in cases:
            self.runtime.capabilities['removal']=cap
            with patch('avionmesh_pi.lifecycle.Lifecycle.preview_remove',return_value=review), patch('avionmesh_pi.lifecycle.Lifecycle.remove_and_verify',new=AsyncMock()) as remove:
                response=await self.client.post('/api/unclaim',headers=self.headers,json={'target':target,'revision':'a'*64})
                self.assertEqual(response.status,409)
                body=await response.json()
                self.assertEqual(body['error'],code)
                self.assertFalse(body['operation_started'])
                remove.assert_not_awaited()
        self.assertEqual(self.transport.frames,[])

    async def test_failure_after_lifecycle_starts_is_never_reported_not_started(self):
        self.runtime.capabilities['removal']=True
        with patch('avionmesh_pi.lifecycle.Lifecycle.preview_remove',return_value=self.review()), patch('avionmesh_pi.lifecycle.Lifecycle.remove_and_verify',new=AsyncMock(side_effect=ValueError('post-dispatch error'))):
            response=await self.client.post('/api/unclaim',headers=self.headers,json={'target':40002,'revision':'a'*64})
        self.assertEqual(response.status,400)
        self.assertNotEqual((await response.json()).get('operation_started'),False)

    async def test_import_confirmation_failure_is_actionable_and_retains_review(self):
        with patch('avionmesh_pi.lifecycle.Lifecycle.confirm_imported_removal',new=AsyncMock(side_effect=ValueError('private synthetic detail')),create=True):
            response=await self.client.post('/api/unclaim/confirm',headers=self.headers,json={
                'transaction':'imported-removal','uuid':'01'*16,'owner_confirmed':True})
        body=await response.json()
        self.assertEqual(response.status,409)
        self.assertEqual(body['error'],'removal_confirmation_unavailable')
        self.assertTrue(body['requires_review'])
        self.assertNotEqual(body.get('operation_started'),False)
        self.assertIn('Check what happened',body['message'])
        self.assertNotIn('private synthetic detail',body['message'])
        self.assertEqual(self.transport.frames,[])

    async def test_import_confirmation_requires_explicit_boolean_and_exact_fields(self):
        data={'transaction':'imported-removal','uuid':'01'*16,'owner_confirmed':True}
        with patch('avionmesh_pi.lifecycle.Lifecycle.confirm_imported_removal',new=AsyncMock(return_value={'removed':True,'verification_mode':'owner_confirmed_unpaired'}),create=True) as confirm:
            for invalid in ({**data,'owner_confirmed':False},{**data,'owner_confirmed':1},{**data,'reset':True},{'transaction':'imported-removal','uuid':'01'*16}):
                response=await self.client.post('/api/unclaim/confirm',headers=self.headers,json=invalid)
                self.assertEqual(response.status,400)
            confirm.assert_not_awaited()
            response=await self.client.post('/api/unclaim/confirm',headers=self.headers,json=data)
            self.assertEqual(response.status,200)
            confirm.assert_awaited_once_with('imported-removal','01'*16,True)
        self.assertEqual(self.transport.frames,[])


del HttpTests
