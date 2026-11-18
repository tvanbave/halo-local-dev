import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from avionmesh_pi.bootstrap import BootstrapSelection,DeferredBlueZTransport
from avionmesh_pi.bluez import SERVICE,BlueZTransport
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.service import load_config,bootstrap_mode
from support import synthetic_store

ADDRESS='02:00:00:00:00:88'
OTHER='02:00:00:00:00:89'

class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=synthetic_store(self.temp.name,bytes(range(16)))
        self.transport=DeferredBlueZTransport()
        self.now=1000
        self.runtime=Runtime(self.store,bytes(range(16)),self.transport,clock=lambda:self.now)
        self.scanner=SimpleNamespace(discover=AsyncMock(return_value={1:(SimpleNamespace(address=ADDRESS),SimpleNamespace(service_uuids=[SERVICE],rssi=-60)),2:(SimpleNamespace(address=OTHER),SimpleNamespace(service_uuids=[],rssi=-40))}))
        self.selection=BootstrapSelection(self.runtime,scanner=self.scanner)
        self.runtime.bootstrap=self.selection
    async def asyncTearDown(self):self.store.close();self.temp.cleanup()

    async def test_deferred_connect_does_not_scan_or_connect_until_selection(self):
        with patch.object(BlueZTransport,'connect',new_callable=AsyncMock) as connect:
            task=asyncio.create_task(self.transport.connect());await asyncio.sleep(0)
            connect.assert_not_awaited()
            self.transport.select(ADDRESS);await task;connect.assert_awaited_once()

    async def test_fresh_select_persists_but_does_not_claim_or_transmit(self):
        self.assertEqual((await self.selection.scan())['candidates'],[{'address':ADDRESS,'rssi':-60}])
        result=await self.selection.select(ADDRESS,True)
        self.assertFalse(result['association_started']);self.assertEqual(self.store.get('bootstrap_relay_address'),ADDRESS)
        self.assertTrue(self.transport.selection.is_set());self.assertEqual(self.store.journals(),[])
        self.assertEqual(self.store.get('sequence'),0)
        replacement=DeferredBlueZTransport(self.store.get('bootstrap_relay_address'))
        self.assertTrue(replacement.selection.is_set());self.assertEqual(replacement.address,ADDRESS)

    async def test_no_declaration_unknown_address_and_stale_scan_refused(self):
        await self.selection.scan()
        for address,declaration in ((ADDRESS,False),(ADDRESS,1),(OTHER,True)):
            with self.assertRaises(ValueError):await self.selection.select(address,declaration)
        self.now+=30001
        with self.assertRaises(ValueError):await self.selection.select(ADDRESS,True)
        self.assertFalse(self.transport.selection.is_set())

    async def test_reselection_cannot_abandon_retained_identity(self):
        await self.selection.scan();await self.selection.select(ADDRESS,True)
        self.assertTrue((await self.selection.select(ADDRESS,True))['selected'])
        with self.assertRaises(ValueError):await self.selection.select(OTHER,True)
        with self.assertRaises(RuntimeError):await self.selection.scan()

    async def test_nonempty_or_attempted_state_blocks_before_scanning(self):
        self.store.begin('claim',40000,{'uuid':'x'})
        with self.assertRaises(ValueError):await self.selection.scan()
        self.scanner.discover.assert_not_awaited()

    async def test_nonzero_sequence_blocks_before_scanning(self):
        self.store.allocate_sequence()
        with self.assertRaises(ValueError):await self.selection.scan()
        self.scanner.discover.assert_not_awaited()

    async def test_failed_scan_invalidates_old_candidates(self):
        await self.selection.scan();self.scanner.discover.side_effect=RuntimeError('lost scan')
        with self.assertRaises(RuntimeError):await self.selection.scan()
        with self.assertRaises(ValueError):await self.selection.select(ADDRESS,True)

    async def test_status_does_not_claim_physical_identity_proof(self):
        s=self.runtime.status()['bootstrap'];self.assertTrue(s['needs_first_claim']);self.assertIsNone(s['selected_address'])

class BootstrapConfigTests(unittest.TestCase):
    def test_only_explicit_commissioning_mode_accepts_null_relay(self):
        from test_commissioning_mode import CommissioningConfigTests
        config=dict(CommissioningConfigTests().config(),first_device_bootstrap=True,relay_address=None)
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'config.json';p.write_text(json.dumps(config));self.assertTrue(load_config(p)['first_device_bootstrap'])
            for change in ({'first_device_bootstrap':'true'},{'commissioning_only':False},{'relay_address':ADDRESS}):
                p.write_text(json.dumps(dict(config,**change)))
                with self.assertRaises(ValueError):load_config(p)

class BootstrapServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_can_start_without_selecting_or_touching_radio(self):
        try:import aiohttp.web
        except ImportError:self.skipTest('HTTP dependencies tested in container')
        from avionmesh_pi.service import serve
        from avionmesh_pi.protocol import derive_mcp_key
        from test_commissioning_mode import CommissioningConfigTests
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,value in [('key','synthetic-bootstrap-service'),('token','x'*40)]:
                (root/name).write_text(value);(root/name).chmod(0o600)
            store=synthetic_store(root/'state',derive_mcp_key('synthetic-bootstrap-service'));store.close()
            config=dict(CommissioningConfigTests().config(),first_device_bootstrap=True,relay_address=None,
                state_directory=str(root/'state'),mesh_passphrase_file=str(root/'key'),api_token_file=str(root/'token'))
            started=asyncio.Event();site=AsyncMock();site.start.side_effect=started.set
            runner=AsyncMock()
            with patch('aiohttp.web.AppRunner',return_value=runner),patch('aiohttp.web.TCPSite',return_value=site),patch.object(BlueZTransport,'connect',new_callable=AsyncMock) as connect,patch('avionmesh_pi.service.MqttService',side_effect=AssertionError('No MQTT')):
                task=asyncio.create_task(serve(config))
                try:
                    await asyncio.wait_for(started.wait(),2);await asyncio.sleep(0)
                    connect.assert_not_awaited()
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):await task
                runner.cleanup.assert_awaited_once()
