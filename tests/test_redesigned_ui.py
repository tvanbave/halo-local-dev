"""Redesigned trusted-LAN home page, classic fallback, capabilities and room labels.

Synthetic HTTP loopback only. Room labels are bridge-side metadata: no radio frames,
no inventory/mesh membership change.
"""
import re
import unittest
from pathlib import Path

import avionmesh_pi
import test_http as http
from avionmesh_pi.ui import HTML, management_html

HOST = '192.168.50.20:18089'
ROOT = Path(__file__).resolve().parents[1]
# The imported package's own assets: the source tree in development, site-packages in the image.
WEB = Path(avionmesh_pi.__file__).resolve().parent / 'web'


class RedesignedAssetTests(unittest.TestCase):
    def test_assets_are_packaged_and_allowed_into_the_image(self):
        # In the installed image this proves the wheel carried them; in a checkout it
        # also checks the declarations that make that happen.
        source_checkout = (ROOT / 'pyproject.toml').is_file()
        for name in ('ui.html', 'ui.css', 'ui.js', 'ops.js'):
            self.assertTrue((WEB / name).is_file(), name)
            if source_checkout:
                self.assertIn('web/' + name, (ROOT / 'pyproject.toml').read_text())
                self.assertIn('!avionmesh_pi/web/' + name, (ROOT / '.dockerignore').read_text())

    def test_page_uses_only_same_origin_assets(self):
        page = (WEB / 'ui.html').read_text()
        self.assertIn('<script src="/ui.js"></script>', page)
        self.assertIn('href="/ui.css"', page)
        self.assertIn('<script src="/ops.js"></script>', page)
        # Nothing in the redesigned page sends the owner to the previous interface.
        for name in ('ui.html', 'ui.js', 'ops.js', 'settings.js'):
            self.assertNotIn('classic', (WEB / name).read_text().lower(), name)
        self.assertNotRegex(page + (WEB / 'ui.css').read_text(), r'https?://')

    def test_script_never_resends_a_claim_and_uses_live_api(self):
        script = (WEB / 'ui.js').read_text()
        claims = [line for line in script.splitlines() if "'/api/claim'" in line]
        self.assertEqual(len(claims), 1)
        self.assertNotIn('retryBusy', claims[0])
        self.assertIn('setup_ha: false', claims[0])
        # Busy retries exist only for idempotent light commands, reads and searches.
        retried = sorted(set(re.findall(r"api\('(/api/[a-z/-]+)'[^\n]*retryBusy", script)))
        self.assertEqual(retried, ['/api/control', '/api/device/read', '/api/discover'])
        for removed in ("post('", '/health', 'claim_device', 'scan_unassociated', 'set_labels'):
            self.assertNotIn(removed, script)
        for storage in ('sessionStorage', 'document.cookie'):
            self.assertNotIn(storage, script)


@unittest.skipIf(http.create_app is None, 'HTTP dependencies are tested in the container')
class RedesignedHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await http.HttpTests.asyncSetUp(self)
        await self.client.close()
        self.client = http.TestClient(http.TestServer(http.create_app(
            self.runtime, None, trusted_lan_no_auth=True, allowed_host=HOST)))
        await self.client.start_server()
        self.headers = {'Host': HOST}

    asyncTearDown = http.HttpTests.asyncTearDown

    async def test_home_is_redesigned_and_classic_remains(self):
        response = await self.client.get('/', headers=self.headers)
        self.assertEqual(response.status, 200)
        text = await response.text()
        self.assertIn('<script src="/ui.js"></script>', text)
        self.assertNotIn('id="token"', text)
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        response = await self.client.get('/classic', headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), management_html(trusted_lan_no_auth=True))
        for path, kind in (('/ui.js', 'text/javascript'), ('/ops.js', 'text/javascript'), ('/ui.css', 'text/css')):
            response = await self.client.get(path, headers=self.headers)
            self.assertEqual(response.status, 200, path)
            self.assertEqual(response.content_type, kind)
            self.assertEqual(await response.read(), (WEB / path[1:]).read_bytes())
        self.assertEqual(self.transport.frames, [])

    async def test_assets_keep_host_and_cross_site_guards(self):
        for path in ('/ui.js', '/classic', '/api/capabilities'):
            response = await self.client.get(path, headers={'Host': 'attacker.invalid'})
            self.assertEqual(response.status, 403, path)
            response = await self.client.get(path, headers=dict(self.headers, **{'Sec-Fetch-Site': 'cross-site'}))
            self.assertEqual(response.status, 403, path)
        response = await self.client.post('/api/device-rooms', headers=dict(self.headers, Origin='http://attacker.invalid'),
                                          json={'rooms': {'40002': 'Hall'}})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.store.get('device_rooms'), None)

    async def test_capabilities_reflect_runtime(self):
        self.runtime.capabilities['ordinary_claim'] = True
        response = await self.client.get('/api/capabilities', headers=self.headers)
        data = await response.json()
        self.assertEqual(data['bridge'], 'pi-avionmesh')
        self.assertEqual(data['features']['pairable_types'], [93, 134, 127, 126, 91, 81])
        for off in ('group_edit', 'keypad_edit', 'remove', 'backup', 'all_lights'):
            self.assertIs(data['features'][off], False)
        self.assertIn('93', data['types']['lights'])
        self.assertIn('127', data['types']['controllers'])
        self.runtime.capabilities['ordinary_claim'] = False
        data = await (await self.client.get('/api/capabilities', headers=self.headers)).json()
        self.assertIs(data['features']['pair_lights'], False)
        # The removal row follows the same capability that gates the removal endpoint.
        self.runtime.capabilities['removal'] = True
        data = await (await self.client.get('/api/capabilities', headers=self.headers)).json()
        self.assertIs(data['features']['remove'], True)
        self.assertIs(data['features']['backup'], False)

    async def test_group_capabilities_and_snapshot_keep_physical_gates(self):
        for write, readback in ((False, False), (True, False), (False, True), (True, True)):
            self.runtime.capabilities.update(group_membership=write, group_membership_readback=readback)
            data = await (await self.client.get('/api/capabilities', headers=self.headers)).json()
            self.assertTrue(data['features']['group_metadata'])
            self.assertEqual(data['features']['group_edit'], write and readback)
            snapshot = await (await self.client.get('/api/snapshot', headers=self.headers)).json()
            self.assertEqual(snapshot['group_workflow']['programming_enabled'], write and readback)
        self.assertEqual(self.transport.frames, [])

    async def test_group_creation_returns_identity_without_programming(self):
        snapshot = await (await self.client.get('/api/snapshot', headers=self.headers)).json()
        response = await self.client.post('/api/metadata', headers=self.headers, json={
            'operation': 'create_group', 'revision': snapshot['status']['inventory_revision'], 'name': 'Theatre'})
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertFalse(result['physical_memberships_changed'])
        group = next(g for g in self.store.inventory()['groups'] if g['group_id'] == result['group_id'])
        self.assertEqual(group['name'], 'Theatre')
        self.assertEqual(group['members'], [])
        self.assertEqual(self.transport.frames, [])

    async def test_room_labels_are_bridge_metadata_only(self):
        revision = (await (await self.client.get('/api/status', headers=self.headers)).json())['inventory_revision']
        inventory = self.store.inventory()
        response = await self.client.post('/api/device-rooms', headers=self.headers,
                                          json={'rooms': {'40002': '  Basement   theatre ', '40001': 'Hall'}})
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data['device_rooms'], {'40002': 'Basement theatre', '40001': 'Hall'})
        self.assertEqual(data['radio_commands'], 0)
        snapshot = await (await self.client.get('/api/snapshot', headers=self.headers)).json()
        self.assertEqual(snapshot['device_rooms'], {'40002': 'Basement theatre', '40001': 'Hall'})
        self.assertEqual(snapshot['status']['inventory_revision'], revision)
        self.assertEqual(self.store.inventory(), inventory)
        response = await self.client.post('/api/device-rooms', headers=self.headers, json={'rooms': {'40001': ''}})
        self.assertEqual((await response.json())['device_rooms'], {'40002': 'Basement theatre'})
        self.assertEqual(self.transport.frames, [])

    async def test_room_label_validation_fails_closed(self):
        self.store.set('device_rooms', {'40002': 'Hall'})
        for body in ({'rooms': {'49999': 'Hall'}}, {'rooms': {'40002': 'x' * 41}}, {'rooms': {'40002': 'a\u0000b'}},
                     {'rooms': {'40002': 7}}, {'rooms': {}}, {'rooms': []}, {'rooms': {'40002': 'Hall'}, 'extra': 1}, []):
            with self.subTest(body=body):
                response = await self.client.post('/api/device-rooms', headers=self.headers, json=body)
                self.assertEqual(response.status, 400)
                self.assertEqual(self.store.get('device_rooms'), {'40002': 'Hall'})

    async def test_labels_for_removed_devices_are_not_reported(self):
        self.store.set('device_rooms', {'40002': 'Hall', '45555': 'Old'})
        snapshot = await (await self.client.get('/api/snapshot', headers=self.headers)).json()
        self.assertEqual(snapshot['device_rooms'], {'40002': 'Hall'})


@unittest.skipIf(http.create_app is None, 'HTTP dependencies are tested in the container')
class TokenModeKeepsClassicTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = http.HttpTests.asyncSetUp
    asyncTearDown = http.HttpTests.asyncTearDown

    async def test_token_mode_home_is_unchanged_and_assets_need_auth(self):
        response = await self.client.get('/')
        self.assertEqual(await response.text(), HTML)
        response = await self.client.get('/ui.js')
        self.assertEqual(response.status, 401)
        response = await self.client.get('/ui.js', headers=self.headers)
        self.assertEqual(response.status, 404)
        response = await self.client.post('/api/device-rooms', json={'rooms': {'40002': 'Hall'}})
        self.assertEqual(response.status, 401)
