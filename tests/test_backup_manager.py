"""Complete backup/restore orchestration on private synthetic installations only."""
import asyncio
import base64
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from avionmesh_pi import backup_manager as b, quickstart as q
from avionmesh_pi.installation_backup import decode_backup, encode_backup, InstallationBackupError
from avionmesh_pi.mqtt_settings import MqttSettings
from avionmesh_pi.protocol import derive_mcp_key, model_command
from avionmesh_pi.restore_state import RestoreStateError
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.service import load_config, private_text
from avionmesh_pi.storage import Store
from test_service import FakeTransport, INVENTORY

PASSWORD = 'synthetic backup password 2026'
HOST = '192.168.50.20'
PORT = 18089


def metadata(path):
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as db:
        return {k:json.loads(v) for k,v in db.execute('SELECT key,value FROM metadata')}


class SyntheticInstallation:
    def __init__(self, root):
        self.root = root
        self.lock = q.installation_lock(root); self.lock.__enter__()
        with redirect_stdout(io.StringIO()): self.token = q.password(root)
        self.config = q.initialize(root, HOST, PORT, 'hci0')
        self.key = derive_mcp_key(private_text(self.config['mesh_passphrase_file']))
        self.store = Store(self.config['state_directory'], self.key)
        self.store.import_inventory(copy.deepcopy(INVENTORY))
        self.store.set('device_rooms', {'40001':'Synthetic room','40002':'Synthetic room'})
        self.transport = FakeTransport()
        self.runtime = Runtime(self.store, self.key, self.transport, node=self.config['node'])
        self.runtime.capabilities = self.config['capabilities'].copy()
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.transport.receiver = self.runtime.observe
        self.runtime.session()
        self.settings = MqttSettings(self.runtime, self.config['state_directory'],
            initial_config={'host':'broker.invalid','port':1883,'tls':False,'username':'synthetic-user','password':'synthetic-mqtt-secret'})
        self.runtime.mqtt_settings = self.settings
        self.state_id=self.store.get('state_id')
        self.scene_owner=self.runtime.scene_engine._owner()
        self.runtime.store.set('native_scenes_v1', [{'id':'s'*32,'reference':1,'name':'Synthetic scene',
            'owner':self.runtime.scene_engine._owner(),'members':[{'target':40002,'brightness':128,'kelvin':4000}], 'exposed':True}])

    def backup(self):
        raw = b.snapshot_db(self.store.db)
        settings = b.portable_settings(self.runtime, self.config)
        blob = encode_backup(raw, settings, PASSWORD)
        return dict(decoded=decode_backup(blob, PASSWORD), digest=hashlib.sha256(blob).hexdigest(),
                    password=PASSWORD, current_settings=settings)

    async def close(self):
        await self.settings.close(); self.store.close(); self.lock.__exit__(None,None,None)


class BackupManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = SyntheticInstallation(Path(self.tmp.name).resolve()/'data')
        self.restart = []
        self.manager = b.BackupManager(self.fixture.runtime, self.fixture.config, self.restart.append)
        self.fixture.runtime.backup_manager = self.manager

    async def asyncTearDown(self):
        self.manager.clear(); await self.fixture.close(); self.tmp.cleanup()

    async def create(self):
        return await self.manager.create({'password':PASSWORD})

    async def preview(self, archive=None):
        archive = archive if archive is not None else (await self.create())['archive']
        return await self.manager.preview({'archive':archive,'password':PASSWORD})

    def apply(self, preview):
        return dict(preview_id=preview['preview_id'], revision=preview['revision'],
            confirm_old_bridge_stopped=True, confirm_replace=True, confirm_backup_history=True)

    async def test_create_preserves_full_identity_and_settings_without_radio(self):
        before = b.snapshot_db(self.fixture.store.db)
        result = await self.create()
        decoded = decode_backup(base64.b64decode(result['archive']),PASSWORD)
        self.assertEqual(decoded['database'],before)
        self.assertEqual(decoded['settings']['node'],self.fixture.config['node'])
        self.assertEqual(decoded['settings']['mqtt']['broker']['password'],'synthetic-mqtt-secret')
        self.assertEqual(decoded['summary']['devices'],2)
        self.assertEqual(decoded['summary']['scenes'],1)
        self.assertEqual(self.fixture.transport.frames,[])
        self.assertTrue(result['filename'].endswith('.halobackup'))
        self.assertEqual(self.manager.status()['last_backup']['created_at'],decoded['created_at'])
        self.assertEqual(self.manager.status()['current']['scenes'],1)

    async def test_preview_does_not_apply_or_send_and_is_credential_free(self):
        result = await self.create(); before = b.snapshot_db(self.fixture.store.db)
        preview = await self.preview(result['archive'])
        self.assertTrue(preview['can_restore']); self.assertEqual(preview['mode'],'same_bridge')
        self.assertEqual(b.snapshot_db(self.fixture.store.db),before)
        self.assertEqual(self.restart,[]); self.assertEqual(self.fixture.transport.frames,[])
        text=json.dumps(preview)
        for secret in (PASSWORD,'synthetic-mqtt-secret',self.fixture.token,private_text(self.fixture.config['mesh_passphrase_file'])):
            self.assertNotIn(secret,text)

    async def test_wrong_password_clears_prior_review_and_changes_no_state(self):
        result=await self.create(); await self.preview(result['archive'])
        before=b.snapshot_db(self.fixture.store.db)
        with self.assertRaises(InstallationBackupError):
            await self.manager.preview({'archive':result['archive'],'password':'wrong synthetic password'})
        self.assertIsNone(self.manager.review)
        self.assertEqual(b.snapshot_db(self.fixture.store.db),before)
        self.assertEqual(self.restart,[])

    async def test_invalid_base64_or_oversized_input_never_decrypts(self):
        with patch.object(b,'decode_backup',side_effect=AssertionError('No decrypt')):
            for archive in ('%%%invalid%%%',123):
                with self.assertRaises(b.BackupManagerError):
                    await self.manager.preview({'archive':archive,'password':PASSWORD})
            with patch.object(b,'MAX_HTTP_BYTES',10):
                with self.assertRaises(b.BackupManagerError):
                    await self.manager.preview({'archive':'A'*11,'password':PASSWORD})

    async def test_missing_confirmation_and_expired_wrong_preview_never_apply(self):
        preview=await self.preview(); valid=self.apply(preview)
        for field in ('confirm_old_bridge_stopped','confirm_replace','confirm_backup_history'):
            with self.assertRaises(b.BackupManagerError):
                await self.manager.restore(dict(valid,**{field:False}))
        for field in ('preview_id','revision'):
            with self.assertRaises(b.BackupManagerError):
                await self.manager.restore(dict(valid,**{field:'not-current'}))
        self.manager.clock=lambda:self.manager.review['expires']+1
        with self.assertRaises(b.BackupManagerError) as caught:
            await self.manager.restore(valid)
        self.assertEqual(caught.exception.code,'preview_expired')
        self.assertEqual(self.restart,[])
        self.assertEqual(self.fixture.transport.frames,[])

    async def test_restore_hands_off_once_without_replacing_live_database(self):
        preview=await self.preview(); before=b.snapshot_db(self.fixture.store.db)
        result=await self.manager.restore(self.apply(preview))
        self.assertTrue(result['accepted']);self.assertFalse(result['device_programming'])
        self.assertEqual(len(self.restart),1)
        self.assertEqual(b.snapshot_db(self.fixture.store.db),before)
        self.assertTrue(self.fixture.runtime.maintenance)
        self.assertFalse((Path(self.fixture.config['state_directory'])/b.POINTER).exists())
        with self.assertRaises(b.BackupManagerError):await self.manager.restore(self.apply(preview))
        with self.assertRaises(RuntimeError):await self.fixture.runtime.send(model_command(40002,0,2,b'\x01\0\0'))
        self.assertEqual(self.fixture.transport.frames,[])

    async def test_programming_changes_after_preview_are_revalidated(self):
        preview=await self.preview()
        receipt=self.fixture.store.begin('group_membership',40002,{'group':291,'desired_member':False})
        self.fixture.store.transition(receipt,'prepared','attempted')
        self.fixture.store.transition(receipt,'attempted','uncertain')
        before=b.snapshot_db(self.fixture.store.db)
        with self.assertRaises(RestoreStateError):await self.manager.restore(self.apply(preview))
        self.assertIsNone(self.manager.review)
        self.assertEqual(b.snapshot_db(self.fixture.store.db),before)
        self.assertEqual(self.restart,[])
        self.assertFalse(getattr(self.fixture.runtime,'maintenance',False))

    async def test_different_node_is_reviewed_as_blocked_and_cannot_apply(self):
        plan=self.fixture.backup(); settings=dict(plan['decoded']['settings'],node='another_bridge')
        blob=encode_backup(plan['decoded']['database'],settings,PASSWORD)
        preview=await self.preview(base64.b64encode(blob).decode())
        self.assertFalse(preview['can_restore']); self.assertIsNone(preview['preview_id'])
        self.assertIsNone(self.manager.review);self.assertEqual(self.restart,[])

    async def test_replacement_manager_requires_one_use_review_and_calls_installer_once(self):
        archive=(await self.create())['archive']; installed=[]
        async def install(plan):installed.append(plan)
        manager=b.BackupManager(install=install)
        try:
            self.assertFalse(manager.status()['backup_supported'])
            preview=await manager.preview({'archive':archive,'password':PASSWORD})
            self.assertTrue(preview['can_restore']);self.assertEqual(preview['mode'],'replacement')
            await manager.restore(self.apply(preview))
            self.assertEqual(len(installed),1)
            self.assertEqual(installed[0]['decoded']['settings']['node'],self.fixture.config['node'])
            with self.assertRaises(b.BackupManagerError):await manager.restore(self.apply(preview))
        finally:manager.clear()
        self.assertEqual(self.fixture.transport.frames,[])

    async def test_active_transaction_and_maintenance_refuse_snapshot(self):
        with self.fixture.store.transaction():
            with self.assertRaises(b.BackupManagerError):await self.create()
        self.fixture.runtime.maintenance=True
        with self.assertRaises(b.BackupManagerError):await self.create()
        self.assertEqual(self.fixture.transport.frames,[])


    async def test_final_validation_holds_write_locks_and_blocks_radio(self):
        import threading
        preview=await self.preview(); entered=asyncio.Event(); release=threading.Event()
        loop=asyncio.get_running_loop(); original=b.validate_restored_database
        def gated(*args,**kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):raise RuntimeError('synthetic test deadline')
            return original(*args,**kwargs)
        with patch.object(b,'validate_restored_database',side_effect=gated):
            task=asyncio.create_task(self.manager.restore(self.apply(preview)))
            try:
                await asyncio.wait_for(entered.wait(),2)
                self.assertTrue(self.fixture.runtime.maintenance)
                self.assertTrue(self.fixture.runtime.operation.locked())
                self.assertTrue(self.fixture.settings._change.locked())
                with self.assertRaises(RuntimeError):await self.fixture.runtime.send(model_command(40002,0,2,b'\x01\0\0'))
                self.assertEqual(self.fixture.transport.frames,[])
            finally:release.set()
            result=await asyncio.wait_for(task,5)
        self.assertTrue(result['accepted']);self.assertEqual(len(self.restart),1)
        self.assertTrue(self.fixture.runtime.maintenance)
        self.assertFalse(self.fixture.runtime.operation.locked())
        self.assertFalse(self.fixture.settings._change.locked())

    async def test_final_validation_failure_releases_locks_and_leaves_original_running(self):
        preview=await self.preview();before=b.snapshot_db(self.fixture.store.db)
        with patch.object(b,'validate_restored_database',side_effect=RestoreStateError('synthetic','Synthetic validation refusal')):
            with self.assertRaises(RestoreStateError):await self.manager.restore(self.apply(preview))
        self.assertFalse(self.fixture.runtime.maintenance)
        self.assertFalse(self.fixture.runtime.operation.locked())
        self.assertFalse(self.fixture.settings._change.locked())
        self.assertEqual(self.restart,[]);self.assertEqual(b.snapshot_db(self.fixture.store.db),before)
        self.assertIsNone(self.manager.review)

    async def test_oversized_live_database_refused_before_temporary_copy(self):
        with patch.object(b,'MAX_DATABASE_BYTES',1), patch.object(b.tempfile,'TemporaryDirectory',side_effect=AssertionError('No temporary copy')):
            with self.assertRaises(b.BackupManagerError) as caught:b.snapshot_db(self.fixture.store.db)
        self.assertEqual(caught.exception.code,'backup_too_large')

    async def test_restore_maintenance_blocks_discovery_and_claim_without_radio_or_receipts(self):
        from avionmesh_pi.commissioning import Commissioning
        preview=await self.preview()
        await self.manager.restore(self.apply(preview))
        commissioner=Commissioning(self.fixture.runtime)
        before=b.snapshot_db(self.fixture.store.db)
        receipts=self.fixture.store.journals()
        with self.assertRaisesRegex(RuntimeError,'restarting for restore'):
            await commissioner.discover(0)
        with self.assertRaisesRegex(RuntimeError,'restarting for restore'):
            await commissioner.claim('0100000000021400000a000000000000','Synthetic fresh A19',134,inventory_complete=True)
        self.assertEqual(self.fixture.transport.frames,[])
        self.assertEqual(self.fixture.store.journals(),receipts)
        self.assertEqual(b.snapshot_db(self.fixture.store.db),before)
        self.assertEqual(self.fixture.runtime.raw_observers,[])

    async def test_newer_version_backup_refused_before_capture_or_install(self):
        plan=self.fixture.backup()
        blob=encode_backup(plan['decoded']['database'],plan['decoded']['settings'],PASSWORD,version='0.5.0.dev999999')
        before=b.snapshot_db(self.fixture.store.db)
        with patch.object(self.manager,'capture',new=AsyncMock(side_effect=AssertionError('No current capture for newer format'))):
            with self.assertRaises(b.BackupManagerError) as caught:
                await self.manager.preview({'archive':base64.b64encode(blob).decode(),'password':PASSWORD})
        self.assertEqual(caught.exception.code,'backup_version_unsupported')
        self.assertIsNone(self.manager.review)
        self.assertEqual(self.restart,[])
        self.assertEqual(self.fixture.transport.frames,[])
        self.assertEqual(b.snapshot_db(self.fixture.store.db),before)


class InstallationSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.fixture=SyntheticInstallation(Path(self.tmp.name).resolve()/'data')

    async def asyncTearDown(self):
        await self.fixture.close();self.tmp.cleanup()

    async def stopped(self):
        await self.fixture.settings.close();self.fixture.store.close()

    async def test_installation_switch_preserves_old_files_and_blocks_old_writer(self):
        plan=self.fixture.backup(); original=dict(self.fixture.config)
        before_key=Path(original['mesh_passphrase_file']).read_bytes(); before_cfg=Path(self.fixture.root/'bridge/service.json').read_bytes()
        await self.stopped(); root=Path(original['state_directory'])
        result=b.finish_restore(original,plan)
        self.assertEqual(Path(original['mesh_passphrase_file']).read_bytes(),before_key)
        self.assertEqual((self.fixture.root/'bridge/service.json').read_bytes(),before_cfg)
        self.assertNotEqual(result['state_directory'],original['state_directory'])
        self.assertEqual(result['node'],original['node'])
        self.assertEqual(result['http_host'],HOST);self.assertEqual(result['adapter'],'hci0')
        self.assertEqual(private_text(result['mesh_passphrase_file']),private_text(original['mesh_passphrase_file']))
        self.assertEqual(private_text(result['api_token_file']),self.fixture.token)
        self.assertTrue(metadata(root/'bridge.sqlite3')['installation_superseded_v1'])
        self.assertTrue((root/b.POINTER).is_file());self.assertFalse((root/b.PENDING).exists())
        generation=Path(result['state_directory']).parent
        archive=(generation/'before-restore.halobackup').read_bytes()
        self.assertEqual(decode_backup(archive,PASSWORD)['database'],plan['decoded']['database'])
        old=Store(root,self.fixture.key)
        try:
            with self.assertRaises(RuntimeError):old.require_tx_ready()
        finally:old.close()
        new=Store(result['state_directory'],self.fixture.key)
        try:
            new.require_tx_ready();self.assertEqual(new.inventory(),INVENTORY)
            self.assertEqual(new.get('state_id'),self.fixture.state_id)
            self.assertEqual(new.get('device_rooms'),{'40001':'Synthetic room','40002':'Synthetic room'})
            self.assertEqual(new.get('native_scenes_v1')[0]['owner'],self.fixture.scene_owner)
        finally:new.close()
        self.assertEqual(b.resolve_config(original)['state_directory'],result['state_directory'])

    async def test_plain_restore_saves_an_encrypted_local_recovery_copy(self):
        plan = self.fixture.backup()
        plan['password'] = None
        await self.stopped()
        installed = b.finish_restore(self.fixture.config, plan)
        generation = Path(installed['state_directory']).parent
        password_file = generation / 'before-restore.password'
        self.assertEqual(password_file.stat().st_mode & 0o077, 0)
        recovered = decode_backup((generation / 'before-restore.halobackup').read_bytes(),
                                  private_text(password_file))
        self.assertTrue(recovered['summary']['encrypted'])
        self.assertEqual(recovered['database'], plan['decoded']['database'])

    async def test_destination_access_and_removal_survive_in_place_restore(self):
        from avionmesh_pi.service import trusted_lan_mode, browser_login_mode
        archive = self.fixture.backup()
        await self.stopped()
        root_config = self.fixture.config.copy()
        # Token-authenticated LAN service relies on the default false access flags.
        for flag in ('trusted_lan_no_auth', 'browser_login'):
            root_config.pop(flag)
        root_config['capabilities'] = dict(root_config['capabilities'], removal=False)
        restored = b.finish_restore(root_config, archive)
        self.assertFalse(trusted_lan_mode(restored))
        self.assertFalse(browser_login_mode(restored))
        self.assertFalse(restored['capabilities']['removal'])
        # Dev69/70 could persist trusted-LAN access in a generation. The active
        # root policy and credentials must still win, including after a restart.
        generation = Path(restored['state_directory']).parent
        spec = b.read_json(generation / 'service.json')
        b.replace_json(generation / 'service.json', dict(spec, trusted_lan_no_auth=True))
        for host, login, trusted in ((HOST, False, False), (HOST, True, False),
                                     (HOST, False, True), ('127.0.0.1', False, False)):
            for removal in (True, False):
                with self.subTest(host=host, login=login, trusted=trusted, removal=removal):
                    config = dict(root_config, http_host=host, browser_login=login, trusted_lan_no_auth=trusted,
                                  capabilities=dict(root_config['capabilities'], removal=removal))
                    for _ in range(2):
                        active = b.resolve_config(config)
                        self.assertEqual(active['http_host'], host)
                        self.assertEqual(trusted_lan_mode(active), trusted)
                        self.assertEqual(browser_login_mode(active), login)
                        self.assertEqual(active['capabilities']['removal'], removal)

    async def test_advanced_service_keeps_token_access_without_commissioning_lan_setting(self):
        archive = self.fixture.backup()
        await self.stopped()
        config = dict(self.fixture.config, commissioning_only=False, mqtt={'host':'local.test'})
        for key in ('first_device_bootstrap','automatic_bootstrap','trusted_lan_no_auth','browser_login','allow_lan_http'):
            config.pop(key, None)
        config['relay_address'] = '00:11:22:33:44:55'
        restored = b.finish_restore(config, archive)
        self.assertFalse(restored['commissioning_only'])
        self.assertFalse(restored['trusted_lan_no_auth'])
        self.assertEqual(restored['mqtt']['host'], 'local.test')
        self.assertEqual(b.resolve_config(config), restored)

    async def test_loopback_restore_accepts_bearer_defaults_before_pointer_commit(self):
        archive = self.fixture.backup()
        await self.stopped()
        config = dict(self.fixture.config, http_host='127.0.0.1')
        for key in ('trusted_lan_no_auth', 'browser_login', 'allow_lan_http'):
            config.pop(key)
        restored = b.finish_restore(config, archive)
        self.assertFalse(restored['trusted_lan_no_auth'])
        self.assertFalse(restored['allow_lan_http'])

    async def test_live_database_lock_prevents_any_installation_switch(self):
        plan=self.fixture.backup();root=Path(self.fixture.config['state_directory'])
        with self.assertRaises(b.BackupManagerError) as caught:b.finish_restore(self.fixture.config,plan)
        self.assertEqual(caught.exception.code,'restore_busy')
        self.assertFalse((root/b.PENDING).exists());self.assertFalse((root/b.POINTER).exists())
        self.assertNotIn('installation_superseded_v1',metadata(root/'bridge.sqlite3'))

    async def test_failure_before_pending_pointer_leaves_old_installation_usable(self):
        plan=self.fixture.backup();await self.stopped();root=Path(self.fixture.config['state_directory'])
        real_write=b.write_json
        def fail_pending(path,value):
            if path.name==b.PENDING:raise OSError('synthetic pending failure')
            return real_write(path,value)
        with patch.object(b,'write_json',side_effect=fail_pending):
            with self.assertRaises(OSError):b.finish_restore(self.fixture.config,plan)
        self.assertFalse((root/b.POINTER).exists());self.assertFalse((root/b.PENDING).exists())
        self.assertNotIn('installation_superseded_v1',metadata(root/'bridge.sqlite3'))
        self.assertEqual(b.resolve_config(self.fixture.config),self.fixture.config)
        old=Store(root,self.fixture.key)
        try:old.require_tx_ready()
        finally:old.close()

    async def test_failure_after_old_fence_resumes_pointer_commit_once(self):
        plan=self.fixture.backup();await self.stopped();root=Path(self.fixture.config['state_directory'])
        with patch.object(b,'replace_json',side_effect=OSError('synthetic pointer failure')):
            with self.assertRaises(OSError):b.finish_restore(self.fixture.config,plan)
        self.assertTrue((root/b.PENDING).exists());self.assertFalse((root/b.POINTER).exists())
        marker=b.read_json(root/b.PENDING)
        self.assertEqual(metadata(root/'bridge.sqlite3')['installation_superseded_v1'],marker)
        result=b.resolve_config(self.fixture.config)
        self.assertEqual(b.read_json(root/b.POINTER),marker);self.assertFalse((root/b.PENDING).exists())
        before=Path(result['state_directory'],'bridge.sqlite3').read_bytes()
        again=b.resolve_config(self.fixture.config)
        self.assertEqual(again,result)
        self.assertEqual(Path(result['state_directory'],'bridge.sqlite3').read_bytes(),before)
        self.assertEqual(len(list((root/'installations').glob('[0-9a-f]'*32))),1)

    async def test_invalid_pointer_or_symlink_never_falls_back_to_old_database(self):
        root=Path(self.fixture.config['state_directory'])
        for marker in ({'format':1,'generation':'../state'}, {'format':1,'generation':'f'*32}, {'format':2,'generation':'f'*32}):
            b.replace_json(root/b.POINTER,marker)
            with self.assertRaises(b.BackupManagerError):b.resolve_config(self.fixture.config)
        (root/b.POINTER).unlink()
        parent=root/'installations';parent.mkdir(mode=0o700)
        destination=Path(self.tmp.name)/'unowned';destination.mkdir()
        (parent/('f'*32)).symlink_to(destination,target_is_directory=True)
        b.write_json(root/b.POINTER,{'format':1,'generation':'f'*32})
        with self.assertRaises(b.BackupManagerError):b.resolve_config(self.fixture.config)
        self.assertNotIn('installation_superseded_v1',metadata(root/'bridge.sqlite3'))

    async def test_failure_after_pointer_commit_completes_without_repeating_restore(self):
        plan=self.fixture.backup();await self.stopped();root=Path(self.fixture.config['state_directory'])
        original_unlink=Path.unlink
        def fail_pending(path,*args,**kwargs):
            if path==root/b.PENDING:raise OSError('synthetic cleanup failure')
            return original_unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail_pending):
            with self.assertRaises(OSError):b.finish_restore(self.fixture.config,plan)
        self.assertTrue((root/b.POINTER).exists());self.assertTrue((root/b.PENDING).exists())
        marker=b.read_json(root/b.POINTER);generation=b.generation_path(root,marker)
        before=(generation/'state/bridge.sqlite3').read_bytes()
        result=b.resolve_config(self.fixture.config)
        self.assertFalse((root/b.PENDING).exists());self.assertEqual(b.read_json(root/b.POINTER),marker)
        self.assertEqual((generation/'state/bridge.sqlite3').read_bytes(),before)
        self.assertEqual(result['state_directory'],str(generation/'state'))
        self.assertEqual(len(metadata(generation/'state/bridge.sqlite3')['tx_restore_history_v1']),1)

    async def test_second_restore_uses_active_generation_and_keeps_original_history(self):
        plan=self.fixture.backup();await self.stopped();root=Path(self.fixture.config['state_directory'])
        first=b.finish_restore(self.fixture.config,plan)
        current=b.read_database(Path(first['state_directory'])/'bridge.sqlite3')
        archive=encode_backup(current,plan['decoded']['settings'],PASSWORD)
        second_plan=dict(decoded=decode_backup(archive,PASSWORD),digest=hashlib.sha256(archive).hexdigest(),
            password=PASSWORD,current_settings=plan['decoded']['settings'])
        second=b.finish_restore(first,second_plan)
        self.assertNotEqual(first['state_directory'],second['state_directory'])
        self.assertEqual(second['_installation_root'],str(root))
        self.assertTrue(metadata(Path(first['state_directory'])/'bridge.sqlite3')['installation_superseded_v1'])
        self.assertTrue(metadata(root/'bridge.sqlite3')['installation_superseded_v1'])
        self.assertEqual(b.resolve_config(self.fixture.config)['state_directory'],second['state_directory'])
        restored=metadata(Path(second['state_directory'])/'bridge.sqlite3')
        self.assertEqual(restored['state_id'],self.fixture.state_id)
        self.assertEqual(len(restored['tx_restore_history_v1']),2)
        self.assertEqual(restored['inventory'],INVENTORY)

    async def test_nested_state_symlink_refuses_commit_before_old_writer_fenced(self):
        plan=self.fixture.backup();await self.stopped();root=Path(self.fixture.config['state_directory'])
        with patch.object(b,'complete_switch',side_effect=OSError('synthetic interruption before commit')):
            with self.assertRaises(OSError):b.finish_restore(self.fixture.config,plan)
        marker=b.read_json(root/b.PENDING);generation=b.generation_path(root,marker)
        outside=Path(self.tmp.name)/'outside-state'
        (generation/'state').rename(outside)
        (generation/'state').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):b.resolve_config(self.fixture.config)
        self.assertFalse((root/b.POINTER).exists())
        self.assertNotIn('installation_superseded_v1',metadata(root/'bridge.sqlite3'))

    async def test_changed_generation_identity_refuses_commit_preserving_old_state(self):
        plan=self.fixture.backup();await self.stopped();root=Path(self.fixture.config['state_directory'])
        with patch.object(b,'complete_switch',side_effect=OSError('synthetic interruption before commit')):
            with self.assertRaises(OSError):b.finish_restore(self.fixture.config,plan)
        marker=b.read_json(root/b.PENDING);generation=b.generation_path(root,marker)
        with sqlite3.connect(generation/'state/bridge.sqlite3') as db:
            db.execute('UPDATE metadata SET value=? WHERE key=?',(json.dumps('f'*32),'state_id'))
        with self.assertRaises(ValueError):b.resolve_config(self.fixture.config)
        self.assertFalse((root/b.POINTER).exists())
        self.assertNotIn('installation_superseded_v1',metadata(root/'bridge.sqlite3'))
