"""Synthetic HTTP access-mode checks. Socket tests run only in offline containers."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_http as http
from avionmesh_pi.ui import HTML, management_html

HOST = '192.168.50.20:18089'


class ManagementHtmlTests(unittest.TestCase):
    def test_token_mode_stays_default_and_never_saves_credentials(self):
        page = management_html()
        self.assertEqual(page, HTML)
        self.assertIn('id="token" type="password"', page)
        self.assertIn('const authenticationRequired=true;', page)
        for storage in ('localStorage', 'sessionStorage', 'document.cookie'):
            self.assertNotIn(storage, page)

    def test_lan_page_has_no_management_password_control_and_autoloads(self):
        page = management_html(trusted_lan_no_auth=True)
        self.assertNotIn('id="token"', page)
        self.assertNotIn('Management token', page)
        self.assertIn('Trusted LAN access.', page)
        self.assertIn('Anyone who can reach this bridge on your LAN can control it.', page)
        self.assertIn('const authenticationRequired=false;', page)
        self.assertIn('if(!authenticationRequired)run(refresh);', page)
        self.assertIn('Find unpaired devices', page)


@unittest.skipIf(http.create_app is None, 'HTTP dependencies are tested in the container')
class TrustedLanConfigurationTests(unittest.TestCase):
    def test_no_token_requires_explicit_boolean_opt_in(self):
        for token in (None, '', 'short'):
            with self.subTest(token=token), self.assertRaises(ValueError):
                http.create_app(None, token, allowed_host=HOST)
        for flag in ('true', 1, None):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                http.create_app(None, None, trusted_lan_no_auth=flag, allowed_host=HOST)

    def test_lan_host_requires_canonical_rfc1918_ipv4_and_port(self):
        from avionmesh_pi.server import _trusted_lan_host
        for value in ('10.0.0.1:1', '172.16.0.1:65535', '172.31.255.254:8088', HOST):
            self.assertEqual(_trusted_lan_host(value), value)
        invalid = (None, '', '192.168.50.20', 'localhost:8088', 'bridge.local:8088',
                   '127.0.0.1:8088', '0.0.0.0:8088', '8.8.8.8:8088', '169.254.1.1:8088',
                   '100.64.1.1:8088', '172.15.0.1:8088', '172.32.0.1:8088',
                   '[fd00::1]:8088', '192.168.050.20:8088', '192.168.50.20:0',
                   '192.168.50.20:65536', '192.168.50.20:08088', '192.168.50.20:+8088',
                   '192.168.50.20:8088 ', 'http://192.168.50.20:8088', '192.168.50.20:8088/anything')
        for host in invalid:
            with self.subTest(host=host), self.assertRaises(ValueError):
                http.create_app(None, None, trusted_lan_no_auth=True, allowed_host=host)

    def test_read_only_preview_cannot_be_changed_into_lan_access(self):
        with self.assertRaises(ValueError):
            http.create_app(None, None, read_only=True, trusted_lan_no_auth=True, allowed_host=HOST)


@unittest.skipIf(http.create_app is None, 'HTTP dependencies are tested in the container')
class TrustedLanHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await http.HttpTests.asyncSetUp(self)
        await self.client.close()
        self.client = http.TestClient(http.TestServer(http.create_app(
            self.runtime, None, trusted_lan_no_auth=True, allowed_host=HOST)))
        await self.client.start_server()
        self.headers = {'Host': HOST}

    asyncTearDown = http.HttpTests.asyncTearDown

    async def test_browser_default_http_port_is_canonicalized(self):
        await self.client.close()
        self.client = http.TestClient(http.TestServer(http.create_app(
            self.runtime, None, trusted_lan_no_auth=True, allowed_host='192.168.50.20:80')))
        await self.client.start_server()
        response = await self.client.get('/api/status', headers={'Host':'192.168.50.20','Origin':'http://192.168.50.20'})
        self.assertEqual(response.status,200)

    async def test_user_clicked_link_opens_home_but_never_cross_site_api(self):
        headers=dict(self.headers, **{'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate',
                                     'Sec-Fetch-Dest':'document','Sec-Fetch-User':'?1'})
        response=await self.client.get('/',headers=headers)
        self.assertEqual(response.status,200)
        self.assertEqual(response.headers['X-Frame-Options'],'DENY')
        for change in ({'Sec-Fetch-Dest':'iframe'},{'Sec-Fetch-User':'?0'},
                       {'Origin':'http://attacker.invalid'},{'Host':'attacker.invalid'}):
            response=await self.client.get('/',headers=dict(headers,**change))
            self.assertEqual(response.status,403)
        for method,path in (('GET','/api/status'),('POST','/api/control'),('POST','/')):
            response=await self.client.request(method,path,headers=headers,json={})
            self.assertEqual(response.status,403)
        self.assertEqual(self.transport.frames,[])

    async def test_lan_home_and_status_without_credentials(self):
        with patch('avionmesh_pi.server.hmac.compare_digest', side_effect=AssertionError('No token access')):
            response = await self.client.get('/', headers=self.headers)
            self.assertEqual(response.status, 200)
            text = await response.text()
            self.assertNotIn('id="token"', text)
            self.assertNotIn(self.token, text)
            response = await self.client.get('/api/status', headers=self.headers)
            self.assertEqual(response.status, 200)
            self.assertIs((await response.json())['authentication_required'], False)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            response = await self.client.get('/api/snapshot', headers=self.headers)
            self.assertIs((await response.json())['status']['authentication_required'], False)
        self.assertEqual(self.transport.frames, [])

    async def test_any_supplied_token_is_neither_read_nor_embedded_in_lan_mode(self):
        await self.client.close()
        self.client = http.TestClient(http.TestServer(http.create_app(
            self.runtime, self.token, trusted_lan_no_auth=True, allowed_host=HOST)))
        await self.client.start_server()
        for path in ('/', '/api/status', '/api/snapshot'):
            response = await self.client.get(path, headers=dict(self.headers, Authorization='not-a-token'))
            self.assertEqual(response.status, 200)
            self.assertNotIn(self.token, await response.text())

    async def test_host_rebinding_rejected_for_html_get_post_and_unknown_routes(self):
        bad_hosts = ('attacker.invalid:18089', '192.168.50.21:18089', '192.168.50.20:8088',
                     '192.168.50.20', '127.0.0.1:18089', '192.168.50.20:018089')
        with patch.object(self.runtime, 'status', side_effect=AssertionError('Must not run')):
            for host in bad_hosts:
                for method, path in (('GET', '/'), ('GET', '/api/status'), ('GET', '/api/events'),
                                     ('POST', '/api/control'), ('GET', '/unknown')):
                    with self.subTest(host=host, method=method, path=path):
                        response = await self.client.request(method, path, headers={
                            'Host': host, 'X-Forwarded-Host': HOST, 'Content-Type': 'application/json'}, data='{}')
                        self.assertEqual(response.status, 403)
                        self.assertEqual((await response.json())['error'], 'invalid_host')
        self.assertEqual(self.transport.frames, [])

    async def test_cross_site_and_nonmatching_origins_rejected_before_any_handler(self):
        bad_headers = ({'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'http://foreign.invalid'},
                       {'Origin': 'https://' + HOST}, {'Origin': 'null'},
                       {'Origin': 'http://192.168.50.20:8088'}, {'Origin': 'http://' + HOST + '/'})
        with patch.object(self.runtime, 'status', side_effect=AssertionError('Must not run')):
            for added in bad_headers:
                for method, path in (('GET', '/'), ('GET', '/api/status'), ('GET', '/api/events'),
                                     ('POST', '/api/control'), ('GET', '/unknown')):
                    with self.subTest(headers=added, method=method, path=path):
                        response = await self.client.request(method, path,
                            headers=dict(self.headers, **added), json={})
                        self.assertEqual(response.status, 403)
        self.assertEqual(self.transport.frames, [])

    async def test_same_origin_is_allowed_without_auth_and_post_requires_json(self):
        headers = dict(self.headers, Origin='http://' + HOST, **{'Sec-Fetch-Site': 'same-origin'})
        response = await self.client.get('/api/status', headers=headers)
        self.assertEqual(response.status, 200)
        for content_type in ('text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data'):
            response = await self.client.post('/api/control',
                headers=dict(headers, **{'Content-Type': content_type}), data='{}')
            self.assertEqual(response.status, 415)
            self.assertEqual((await response.json())['error'], 'json_required')
        self.assertEqual(self.transport.frames, [])
        response = await self.client.post('/api/control', headers=headers,
            json={'target': 40002, 'values': {'brightness': 128}})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())['confirmed'])
        self.assertEqual(len(self.transport.frames), 2)

    async def test_error_guards_are_preserved_without_leaking_details(self):
        for failure, expected, code in ((ValueError('secret'), 'invalid_request', 400),
                                       (RuntimeError('secret'), 'operation_unavailable_or_uncertain', 409),
                                       (asyncio.TimeoutError('secret'), 'timeout_outcome_unknown', 504)):
            with patch.object(self.runtime, 'control', AsyncMock(side_effect=failure)):
                response = await self.client.post('/api/control', headers=self.headers,
                    json={'target': 40002, 'values': {'state': 'ON'}})
            self.assertEqual(response.status, code)
            self.assertEqual(await response.json(), {'error': expected})
        response = await self.client.post('/api/control',
            headers=dict(self.headers, **{'Content-Type': 'application/json'}), data='{')
        self.assertEqual(response.status, 400)
        self.assertEqual(self.transport.frames, [])

    async def test_manual_bootstrap_endpoints_are_disabled_in_automatic_mode(self):
        self.runtime.bootstrap = SimpleNamespace(automatic=True)
        self.runtime.capabilities['ordinary_claim'] = True
        for route in ('scan', 'select'):
            response = await self.client.post('/api/bootstrap/' + route, headers=self.headers, json={})
            self.assertEqual(response.status, 409)
            self.assertEqual(await response.json(), {'error': 'automatic_bootstrap'})
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.journals(), [])

    async def test_default_mode_still_requires_bearer_and_reports_it(self):
        await self.client.close()
        self.client = http.TestClient(http.TestServer(http.create_app(self.runtime, self.token)))
        await self.client.start_server()
        for path in ('/api/status', '/api/snapshot'):
            response = await self.client.get(path)
            self.assertEqual(response.status, 401)
        response = await self.client.get('/api/status', headers={'Authorization': 'Bearer ' + self.token})
        self.assertEqual(response.status, 200)
        self.assertIs((await response.json())['authentication_required'], True)
        response = await self.client.post('/api/control', headers={'Authorization': 'Bearer ' + self.token}, data='{}')
        self.assertEqual(response.status, 415)
