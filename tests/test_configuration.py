import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.service import load_config,private_text,serve
from avionmesh_pi.protocol import derive_mcp_key
from avionmesh_pi.storage import Store
from avionmesh_pi.migration import admit
from support import declaration

class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.secret=self.root/'secret';self.secret.write_text('synthetic');self.secret.chmod(0o600)
        self.path=self.root/'config.json'
        self.config={'relay_address':'00:11:22:33:44:55','node':'synthetic',
            'state_directory':str(self.root/'state'),'mesh_passphrase_file':str(self.secret),
            'api_token_file':str(self.secret),'mqtt':{'host':'broker.example.invalid'}}
    def tearDown(self):self.temp.cleanup()
    def load(self,data):
        self.path.write_text(json.dumps(data));return load_config(self.path)
    def test_anyplace_capabilities_are_separate_opt_in_booleans(self):
        self.assertFalse(self.load(self.config).get('capabilities', {}).get('anyplace_onboarding', False))
        for flag in ('anyplace_editor', 'anyplace_onboarding'):
            data=copy.deepcopy(self.config);data['capabilities']={flag:True}
            self.assertTrue(self.load(data)['capabilities'][flag])
            data['capabilities'][flag]=1
            with self.assertRaises(ValueError):self.load(data)
    def test_defaults_and_strict_types_are_checked_before_secret_reads(self):
        self.assertNotIn('tls',self.load(self.config)['mqtt'])  # MQTT transport's default is verified TLS.
        cases=[('node',None),('http_port',True),('http_port',0),('http_port',65536),('http_host',[]),
               ('http_host','https://invalid'),('adapter','hci0/escape'),('state_directory','relative'),
               ('capabilities',{'ordinary_claim':1}),('relay_address',False)]
        for key,value in cases:
            data=copy.deepcopy(self.config);data[key]=value
            with self.assertRaises(ValueError):self.load(data)
        for key,value in [('port',True),('port',-1),('tls','false'),('host',None),('host','https://broker'),
                          ('host','broker name'),('username',[]),('ca_file','relative')]:
            data=copy.deepcopy(self.config);data['mqtt'][key]=value
            with self.assertRaises(ValueError):self.load(data)
        data=copy.deepcopy(self.config);data['mqtt']['password_file']=str(self.root/'missing')
        data['capabilities']={'unknown':True}
        with self.assertRaises(ValueError):self.load(data)  # No attempt to open the nonexistent password.
    def test_secret_permissions_symlink_nonregular_empty_and_size(self):
        self.assertEqual(private_text(self.secret),'synthetic')
        link=self.root/'link';link.symlink_to(self.secret)
        with self.assertRaises(OSError):private_text(link)
        self.secret.chmod(0o644)
        with self.assertRaises(ValueError):private_text(self.secret)
        self.secret.chmod(0o600);self.secret.write_text(' \n')
        with self.assertRaises(ValueError):private_text(self.secret)
        self.secret.write_text('x'*4097)
        with self.assertRaises(ValueError):private_text(self.secret)
        with self.assertRaises(ValueError):private_text(self.root)
        fifo=self.root/'fifo';os.mkfifo(fifo,0o600)
        with self.assertRaises(ValueError):private_text(fifo)  # Must not wait for a writer.
    def test_replacing_path_after_open_cannot_redirect_secret_read(self):
        other=self.root/'other';other.write_text('not-the-opened-file');other.chmod(0o600)
        real_fstat=os.fstat
        def replace(fd):
            info=real_fstat(fd);self.secret.unlink();self.secret.symlink_to(other);return info
        with patch('avionmesh_pi.service.os.fstat',side_effect=replace):
            self.assertEqual(private_text(self.secret),'synthetic')

@unittest.skipIf(importlib.util.find_spec('aiohttp') is None,'Container tests cover service lifecycle')
class StartupCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.secret=self.root/'secret';self.secret.write_text('synthetic');self.secret.chmod(0o600)
        self.token=self.root/'token';self.token.write_text('x'*40);self.token.chmod(0o600)
        self.key=derive_mcp_key('synthetic');self.directory=self.root/'state'
        store=Store(self.directory,self.key);admit(store,declaration(store));store.close()
        self.config={'relay_address':'00:11:22:33:44:55','node':'synthetic','state_directory':str(self.directory),
            'mesh_passphrase_file':str(self.secret),'api_token_file':str(self.token),'mqtt':{'host':'unused.invalid'}}
    async def asyncTearDown(self):self.temp.cleanup()
    def assert_unlocked(self):
        store=Store(self.directory,self.key);store.close()
    async def test_constructor_and_bad_token_failures_release_lock_without_network(self):
        class Transport:
            dropped=0
            async def disconnect(self):pass
        with patch('avionmesh_pi.service.BlueZTransport',return_value=Transport()):
            with patch('avionmesh_pi.service.Runtime',side_effect=ValueError('synthetic constructor failure')):
                with self.assertRaises(ValueError):await serve(self.config)
            self.assert_unlocked()
            self.token.write_text('short')
            with self.assertRaises(ValueError):await serve(self.config)
            self.assert_unlocked()
    async def test_cleanup_and_disconnect_failure_still_release_lock(self):
        class Transport:
            dropped=0
            async def disconnect(self):raise RuntimeError('synthetic disconnect failure')
        class Runner:
            async def setup(self):raise RuntimeError('synthetic setup failure')
            async def cleanup(self):raise RuntimeError('synthetic cleanup failure')
        with patch('avionmesh_pi.service.BlueZTransport',return_value=Transport()),patch('aiohttp.web.AppRunner',return_value=Runner()):
            with self.assertRaises(RuntimeError):await serve(self.config)
        self.assert_unlocked()
