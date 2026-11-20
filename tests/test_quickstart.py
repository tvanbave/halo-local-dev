"""Synthetic install/restart and authenticated setup checks; no real Bluetooth."""
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from avionmesh_pi.browser_auth import basic_header
from aiohttp.test_utils import TestClient, TestServer

from avionmesh_pi import quickstart as q
from avionmesh_pi.__main__ import main
from avionmesh_pi.migration import retire
from avionmesh_pi.protocol import derive_mcp_key
from avionmesh_pi.service import load_config, private_text
from avionmesh_pi.storage import Store
import test_http as http

HOST = '192.168.50.20'
PORT = 18089


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'data'
        self.lock = q.installation_lock(self.root)
        self.lock.__enter__()
        with redirect_stdout(io.StringIO()):
            self.token = q.password(self.root)
    def tearDown(self):
        self.lock.__exit__(None, None, None)
        self.temp.cleanup()
    def initialize(self):
        return q.initialize(self.root, HOST, PORT, 'hci0')
    def store(self):
        key = derive_mcp_key(private_text(self.root / 'bridge/mesh_passphrase'))
        return Store(self.root / 'bridge/state', key)
    def test_password_alone_does_not_create_mesh_or_transmit_history(self):
        self.assertEqual(set(p.name for p in self.root.iterdir()), {'startup.lock', 'ui-password'})
        self.assertGreaterEqual(len(self.token), 32)
        self.assertEqual((self.root/'ui-password').stat().st_mode & 0o777, 0o600)
    def test_restart_preserves_identity_sequence_inventory_and_settings(self):
        config = self.initialize()
        store = self.store()
        try:
            seq = store.allocate_sequence()
            state_id = store.get('state_id')
            store.import_inventory({'devices': [{'device_id': 32901, 'name': 'Synthetic', 'product_id': 40}], 'groups': []})
        finally: store.close()
        settings = self.root / 'bridge/state/mqtt-settings.json'
        settings.write_text('synthetic settings must survive');settings.chmod(0o600)
        key = (self.root/'bridge/mesh_passphrase').read_bytes()
        restarted = q.existing_config(self.root, '192.168.50.21', PORT, 'hci1')
        self.assertEqual(restarted['node'], config['node'])
        self.assertEqual(restarted['http_host'], '192.168.50.21')
        self.assertFalse(restarted['browser_login'])
        self.assertTrue(restarted['trusted_lan_no_auth'])
        self.assertEqual((self.root/'bridge/mesh_passphrase').read_bytes(), key)
        self.assertEqual(settings.read_text(), 'synthetic settings must survive')
        store = self.store()
        try:
            self.assertEqual(store.get('state_id'), state_id)
            self.assertEqual(store.get('sequence'), seq)
            self.assertEqual(len(store.inventory()['devices']), 1)
        finally: store.close()
    def test_explicit_backup_opt_out_survives_setup_and_restart(self):
        from avionmesh_pi.backup_manager import BackupManager
        with patch.dict('os.environ', {'HALO_TRUSTED_LAN_BACKUPS': 'false'}):
            config = self.initialize()
            self.assertFalse(config['trusted_lan_backups'])
        restarted = q.existing_config(self.root, HOST, PORT, 'hci0')
        for value, expected in (('', False), ('true', True), ('false', False), ('typo', False)):
            with self.subTest(value=value), patch.dict('os.environ', {'HALO_TRUSTED_LAN_BACKUPS': value}):
                self.assertEqual(BackupManager(config=restarted).trusted_lan_backups, expected)

    def test_new_install_enables_reviewed_removal_without_enabling_reset_recovery(self):
        config = self.initialize()
        self.assertTrue(config['capabilities']['removal'])
        self.assertFalse(config['capabilities']['claim_reset_recovery'])
        self.assertTrue(config['capabilities']['group_membership'])

    def test_existing_removal_setting_changes_only_with_explicit_override(self):
        self.initialize()
        path = self.root / 'bridge/service.json'
        config = load_config(path); config['capabilities']['removal'] = False
        path.write_text(json.dumps(config))
        key = (self.root / 'bridge/mesh_passphrase').read_bytes()
        database = (self.root / 'bridge/state/bridge.sqlite3').read_bytes()
        restarted = q.existing_config(self.root, HOST, PORT, 'hci0')
        self.assertFalse(restarted['capabilities']['removal'])
        enabled = q.existing_config(self.root, HOST, PORT, 'hci0', removal=True)
        self.assertTrue(enabled['capabilities']['removal'])
        self.assertTrue(q.existing_config(self.root, HOST, PORT, 'hci0')['capabilities']['removal'])
        self.assertEqual({k:v for k,v in enabled.items() if k != 'capabilities'},
                         {k:v for k,v in restarted.items() if k != 'capabilities'})
        self.assertEqual(enabled['capabilities'], dict(restarted['capabilities'], removal=True))
        self.assertFalse(q.existing_config(self.root, HOST, PORT, 'hci0', removal=False)['capabilities']['removal'])
        self.assertEqual((self.root / 'bridge/mesh_passphrase').read_bytes(), key)
        self.assertEqual((self.root / 'bridge/state/bridge.sqlite3').read_bytes(), database)

    def test_removal_override_rejects_invalid_values_without_changing_config(self):
        self.initialize(); path = self.root / 'bridge/service.json'; before = path.read_bytes()
        for value in ('true', 1, [], {}):
            with self.subTest(value=value), self.assertRaises(q.SetupError):
                q.existing_config(self.root, HOST, PORT, 'hci0', removal=value)
        self.assertEqual(path.read_bytes(), before)
        for value, expected in ((None,None), ('',None), ('true',True), ('false',False)):
            self.assertIs(q.removal_setting(value), expected)
        for value in ('1', 'yes', 'False', 'on'):
            with self.subTest(value=value), self.assertRaises(q.SetupError): q.removal_setting(value)

    def test_saved_login_changes_only_with_explicit_override_and_preserves_state(self):
        q.initialize(self.root, HOST, PORT, 'hci0', browser_login=True)
        files = ('ui-password', 'bridge/mesh_passphrase', 'bridge/installation.json', 'bridge/state/bridge.sqlite3')
        before = {name: (self.root / name).read_bytes() for name in files}
        self.assertTrue(q.existing_config(self.root, HOST, PORT, 'hci0')['browser_login'])
        disabled = q.existing_config(self.root, HOST, PORT, 'hci0', browser_login=False)
        self.assertFalse(disabled['browser_login']); self.assertTrue(disabled['trusted_lan_no_auth'])
        self.assertFalse(q.existing_config(self.root, HOST, PORT, 'hci0')['browser_login'])
        enabled = q.existing_config(self.root, HOST, PORT, 'hci0', browser_login=True)
        self.assertTrue(enabled['browser_login']); self.assertFalse(enabled['trusted_lan_no_auth'])
        self.assertEqual({name: (self.root / name).read_bytes() for name in files}, before)

    def test_login_override_rejects_invalid_values_without_changing_config(self):
        self.initialize(); path = self.root / 'bridge/service.json'; before = path.read_bytes()
        for value in ('true', 1, [], {}):
            with self.subTest(value=value), self.assertRaises(q.SetupError):
                q.existing_config(self.root, HOST, PORT, 'hci0', browser_login=value)
        self.assertEqual(path.read_bytes(), before)
        for value, expected in ((None,None), ('',None), ('true',True), ('false',False)):
            self.assertIs(q.browser_login_setting(value), expected)
        for value in ('1', 'yes', 'False', 'on'):
            with self.subTest(value=value), self.assertRaises(q.SetupError): q.browser_login_setting(value)

    def test_default_password_generation_does_not_print_optional_credential(self):
        with tempfile.TemporaryDirectory() as temp:
            with q.installation_lock(Path(temp) / 'data') as root, redirect_stdout(io.StringIO()) as output:
                value = q.password(root)
            self.assertNotIn(value, output.getvalue())
        with tempfile.TemporaryDirectory() as temp:
            with q.installation_lock(Path(temp) / 'data') as root, redirect_stdout(io.StringIO()) as output:
                value = q.password(root, show_login=True)
            self.assertIn('First-run login: username halo; password ' + value, output.getvalue())

    def test_duplicate_initialization_refuses_without_changing_key(self):
        self.initialize(); before=(self.root/'bridge/mesh_passphrase').read_bytes()
        with self.assertRaises(q.SetupError): self.initialize()
        self.assertEqual((self.root/'bridge/mesh_passphrase').read_bytes(), before)
    def test_missing_database_never_creates_replacement(self):
        self.initialize(); path=self.root/'bridge/state/bridge.sqlite3';path.unlink()
        with self.assertRaises(q.SetupError):q.existing_config(self.root,HOST,PORT,'hci0')
        self.assertFalse(path.exists())
    def test_missing_password_never_regenerates_for_existing_mesh(self):
        self.initialize();(self.root/'ui-password').unlink()
        with self.assertRaises(q.SetupError):q.password(self.root)
        self.assertFalse((self.root/'ui-password').exists())
    def test_changed_key_refuses_without_touching_database(self):
        self.initialize();path=self.root/'bridge/state/bridge.sqlite3';before=path.read_bytes()
        (self.root/'bridge/mesh_passphrase').write_text('different synthetic key')
        with self.assertRaises(q.SetupError):q.existing_config(self.root,HOST,PORT,'hci0')
        self.assertEqual(path.read_bytes(),before)
    def test_retired_or_exhausted_state_cannot_be_readmitted(self):
        self.initialize();store=self.store()
        try:retire(store)
        finally:store.close()
        with self.assertRaises(q.SetupError):q.existing_config(self.root,HOST,PORT,'hci0')
    def test_crash_before_publish_is_preserved_not_retried(self):
        with patch.object(q.os,'rename',side_effect=OSError('synthetic crash')):
            with self.assertRaises(OSError):self.initialize()
        key=(self.root/'.initializing/mesh_passphrase').read_bytes()
        with self.assertRaises(q.SetupError):self.initialize()
        self.assertEqual((self.root/'.initializing/mesh_passphrase').read_bytes(),key)
        self.assertFalse((self.root/'bridge').exists())
    def test_second_writer_and_symlink_are_refused(self):
        with self.assertRaises(q.SetupError), q.installation_lock(self.root):pass
        self.initialize();path=self.root/'bridge/installation.json';other=self.root/'copy';path.rename(other);path.symlink_to(other)
        with self.assertRaises(q.SetupError):q.existing_config(self.root,HOST,PORT,'hci0')
    def test_generated_config_validates_and_disallows_unauthenticated_combination(self):
        self.initialize();path=self.root/'bridge/service.json';config=load_config(path)
        self.assertFalse(config['browser_login'])
        self.assertTrue(config['trusted_lan_no_auth'])
        config['browser_login']=True;path.write_text(json.dumps(config))
        with self.assertRaises(ValueError):load_config(path)
    def test_healthcheck_uses_saved_access_mode_and_omits_login_when_disabled(self):
        self.initialize()
        for enabled in (False, True):
            q.existing_config(self.root, HOST, PORT, 'hci0', browser_login=enabled)
            with patch.object(q.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value.__enter__.return_value.status = 200
                q.healthcheck(self.root)
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual(request.get_header('Host'), HOST + ':' + str(PORT))
                self.assertEqual(request.get_header('Authorization'), basic_header('halo', self.token) if enabled else None)

    def test_no_live_opt_in_means_no_setup_io(self):
        with patch.object(q,'start',new_callable=AsyncMock) as start, redirect_stdout(io.StringIO()):
            self.assertEqual(main(['start','--data-directory',str(self.root)]),2)
            start.assert_not_called()


class AddressTests(unittest.TestCase):
    def test_default_route_metric_and_virtual_interface_filter(self):
        routes='Iface Destination Gateway Flags RefCnt Use Metric Mask\nwg0 00000000 0100000A 0003 0 0 1 00000000\neth0 00000000 010132C0 0003 0 0 100 00000000\nwlan0 00000000 010132C0 0003 0 0 600 00000000\n'
        with patch.object(q.Path,'read_text',return_value=routes),patch.object(q,'interface_address',return_value=HOST) as address,patch.object(q.sys,'platform','linux'):
            self.assertEqual(q.lan_address(),HOST);address.assert_called_once_with('eth0')
    def test_public_wildcard_loopback_and_hostname_are_not_accepted(self):
        for value in ('0.0.0.0','8.8.8.8','127.0.0.1','bridge.local','::1'):
            with self.subTest(value=value),self.assertRaises(q.SetupError):q.lan_address(value)
    def test_restart_reuses_recent_http_port_but_refuses_an_active_listener(self):
        with q.socket.socket() as listener:
            listener.setsockopt(q.socket.SOL_SOCKET, q.socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            address = listener.getsockname()
            listener.listen()
            with self.assertRaises(q.SetupError):
                q.check_bind(*address)
            with q.socket.create_connection(address, timeout=2) as client:
                conn, _ = listener.accept()
                conn.close()
                self.assertEqual(client.recv(1), b'')
        q.check_bind(*address)

    def test_occupied_port_is_reported_before_installation(self):
        with patch.object(q.socket,'socket') as sock:
            sock.return_value.__enter__.return_value.bind.side_effect=OSError('busy')
            with self.assertRaises(q.SetupError):q.check_bind(HOST,PORT)


class BluetoothCheckTests(unittest.IsolatedAsyncioTestCase):
    """The start-up check with a synthetic bus; no real D-Bus or Bluetooth."""
    def bus(self, error):
        class Bus:
            def __init__(self, **_): pass
            async def connect(self): raise error
            def disconnect(self): pass
            async def wait_for_disconnect(self): pass
        return patch('dbus_fast.aio.MessageBus', Bus)

    async def test_a_bus_that_hangs_up_names_the_missing_host_user(self):
        for error in (BrokenPipeError(32, 'Broken pipe'), ConnectionResetError(), EOFError()):
            with self.subTest(error=type(error).__name__), self.bus(error), patch.object(q.os, 'getuid', return_value=65532), \
                    patch.object(q.os, 'getgid', return_value=65532), self.assertRaises(q.SetupError) as raised:
                await q.check_bluetooth('hci0')
            text = str(raised.exception)
            self.assertIn('bridge user (ID 65532) does not exist on the host', text)
            self.assertIn('sudo groupadd --gid 65532 halo-local', text)
            self.assertIn('sudo useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin halo-local', text)

    async def test_other_failures_say_what_kind_they_were(self):
        for error, name in ((asyncio.TimeoutError(), 'TimeoutError'), (FileNotFoundError(2, 'No such file'), 'FileNotFoundError')):
            with self.subTest(error=name), self.bus(error), self.assertRaises(q.SetupError) as raised:
                await q.check_bluetooth('hci0')
            self.assertIn(f'Cannot access Bluetooth ({name}).', str(raised.exception))
            self.assertNotIn('useradd', str(raised.exception))


class SetupHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'data'
        self.lock=q.installation_lock(self.root);self.lock.__enter__()
        with redirect_stdout(io.StringIO()):self.token=q.password(self.root)
        self.ready=asyncio.Event();self.headers={'Host':HOST+':'+str(PORT),'Authorization':basic_header('halo',self.token)}
        self.client=TestClient(TestServer(q.setup_app(self.root,HOST,PORT,'hci0',self.token,self.ready,browser_login=True)))
        await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close();self.lock.__exit__(None,None,None);self.temp.cleanup()
    async def test_get_and_unauthenticated_post_never_create_mesh(self):
        response=await self.client.get('/',headers={'Host':HOST+':'+str(PORT)})
        self.assertEqual(response.status,401);self.assertIn('Basic',response.headers['WWW-Authenticate'])
        response=await self.client.post('/api/setup',json={'create_new_mesh':True},headers={'Host':HOST+':'+str(PORT)})
        self.assertEqual(response.status,401)
        response=await self.client.get('/',headers=self.headers)
        self.assertEqual(response.status,200);self.assertIn('Create a new mesh',await response.text())
        self.assertFalse((self.root/'bridge').exists())
    async def test_confirmation_required_and_duplicate_post_is_not_repeated(self):
        for value in ({},{'create_new_mesh':False},{'create_new_mesh':1}):
            response=await self.client.post('/api/setup',json=value,headers=self.headers)
            self.assertEqual(response.status,400)
        response=await self.client.post('/api/setup',json={'create_new_mesh':True},headers=self.headers)
        self.assertEqual(response.status,201);self.assertTrue(self.ready.is_set())
        key=(self.root/'bridge/mesh_passphrase').read_bytes()
        response=await self.client.post('/api/setup',json={'create_new_mesh':True},headers=self.headers)
        self.assertEqual(response.status,409);self.assertEqual((self.root/'bridge/mesh_passphrase').read_bytes(),key)
    async def test_wrong_host_origin_and_password_rejected(self):
        for update,status in (({'Host':'attacker.invalid'},403),({'Origin':'http://attacker.invalid'},403),({'Authorization':basic_header('halo','wrong')},401)):
            response=await self.client.post('/api/setup',json={'create_new_mesh':True},headers=dict(self.headers,**update))
            self.assertEqual(response.status,status)
        self.assertFalse((self.root/'bridge').exists())


class DefaultLanSetupHttpTests(SetupHttpTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.client.close()
        self.client=TestClient(TestServer(q.setup_app(self.root,HOST,PORT,'hci0',self.token,self.ready)))
        await self.client.start_server()
        self.headers={'Host':HOST+':'+str(PORT)}

    async def test_get_and_unauthenticated_post_never_create_mesh(self):
        # No login prompt; merely loading setup still cannot create a mesh.
        for path in ('/', '/setup.js', '/setup.css'):
            response=await self.client.get(path,headers=self.headers)
            self.assertEqual(response.status,200)
            self.assertNotIn('WWW-Authenticate',response.headers)
        self.assertFalse((self.root/'bridge').exists())
        response=await self.client.post('/api/setup',json={'create_new_mesh':True},headers=self.headers)
        self.assertEqual(response.status,201)
        config=load_config(self.root/'bridge/service.json')
        self.assertFalse(config['browser_login']);self.assertTrue(config['trusted_lan_no_auth'])

    async def test_wrong_host_origin_and_password_rejected(self):
        for update in ({'Host':'attacker.invalid'},{'Origin':'http://attacker.invalid'},{'Sec-Fetch-Site':'cross-site'}):
            response=await self.client.post('/api/setup',json={'create_new_mesh':True},headers=dict(self.headers,**update))
            self.assertEqual(response.status,403)
        response=await self.client.post('/api/setup',data='{}',headers=self.headers)
        self.assertEqual(response.status,415)
        self.assertFalse((self.root/'bridge').exists())


class BrowserLoginHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await http.HttpTests.asyncSetUp(self);await self.client.close()
        self.client=TestClient(TestServer(http.create_app(self.runtime,'x'*40,browser_login=True,allowed_host=HOST+':'+str(PORT))))
        await self.client.start_server()
        self.headers={'Host':HOST+':'+str(PORT),'Authorization':basic_header('halo','x'*40)}
    asyncTearDown=http.HttpTests.asyncTearDown
    async def test_root_assets_and_snapshot_require_login_and_work_with_basic(self):
        for path in ('/','/ui.js','/settings.js','/api/snapshot'):
            response=await self.client.get(path,headers={'Host':HOST+':'+str(PORT)})
            self.assertEqual(response.status,401,path)
            response=await self.client.get(path,headers=self.headers)
            self.assertEqual(response.status,200,path)
        response=await self.client.get('/',headers=self.headers)
        self.assertIn('<script src="/ui.js"></script>',await response.text())
        self.assertEqual(self.transport.frames,[])
    async def test_event_stream_authenticates_and_delivers_snapshot(self):
        response = await self.client.get('/api/events', headers=self.headers)
        self.assertEqual(response.status, 200)
        try:
            event = await asyncio.wait_for(response.content.readuntil(b'\n\n'), 2)
            self.assertIn(b'event: snapshot', event)
        finally:
            response.close()
        self.assertEqual(self.transport.frames, [])

    async def test_cookie_style_cross_site_requests_still_fail(self):
        response=await self.client.post('/api/control',json={},headers=dict(self.headers,Origin='http://attacker.invalid'))
        self.assertEqual(response.status,403);self.assertEqual(self.transport.frames,[])
    async def test_bearer_still_works_and_bad_basic_is_challenged(self):
        response=await self.client.get('/api/snapshot',headers=dict(self.headers,Authorization='Bearer '+'x'*40))
        self.assertEqual(response.status,200)
        for auth in ('Basic !!!',basic_header('other','x'*40)):
            response=await self.client.get('/',headers=dict(self.headers,Authorization=auth))
            self.assertEqual(response.status,401)

class StartFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_removal_environment_fails_before_bluetooth_or_setup(self):
        with patch.dict(q.os.environ, {'HALO_REMOVAL': 'yes'}), patch.object(q, 'check_bluetooth', new_callable=AsyncMock) as bluetooth:
            with self.assertRaises(q.SetupError): await q.start(Path('/unused'))
            bluetooth.assert_not_called()

    async def test_invalid_login_environment_fails_before_bluetooth_or_setup(self):
        with patch.dict(q.os.environ, {'HALO_BROWSER_LOGIN': 'yes'}), patch.object(q, 'check_bluetooth', new_callable=AsyncMock) as bluetooth:
            with self.assertRaises(q.SetupError): await q.start(Path('/unused'))
            bluetooth.assert_not_called()

    async def test_environment_can_disable_existing_login_then_reenable_same_password(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'data'
            with q.installation_lock(root), redirect_stdout(io.StringIO()):
                q.password(root); q.initialize(root, HOST, PORT, 'hci0', browser_login=True)
            credential = (root / 'ui-password').read_bytes()
            for value, expected in (('false', False), ('true', True)):
                with patch.dict(q.os.environ, {'HALO_BROWSER_LOGIN': value}), patch.object(q, 'lan_address', return_value=HOST), \
                        patch.object(q, 'check_bind'), patch.object(q, 'check_bluetooth', new_callable=AsyncMock), \
                        patch('avionmesh_pi.service.serve', new_callable=AsyncMock) as serve, redirect_stdout(io.StringIO()):
                    await q.start(root)
                self.assertIs(serve.call_args.args[0]['browser_login'], expected)
                self.assertIs(serve.call_args.args[0]['trusted_lan_no_auth'], not expected)
                self.assertEqual((root / 'ui-password').read_bytes(), credential)

    async def test_environment_explicitly_enables_existing_disabled_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'data'
            with q.installation_lock(root), redirect_stdout(io.StringIO()):
                q.password(root); q.initialize(root, HOST, PORT, 'hci0')
            path = root / 'bridge/service.json'; config = load_config(path)
            config['capabilities']['removal'] = False; path.write_text(json.dumps(config))
            with patch.dict(q.os.environ, {'HALO_REMOVAL': 'true'}), patch.object(q, 'lan_address', return_value=HOST), \
                    patch.object(q, 'check_bind'), patch.object(q, 'check_bluetooth', new_callable=AsyncMock), \
                    patch('avionmesh_pi.service.serve', new_callable=AsyncMock) as serve, redirect_stdout(io.StringIO()):
                await q.start(root)
            self.assertTrue(serve.call_args.args[0]['capabilities']['removal'])
            self.assertTrue(load_config(path)['capabilities']['removal'])

    async def test_confirmed_setup_transitions_to_service_with_saved_identity(self):
        from aiohttp import ClientSession, web
        ready, configured, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
        real_site = web.TCPSite
        seen = {}
        class LoopbackSite(real_site):
            def __init__(self, runner, host, port):
                super().__init__(runner, '127.0.0.1', 0)
            async def start(self):
                await super().start()
                seen['port'] = self._server.sockets[0].getsockname()[1]
                ready.set()
        async def fake_serve(config):
            seen['config'] = config
            configured.set()
            await stop.wait()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'data'
            with patch.object(q,'lan_address',return_value=HOST),patch.object(q,'check_bind'),patch.object(q,'check_bluetooth',new_callable=AsyncMock),patch.object(q.web,'TCPSite',LoopbackSite),patch('avionmesh_pi.service.serve',side_effect=fake_serve),redirect_stdout(io.StringIO()):
                task=asyncio.create_task(q.start(root))
                try:
                    await asyncio.wait_for(ready.wait(),2)
                    self.assertFalse((root/'bridge').exists())
                    headers={'Host':HOST+':'+str(PORT)}
                    async with ClientSession() as client:
                        async with client.post('http://127.0.0.1:'+str(seen['port'])+'/api/setup',json={'create_new_mesh':True},headers=headers) as response:
                            self.assertEqual(response.status,201)
                            self.assertEqual(await response.json(),{'created':True})
                    await asyncio.wait_for(configured.wait(),2)
                    self.assertFalse(seen['config']['browser_login'])
                    self.assertTrue(seen['config']['trusted_lan_no_auth'])
                    self.assertEqual(seen['config']['node'],json.loads((root/'bridge/installation.json').read_text())['node'])
                finally:
                    stop.set()
                    if not configured.is_set():task.cancel()
                    await asyncio.wait_for(task,3)
