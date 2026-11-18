"""Reviewed cloud imports use synthetic responses and never send radio frames."""
import asyncio
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.cloud import CloudImport, CloudImportError
from avionmesh_pi.protocol import derive_mcp_key
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_cloud import FakeCloud, Response, DEVICES, GROUP, aiohttp
from test_service import FakeTransport


@unittest.skipIf(aiohttp is None, 'Cloud HTTP dependency exercised only with fake requests in container')
class CloudReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = derive_mcp_key('synthetic-cloud-mesh')
        self.store = Store(self.temp.name, self.key)
        self.transport = FakeTransport()
        self.runtime = Runtime(self.store, self.key, self.transport)
        self.now = 10
        self.http = FakeCloud({'sessions': {'credentials': {'auth_token': 'synthetic-token'}},
            'user/locations': {'locations': [{'pid': 'home', 'name': 'Synthetic home'}]},
            'locations/home': {'location': {'name': 'Synthetic home', 'passphrase': 'synthetic-cloud-mesh'}},
            'locations/home/abstract_devices': copy.deepcopy(DEVICES),
            'locations/home/groups': {'groups': [{'pid': 'group_a'}]},
            'groups/group_a': copy.deepcopy(GROUP)})
        self.importer = CloudImport(self.runtime, self.http, clock=lambda: self.now)
        self.original_identity = {k: self.store.get(k) for k in
            ('state_id', 'mesh_fingerprint', 'tx_admission', 'sequence')}

    async def asyncTearDown(self):
        self.assertEqual(self.transport.frames, [])
        self.assertFalse(self.runtime.operation.locked())
        self.assertEqual(self.runtime._foreground_requests, 0)
        for key, value in self.original_identity.items():
            self.assertEqual(self.store.get(key), value, key)
        self.store.close()
        self.temp.cleanup()

    async def preview(self, location_id=None):
        return await self.importer.preview('test@example.invalid', 'synthetic-password', location_id)

    def request(self, preview):
        return {k: preview[k] for k in ('preview_id', 'revision')}

    def add_second_location(self, matching=False):
        self.http.documents['user/locations']['locations'].append({'pid': 'other', 'name': 'Other home'})
        self.http.documents['locations/other'] = {'location': {'name': 'Other home',
            'passphrase': 'synthetic-cloud-mesh' if matching else 'synthetic-other-mesh'}}

    async def test_preview_is_read_only_and_apply_is_explicit_single_use(self):
        before = list(self.store.db.iterdump())
        result = await self.preview()
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(result['can_apply'])
        self.assertEqual(result['changes']['devices_added'][0]['device_id'], 40002)
        self.assertEqual(result['changes']['groups_added'][0]['group_id'], 291)
        self.assertEqual(before, list(self.store.db.iterdump()))
        calls = len(self.http.calls)
        applied = await self.importer.apply(self.request(result))
        self.assertTrue(applied['saved'])
        self.assertFalse(applied['physical_memberships_changed'])
        self.assertFalse(applied['new_devices_exposed'])
        self.assertEqual(len(self.http.calls), calls)  # apply has no cloud request
        self.assertEqual(len(self.store.inventory()['devices']), 1)
        with self.assertRaises(CloudImportError) as caught:
            await self.importer.apply(self.request(result))
        self.assertEqual(caught.exception.code, 'cloud_preview_unavailable')

    async def test_secrets_are_not_in_response_plan_or_durable_metadata(self):
        result = await self.preview()
        text = json.dumps(result) + json.dumps(self.importer._plan)
        text += ' '.join(value for value, in self.store.db.execute('SELECT value FROM metadata'))
        for secret in ('synthetic-token', 'synthetic-password', 'synthetic-cloud-mesh', 'test@example.invalid'):
            self.assertNotIn(secret, text)
        self.importer.cancel()
        self.assertIsNone(self.importer._plan)

    async def test_multiple_locations_require_explicit_selection_and_reauthentication(self):
        self.add_second_location()
        listing = await self.preview()
        self.assertEqual(listing['status'], 'select_location')
        self.assertIsNone(listing['preview_id'])
        self.assertFalse(listing['can_apply'])
        self.assertEqual([r['matching_mesh'] for r in listing['locations']], [True, False])
        self.assertFalse(any('/abstract_devices' in url for _, url, _ in self.http.calls))
        ready = await self.preview('home')
        self.assertEqual(ready['selected_location']['id'], 'home')
        self.assertTrue(ready['can_apply'])
        self.assertEqual(sum(url.endswith('/sessions') for _, url, _ in self.http.calls), 2)

    async def test_different_mesh_is_blocked_without_loading_or_adopting_inventory(self):
        self.http.documents['locations/home']['location']['passphrase'] = 'synthetic-other-mesh'
        result = await self.preview()
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['can_apply'])
        self.assertIsNone(self.importer._plan)
        self.assertFalse(any('/abstract_devices' in url for _, url, _ in self.http.calls))
        self.assertEqual(self.store.inventory()['devices'], [])

    async def test_existing_labels_exposure_and_members_are_preserved(self):
        self.store.import_inventory({'devices': [
            {'device_id': 40002, 'product_id': 134, 'name': 'My renamed lamp', 'exposed': True},
            {'device_id': 40003, 'product_id': 93, 'name': 'Local lamp', 'exposed': False}],
            'groups': [{'group_id': 291, 'name': 'My room', 'members': [40003], 'exposed': True, 'protected': False}]})
        result = await self.preview()
        self.assertEqual(result['changes']['devices_added'], [])
        self.assertEqual(result['changes']['members_added'], [{'group_id': 291, 'name': 'My room', 'members': [40002]}])
        await self.importer.apply(self.request(result))
        inv = self.store.inventory()
        self.assertEqual(inv['devices'][0]['name'], 'My renamed lamp')
        self.assertTrue(inv['devices'][0]['exposed'])
        self.assertEqual(inv['groups'][0]['name'], 'My room')
        self.assertTrue(inv['groups'][0]['exposed'])
        self.assertEqual(inv['groups'][0]['members'], [40002, 40003])

    async def test_reserved_protected_retired_and_identity_collisions_refuse_before_plan(self):
        for variant in ('reserved', 'protected', 'retired', 'identity'):
            with self.subTest(variant=variant):
                self.store.set('known_targets', [])
                self.store.set('retired_addresses', [])
                self.store.set('inventory', {'devices': [], 'groups': []})
                if variant == 'reserved':
                    self.store.reserve_targets([291])
                elif variant == 'retired':
                    self.store.set('retired_addresses', [40002])
                elif variant == 'protected':
                    self.store.set('inventory', {'devices': [], 'groups': [{'group_id': 291,
                        'name': 'HA event target', 'members': [], 'exposed': False, 'protected': True}]})
                else:
                    self.store.set('inventory', {'devices': [{'device_id': 40002, 'product_id': 93,
                        'name': 'Other model', 'exposed': False}], 'groups': []})
                before = list(self.store.db.iterdump())
                with self.assertRaises(CloudImportError) as caught:
                    await self.preview()
                self.assertEqual(caught.exception.code, 'cloud_inventory_conflict')
                self.assertIsNone(self.importer._plan)
                self.assertEqual(before, list(self.store.db.iterdump()))

    async def test_stale_inventory_and_new_reservation_invalidate_review(self):
        for key, value in (('device_rooms', {'40002': 'Theatre'}),
                           ('known_targets', [291]), ('retired_addresses', [40002])):
            # Room labels do not participate in the imported inventory and are preserved.
            result = await self.preview()
            self.store.set(key, value)
            if key == 'device_rooms':
                applied = await self.importer.apply(self.request(result))
                self.assertTrue(applied['saved'])
                self.assertEqual(self.store.get(key), value)
                self.store.set('inventory', {'devices': [], 'groups': []})
                self.store.set('known_targets', [])
            else:
                with self.assertRaises(CloudImportError) as caught:
                    await self.importer.apply(self.request(result))
                self.assertEqual(caught.exception.code, 'cloud_preview_stale')
                self.assertIsNone(self.importer._plan)
                self.store.set(key, [])

    async def test_expiry_cancel_and_service_restart_require_fresh_preview(self):
        result = await self.preview()
        self.now += 300
        with self.assertRaises(CloudImportError) as caught:
            await self.importer.apply(self.request(result))
        self.assertEqual(caught.exception.code, 'cloud_preview_expired')
        result = await self.preview()
        self.importer.cancel()
        with self.assertRaises(CloudImportError):
            await self.importer.apply(self.request(result))
        result = await self.preview()
        restarted = CloudImport(self.runtime, self.http)
        with self.assertRaises(CloudImportError):
            await restarted.apply(self.request(result))
        self.assertEqual(self.store.inventory()['devices'], [])

    async def test_pending_operation_prevents_cloud_login_and_apply(self):
        result = await self.preview()
        self.store.begin('keypad_edit', 40001, {})
        calls = len(self.http.calls)
        with self.assertRaises(CloudImportError) as caught:
            await self.importer.apply(self.request(result))
        self.assertEqual(caught.exception.code, 'cloud_pending_operation')
        with self.assertRaises(CloudImportError):
            await self.preview()
        self.assertEqual(len(self.http.calls), calls)
        self.assertEqual(self.store.inventory()['devices'], [])

    async def test_preview_does_not_hold_radio_priority_and_cancel_invalidates_inflight_plan(self):
        entered, release = asyncio.Event(), asyncio.Event()
        fetch = self.importer._fetch
        async def delayed(*args):
            entered.set()
            await release.wait()
            return await fetch(*args)
        with patch.object(self.importer, '_fetch', side_effect=delayed):
            task = asyncio.create_task(self.preview())
            await entered.wait()
            self.assertFalse(self.runtime.operation.locked())
            self.assertEqual(self.runtime._foreground_requests, 0)
            self.importer.cancel()
            release.set()
            with self.assertRaises(CloudImportError) as caught:
                await task
            self.assertEqual(caught.exception.code, 'cloud_cancelled')
        self.assertIsNone(self.importer._plan)

    async def test_inventory_change_during_cloud_fetch_refuses_preview(self):
        fetch = self.importer._fetch
        async def changed(*args):
            value = await fetch(*args)
            self.store.reserve_targets([291])
            return value
        with patch.object(self.importer, '_fetch', side_effect=changed):
            with self.assertRaises(CloudImportError) as caught:
                await self.preview()
        self.assertEqual(caught.exception.code, 'cloud_preview_stale')
        self.assertIsNone(self.importer._plan)

    async def test_busy_apply_never_cancels_current_operation_or_replays(self):
        result = await self.preview()
        async with self.runtime.operation:
            with self.assertRaises(CloudImportError) as caught:
                await self.importer.apply(self.request(result))
            self.assertEqual(caught.exception.code, 'cloud_busy')
            self.assertTrue(self.runtime.operation.locked())
        with self.assertRaises(CloudImportError):
            await self.importer.apply(self.request(result))
        self.assertEqual(self.store.inventory()['devices'], [])

    async def test_apply_waits_for_background_read_without_cancelling_it(self):
        result = await self.preview()
        entered, release = asyncio.Event(), asyncio.Event()
        async def background():
            async with self.runtime._background_read() as active:
                self.assertTrue(active)
                async with self.runtime.operation:
                    entered.set()
                    await release.wait()
        reading = asyncio.create_task(background())
        await entered.wait()
        applying = asyncio.create_task(self.importer.apply(self.request(result)))
        for _ in range(20):
            if self.runtime._foreground_waiters:
                break
            await asyncio.sleep(0)
        self.assertEqual(self.runtime._foreground_waiters, 1)
        self.assertFalse(reading.done())
        self.assertFalse(applying.done())
        release.set()
        await reading
        self.assertTrue((await applying)['saved'])

    async def test_response_limits_and_redirects_do_not_retain_a_plan(self):
        self.http.documents['sessions'] = Response({}, 302)
        with self.assertRaises(ValueError):
            await self.preview()
        self.assertIsNone(self.importer._plan)
        self.http.documents['sessions'] = {'credentials': {'auth_token': 'synthetic-token'}}
        for document in self.http.documents.values():
            document['padding'] = 'x' * (1024 * 1024)
        with self.assertRaises(ValueError):
            await self.preview()
        self.assertIsNone(self.importer._plan)

    async def test_invalid_payload_or_unknown_location_cannot_import(self):
        for payload in ({}, {'preview_id': 12, 'revision': 'x'},
                        {'preview_id': 'x', 'revision': 'x', 'inventory': {}}):
            with self.assertRaises(CloudImportError):
                await self.importer.apply(payload)
        with self.assertRaises(CloudImportError) as caught:
            await self.preview('missing')
        self.assertEqual(caught.exception.code, 'cloud_location_unavailable')
        self.assertIsNone(self.importer._plan)

    async def test_transmitter_retirement_and_changed_admission_prevent_apply(self):
        result = await self.preview()
        admission = self.store.get('tx_admission')
        changed = copy.deepcopy(admission)
        changed['id'] = 'synthetic-new-admission'
        self.store.set('tx_admission', changed)
        try:
            with self.assertRaises(CloudImportError) as caught:
                await self.importer.apply(self.request(result))
            self.assertEqual(caught.exception.code, 'cloud_preview_stale')
        finally:
            self.store.set('tx_admission', admission)
        result = await self.preview()
        self.store.set('tx_retirement', {'synthetic': True})
        with self.assertRaises(CloudImportError) as caught:
            await self.importer.apply(self.request(result))
        self.assertEqual(caught.exception.code, 'cloud_not_admitted')
        self.assertEqual(self.store.inventory()['devices'], [])

    async def test_preview_expiring_during_background_admission_does_not_import(self):
        result = await self.preview()
        entered, release = asyncio.Event(), asyncio.Event()
        async def background():
            async with self.runtime._background_read() as active:
                self.assertTrue(active)
                async with self.runtime.operation:
                    entered.set()
                    await release.wait()
        reading = asyncio.create_task(background())
        await entered.wait()
        applying = asyncio.create_task(self.importer.apply(self.request(result)))
        for _ in range(20):
            if self.runtime._foreground_waiters:
                break
            await asyncio.sleep(0)
        self.assertEqual(self.runtime._foreground_waiters, 1)
        self.now += 300
        release.set()
        await reading
        with self.assertRaises(CloudImportError) as caught:
            await applying
        self.assertEqual(caught.exception.code, 'cloud_preview_expired')
        self.assertEqual(self.store.inventory()['devices'], [])

    async def test_concurrent_preview_is_refused_without_extra_authentication(self):
        entered, release = asyncio.Event(), asyncio.Event()
        fetch = self.importer._fetch
        async def delayed(*args):
            entered.set()
            await release.wait()
            return await fetch(*args)
        with patch.object(self.importer, '_fetch', side_effect=delayed):
            task = asyncio.create_task(self.preview())
            await entered.wait()
            with self.assertRaises(CloudImportError) as caught:
                await self.preview()
            self.assertEqual(caught.exception.code, 'cloud_busy')
            self.assertEqual(self.http.calls, [])
            release.set()
            self.assertTrue((await task)['can_apply'])
        self.assertEqual(sum(url.endswith('/sessions') for _, url, _ in self.http.calls), 1)

    async def test_malformed_login_shape_is_a_sanitizable_value_error_without_plan(self):
        for credentials in (None, [], 'synthetic-provider-content', 23, True):
            with self.subTest(credentials_type=type(credentials).__name__):
                self.http.documents['sessions'] = {'credentials': credentials}
                before = len(self.http.calls)
                with self.assertRaisesRegex(ValueError, '^Invalid cloud credentials document$'):
                    await self.preview()
                self.assertEqual(len(self.http.calls), before + 1)
                self.assertIsNone(self.importer._plan)
                self.assertEqual(self.store.inventory()['devices'], [])
