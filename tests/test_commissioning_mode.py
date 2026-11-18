import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from avionmesh_pi.service import commissioning_mode, load_config, serve
from avionmesh_pi.protocol import derive_mcp_key
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.storage import Store
from support import synthetic_store


class CommissioningConfigTests(unittest.TestCase):
    def config(self):
        return {'relay_address':'00:11:22:33:44:55','node':'pi_claim_lab','state_directory':'/state',
                'mesh_passphrase_file':'/secrets/key','api_token_file':'/secrets/token',
                'mqtt':None,'commissioning_only':True,'http_host':'127.0.0.1','http_port':18089}

    def test_explicit_null_broker_loopback_config(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'config.json';path.write_text(json.dumps(self.config()))
            self.assertTrue(load_config(path)['commissioning_only'])

    def test_scenes_can_be_switched_off_in_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'config.json'
            path.write_text(json.dumps(dict(self.config(),capabilities={'native_scenes':False})))
            self.assertIs(load_config(path)['capabilities']['native_scenes'],False)
            path.write_text(json.dumps(dict(self.config(),capabilities={'native_scenes':'off'})))
            with self.assertRaises(ValueError):load_config(path)

    def test_mode_cannot_silently_ignore_broker_or_expose_api(self):
        for change in ({'mqtt':{'host':'real-broker'}},{'http_host':'0.0.0.0'},
                       {'commissioning_only':'true'}):
            with self.assertRaises(ValueError):commissioning_mode(dict(self.config(),**change))

    def test_normal_service_still_requires_broker(self):
        config=self.config();config.pop('commissioning_only')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'config.json';path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):load_config(path)

    def test_lan_http_requires_explicit_boolean_and_private_address(self):
        config=dict(self.config(),http_host='192.168.50.20',allow_lan_http=True)
        self.assertTrue(commissioning_mode(config))
        for changes in ({'allow_lan_http':False},{'allow_lan_http':'true'},
                        {'http_host':'0.0.0.0'},{'http_host':'8.8.8.8'},
                        {'http_host':'169.254.1.1'},{'http_host':'::'}):
            with self.assertRaises(ValueError):commissioning_mode(dict(config,**changes))
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'config.json';path.write_text(json.dumps(config))
            self.assertTrue(load_config(path)['allow_lan_http'])


class CommissioningServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_broker_or_poll_is_constructed_and_cleanup_runs(self):
        try: import aiohttp.web
        except ImportError:self.skipTest('HTTP dependencies are tested in container')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);key=derive_mcp_key('synthetic-commissioning-mode')
            for name,value in [('key','synthetic-commissioning-mode'),('token','synthetic-token-'+'x'*32)]:
                (root/name).write_text(value);(root/name).chmod(0o600)
            config=CommissioningConfigTests().config()
            config.update(state_directory=str(root/'state'),mesh_passphrase_file=str(root/'key'),api_token_file=str(root/'token'))
            store=synthetic_store(root/'state',key);store.close()
            started=asyncio.Event();runtimes=[]
            async def bluetooth(runtime):
                runtimes.append(runtime);started.set();await asyncio.Future()
            transport=AsyncMock();transport.counts={};transport.dropped=0
            runner=AsyncMock();site=AsyncMock()
            with patch('avionmesh_pi.service.BlueZTransport',return_value=transport), patch('avionmesh_pi.service.MqttService',side_effect=AssertionError('Broker forbidden')), patch.object(Runtime,'poll',side_effect=AssertionError('Poll forbidden')), patch.object(Runtime,'bluetooth_loop',bluetooth), patch('aiohttp.web.AppRunner',return_value=runner),patch('aiohttp.web.TCPSite',return_value=site):
                task=asyncio.create_task(serve(config))
                try:
                    await asyncio.wait_for(started.wait(),2)
                    self.assertTrue(runtimes[0].status()['commissioning_only'])
                    self.assertFalse(runtimes[0].mqtt_ready)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):await task
                transport.disconnect.assert_awaited_once()
                runner.cleanup.assert_awaited_once()
            # State remains reopenable, never sends or allocates a sequence.
            reopened=Store(root/'state',key)
            try:self.assertEqual(reopened.get('sequence'),0)
            finally:reopened.close()

    async def test_unadmitted_state_refuses_before_any_adapter(self):
        try: import aiohttp.web
        except ImportError:self.skipTest('HTTP dependencies are tested in container')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name,value in [('key','synthetic-unadmitted'),('token','x'*40)]:
                (root/name).write_text(value);(root/name).chmod(0o600)
            config=CommissioningConfigTests().config()
            config.update(state_directory=str(root/'state'),mesh_passphrase_file=str(root/'key'),api_token_file=str(root/'token'))
            with patch('avionmesh_pi.service.BlueZTransport') as transport,patch('avionmesh_pi.service.MqttService') as mqtt:
                with self.assertRaises(RuntimeError):await serve(config)
                transport.assert_not_called();mqtt.assert_not_called()
