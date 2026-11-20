"""Offline release discovery, installer rollback and cross-restart handoff."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import io
import json
from pathlib import Path
import socket
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from avionmesh_pi import release_update as host
from avionmesh_pi import settings_update as web

CURRENT = web.__version__
NEXT = str(host.version_key(CURRENT)[0] + 1) + '.0.0.dev1'


class ReleaseTests(unittest.TestCase):
    def test_latest_published_with_matching_assets_and_numeric_version(self):
        def row(v, draft=False):
            return dict(tag_name=host.tag(v), draft=draft, prerelease=True, assets=[
                dict(name=n, state='uploaded') for n in (host.archive(v), 'SHA256SUMS')])
        rows = [row('0.5.0.dev10'), row('0.5.0.dev9'), row('0.6.0', True),
                dict(tag_name='v0.8.0', assets=[]), dict(tag_name='latest')]
        with patch.object(host, 'small_file', return_value=json.dumps(rows).encode()):
            self.assertEqual(host.latest_release(), '0.5.0.dev10')
        self.assertGreater(host.version_key('0.5.0'), host.version_key('0.5.0.dev999'))
        for invalid in ('latest', '-f', '1.0.0/dev1', '1.2.03', None):
            with self.subTest(invalid=invalid), self.assertRaises(host.UpdateError):
                host.version_key(invalid)

    def test_archive_must_match_its_own_unique_checksum(self):
        content = b'synthetic image'
        digest = hashlib.sha256(content).hexdigest()
        row = f'{digest}  {host.archive(NEXT)}\n'
        for manifest, good in ((row, True), (row * 2, False), (row.replace(digest, '0'*64), False),
                               (row.replace(host.archive(NEXT), 'other.tar.gz'), False)):
            with tempfile.TemporaryDirectory() as folder, patch.object(host, 'small_file', return_value=manifest.encode()), patch.object(host, 'open_url', return_value=io.BytesIO(content)):
                path = Path(folder) / 'image'
                if good:
                    host.download(NEXT, path)
                    self.assertEqual(path.read_bytes(), content)
                else:
                    with self.assertRaises(host.UpdateError):
                        host.download(NEXT, path)

    def test_preflight_refuses_changed_release_or_custom_checkout(self):
        install = host.Installation('compose.yaml')
        with patch.object(host, 'latest_release', return_value=CURRENT), patch.object(host, 'run') as run:
            with self.assertRaises(host.UpdateError):
                install.prepare(CURRENT, NEXT)
            run.assert_not_called()
        with patch.object(host, 'latest_release', return_value=NEXT), patch.object(install, 'running', return_value=('old', CURRENT)), patch.object(host, 'run', return_value='user-fork'), patch.object(host, 'download') as download:
            with self.assertRaisesRegex(host.UpdateError, 'public'):
                install.prepare(CURRENT, NEXT)
            download.assert_not_called()

    def test_replace_only_bridge_service_and_verify_version_and_image(self):
        install = host.Installation('compose.bluetooth.yaml')
        with patch.object(host, 'run', return_value='sha256:expected') as run, patch.object(install, 'running', return_value=('sha256:expected', NEXT)):
            install.replace('halo-local:'+NEXT, NEXT)
            compose = [c.args for c in run.call_args_list if 'compose' in c.args][0]
            self.assertIn('--no-deps', compose)
            self.assertIn('--no-build', compose)
            self.assertEqual(compose[-1], 'bridge')
            self.assertIn('compose.bluetooth.yaml', compose)
        with patch.object(host, 'run', return_value='wrong'), patch.object(install, 'running', return_value=('other', CURRENT)):
            with self.assertRaisesRegex(host.UpdateError, 'expected image'):
                install.replace('new', NEXT)

    def test_rollback_restores_source_and_exact_image_on_failed_update(self):
        for failures, phases in (([None], ['installing','complete']),
                ([host.UpdateError('health'), None], ['installing','rolling_back','rolled_back']),
                ([host.UpdateError('health'), host.UpdateError('rollback')], ['installing','rolling_back','needs_review'])):
            install = host.Installation('compose.yaml')
            head = 'old-head'
            def git(*args, **kwargs):
                nonlocal head
                if args == ('git','rev-parse','HEAD'):
                    return head
                if args == ('git','rev-parse','--abbrev-ref','HEAD'):
                    return 'main'
                if args[:2] in (('git','merge'), ('git','reset')):
                    head = args[-1]
                return ''
            with patch.object(install, 'clean'), patch.object(install, 'validate_stack'), patch.object(install, 'running', return_value=('old-id',CURRENT)), patch.object(host, 'run', side_effect=git), patch.object(install, 'replace', side_effect=failures) as replace:
                seen = []
                install.apply(('old-head','new-head','old-id','new-image','main'), CURRENT, NEXT, lambda phase, msg: seen.append(phase))
                self.assertEqual(seen, phases)
                self.assertEqual(head, 'new-head' if seen[-1] == 'complete' else 'old-head')
                if len(failures)>1:
                    self.assertEqual(replace.call_args.args, ('old-id',CURRENT))


@unittest.skipUnless(shutil.which('git'), 'host-side checkout test requires Git')
class CheckoutTests(unittest.TestCase):
    def test_prebuilt_shallow_checkout_and_image_rollback(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            remote = root / 'upstream'
            checkout = root / 'bridge'
            remote.mkdir()
            def git(directory, *args):
                return subprocess.check_output(['git', '-C', str(directory), *args], text=True, stderr=subprocess.PIPE).strip()
            git(remote, 'init')
            git(remote, 'config', 'user.name', 'Synthetic')
            git(remote, 'config', 'user.email', 'test@example.invalid')
            for version in (CURRENT, NEXT):
                (remote/'compose.yaml').write_text('# '+version)
                git(remote, 'add', 'compose.yaml')
                git(remote, 'commit', '-m', version)
                git(remote, 'tag', host.tag(version))
            git(root, 'clone', '--depth', '1', '--branch', host.tag(CURRENT), remote.as_uri(), str(checkout))
            git(checkout, 'remote', 'set-url', 'origin', host.PUBLIC + '.git')
            (checkout/'.env').write_text('HALO_PORT=18089\n')
            image_file = b'synthetic release image'
            checksum = hashlib.sha256(image_file).hexdigest()+'  '+host.archive(NEXT)
            old_id, new_id = 'sha256:previous', 'sha256:next'
            current_id = old_id
            tags = {'halo-local:local':old_id, old_id:old_id}
            cid = 'a'*64
            fail_start = True
            def runner(*args, **kw):
                nonlocal current_id
                if args[0]=='git':
                    argv = [remote.as_uri() if a == host.PUBLIC+'.git' else a for a in args[1:]]
                    return git(checkout, *argv)
                if args[:4]==('docker','compose','-f','compose.yaml'):
                    if args[4]=='ps':
                        return cid
                    if args[4]=='config':
                        return json.dumps({'services':{'bridge':{'image':'halo-local:local','volumes':[
                            {'target':'/data','type':'volume','source':'data'}]}},'volumes':{'data':{'name':'halo-local-data'}}})
                    self.assertEqual(args[4], 'up')
                    current_id = tags['halo-local:local']
                    if fail_start and current_id == new_id:
                        raise host.UpdateError('synthetic unhealthy bridge')
                    return ''
                if args[1]=='inspect':
                    return json.dumps([dict(Image=current_id, Mounts=[dict(Name='halo-local-data', Destination='/data')],
                        Config=dict(Image='halo-local:local', Labels={'com.docker.compose.project.config_files':str(checkout/'compose.yaml'),
                            'com.docker.compose.project.working_dir':str(checkout)}))])
                if args[1]=='exec':
                    return CURRENT if current_id == old_id else NEXT
                if args[1]=='load':
                    self.assertEqual(Path(args[-1]).read_bytes(), image_file)
                    tags['halo-local:'+NEXT] = new_id
                    return 'Loaded image: halo-local:'+NEXT
                if args[1]=='tag':
                    tags[args[3]]=tags[args[2]]
                    return ''
                self.assertEqual(args[1:3], ('image','inspect'))
                return 'linux/arm64' if 'Architecture' in args[-2] else tags[args[-1]]
            try:
                os.chdir(checkout)
                with patch.object(host, 'run', side_effect=runner), patch.object(host, 'STATE', root), patch.object(host, 'latest_release', return_value=NEXT), patch.object(host, 'small_file', return_value=checksum.encode()), patch.object(host, 'open_url', side_effect=lambda _:io.BytesIO(image_file)):
                    stack = host.Installation('compose.yaml')
                    for fail_start in (True, False):
                        prepared = stack.prepare(CURRENT,NEXT)
                        phases = []
                        stack.apply(prepared,CURRENT,NEXT,lambda phase,_:phases.append(phase))
                        self.assertEqual(phases[-1], 'rolled_back' if fail_start else 'complete')
                        self.assertEqual(stack.running()[1], CURRENT if fail_start else NEXT)
                        self.assertEqual((checkout/'compose.yaml').read_text(), '# '+stack.running()[1])
                        self.assertEqual((checkout/'.env').read_text(), 'HALO_PORT=18089\n')
            finally:
                os.chdir(original)


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.sockpath = str(Path(self.folder.name) / 'socket')
        for obj, key, value in ((host, 'STATE', Path(self.folder.name)), (host, 'SOCKET', self.sockpath)):
            p = patch.object(obj, key, value);p.start();self.addCleanup(p.stop)
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(self.sockpath)
        self.server.listen(4)
        self.server.setblocking(False)
        self.addCleanup(self.server.close)
        self.runtime = Mock(maintenance=False)
        @asynccontextmanager
        async def foreground():
            yield
        self.runtime.foreground_request = foreground
        self.busy = Mock(return_value=False)
        self.manager = web.BridgeUpdates(self.runtime, self.busy)
        self.manager.latest = NEXT
        self.manager.checked = time.monotonic()
        self.confirm = dict(version=NEXT, confirm_restart=True)

    async def serve_once(self):
        conn, _ = await asyncio.get_running_loop().sock_accept(self.server)
        with conn:
            await asyncio.to_thread(host.handle, conn, 'compose.yaml')

    async def test_handoff_rechecks_busy_and_survives_browser_disconnection(self):
        for allow in (False, True):
            host.write_state({'phase':'idle'})
            self.busy.side_effect = lambda own_request=False: own_request and not allow
            def apply(prepared, current, target, record):
                self.assertTrue(self.runtime.maintenance)
                record('complete','Healthy')
            with patch.object(host.Installation, 'prepare', return_value=()), patch.object(host.Installation, 'apply', side_effect=apply) as installer:
                helper = asyncio.create_task(self.serve_once())
                receipt = await self.manager.install(self.confirm)
                self.assertEqual(receipt['job']['phase'], 'preparing')
                await asyncio.wait_for(helper, 3)
                await self.manager.task
                self.assertEqual(installer.called, allow)
                self.assertFalse(self.runtime.maintenance)
                helper = asyncio.create_task(self.serve_once())
                after_restart = await web.BridgeUpdates(self.runtime, self.busy).status()
                await helper
                self.assertEqual(after_restart['job']['phase'], 'complete' if allow else 'failed')
                self.assertFalse(after_restart['available'])

    async def test_no_helper_check_still_works_and_install_requires_reviewed_version(self):
        self.server.close()
        with patch.object(host, 'latest_release', return_value=NEXT) as lookup:
            fresh = web.BridgeUpdates(self.runtime, self.busy)
            state = await fresh.check()
            self.assertTrue(state['available'])
            self.assertEqual(state['job']['phase'], 'unavailable')
            await fresh.check()
            lookup.assert_called_once()
        for payload in ({}, dict(version=NEXT,confirm_restart=False), dict(version=CURRENT,confirm_restart=True),
                        dict(self.confirm, url='file:///tmp/image')):
            with self.assertRaises(web.UpdateRequestError):
                await self.manager.install(payload)
        self.manager.checked -= 1000
        with self.assertRaises(web.UpdateRequestError):
            await self.manager.install(self.confirm)

    async def test_interrupted_restart_requires_review(self):
        host.write_state({'phase':'installing','target':NEXT})
        server = asyncio.create_task(self.serve_once())
        self.assertEqual((await self.manager.status())['job']['phase'], 'needs_review')
        await server
        server = asyncio.create_task(self.serve_once())
        with self.assertRaises(web.UpdateRequestError):
            await self.manager.install(self.confirm)
        await server
