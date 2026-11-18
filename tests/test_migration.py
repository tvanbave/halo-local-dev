import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.storage import Store
from avionmesh_pi.migration import admit, retire, status
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.commissioning import Commissioning
from support import declaration
from test_service import KEY, FakeTransport

class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(self.temp.name,KEY)
    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_unadmitted_state_blocks_all_transmit_paths(self):
        t=FakeTransport()
        r=Runtime(self.store,KEY,t)
        r.mesh_ready=True
        for operation in (lambda:r.send(bytes(13)),lambda:Commissioning(r).discover(0),
                          lambda:Commissioning(r).claim('01'*16,'Test',134,inventory_complete=True)):
            with self.assertRaises(RuntimeError):await operation()
        self.assertEqual(t.frames,[])
        self.assertEqual(self.store.get('sequence'),0)

    async def test_history_floor_is_inclusive_durable_and_idempotent(self):
        doc=declaration(self.store,'verified_history',123456)
        admit(self.store,doc)
        self.assertEqual(self.store.allocate_sequence(),123457)
        self.store.close()
        self.store=Store(self.temp.name,KEY)
        admit(self.store,doc)
        self.assertEqual(self.store.allocate_sequence(),123458)
        with self.assertRaises(ValueError):admit(self.store,declaration(self.store,'verified_history',5))
        with self.assertRaises(ValueError):admit(self.store,declaration(self.store))
        self.assertEqual(self.store.get('sequence'),123458)

    async def test_atomic_failure_cannot_lower_floor_or_admit(self):
        original=self.store.set
        def fail(key,value):
            if key=='tx_admission':raise OSError('injected disk failure')
            return original(key,value)
        with patch.object(self.store,'set',side_effect=fail):
            with self.assertRaises(OSError):admit(self.store,declaration(self.store,'verified_history',100))
        self.assertEqual(self.store.get('sequence'),0)
        with self.assertRaises(RuntimeError):self.store.allocate_sequence()

    async def test_wrong_identity_types_attestation_or_exhaustion_fail_closed(self):
        doc=declaration(self.store,'verified_history',12)
        for key,value in [('source',True),('source',32769),('state_id','foreign'),('mesh_fingerprint','0'*64),
                          ('history_complete',False),('evidence_kind','last_esp_sequence'),('previous_writers_stopped',False),('evidence_sha256','unknown'),
                          ('high_water_mark',True),('high_water_mark',0xffffff),('fresh_mesh',True)]:
            changed=dict(doc);changed[key]=value
            with self.assertRaises(ValueError):admit(self.store,changed)
        self.assertFalse(status(self.store)['admitted'])

    async def test_retirement_precedes_export_and_cannot_be_undone(self):
        admit(self.store,declaration(self.store))
        self.assertEqual(self.store.allocate_sequence(),1)
        receipt=retire(self.store)
        self.assertEqual(receipt['high_water_mark'],1)
        self.store.close();self.store=Store(self.temp.name,KEY)
        self.assertEqual(retire(self.store),receipt)
        with self.assertRaises(RuntimeError):self.store.allocate_sequence()
        with self.assertRaises(ValueError):admit(self.store,declaration(self.store,'verified_history',1000))
        self.assertNotIn(KEY.hex(),json.dumps(receipt))

    async def test_last_sequence_used_once_without_wrap(self):
        admit(self.store,declaration(self.store,'verified_history',0xfffffe))
        self.assertEqual(self.store.allocate_sequence(),0xffffff)
        with self.assertRaises(RuntimeError):self.store.allocate_sequence()

    async def test_cli_status_admission_and_retirement_never_open_transport(self):
        import contextlib
        import io
        from pathlib import Path
        from avionmesh_pi.__main__ import main
        from avionmesh_pi.protocol import derive_mcp_key
        self.store.close()
        self.temp.cleanup()
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name);secret=root/'passphrase';secret.write_text('synthetic-new-mesh');secret.chmod(0o600)
        self.store=Store(root/'state',derive_mcp_key('synthetic-new-mesh'))
        doc=declaration(self.store);document=root/'history.json';document.write_text(json.dumps(doc))
        self.store.close()
        arguments=['--state-directory',str(root/'state'),'--mesh-passphrase-file',str(secret)]
        with patch('avionmesh_pi.service.BlueZTransport') as transport:
            for command,extra in [('sequence-status',[]),('sequence-admit',[str(document)]),('sequence-retire',[])]:
                output=io.StringIO()
                with contextlib.redirect_stdout(output):self.assertEqual(main([command,*arguments,*extra]),0)
                result=json.loads(output.getvalue())
                self.assertNotIn('synthetic-new-mesh',output.getvalue())
            self.assertTrue(result['retired']);transport.assert_not_called()
        self.store=Store(root/'state',derive_mcp_key('synthetic-new-mesh'))

    async def test_service_rejects_unadmitted_state_before_any_adapter_construction(self):
        import importlib.util
        if importlib.util.find_spec('aiohttp') is None:self.skipTest('Live-entry gate tested offline in container')
        from pathlib import Path
        from avionmesh_pi.service import serve
        from avionmesh_pi.protocol import derive_mcp_key
        root=Path(self.temp.name);secret=root/'secret';secret.write_text('synthetic');secret.chmod(0o600)
        token=root/'token';token.write_text('x'*40);token.chmod(0o600)
        config={'state_directory':str(root/'unadmitted'),'mesh_passphrase_file':str(secret),'api_token_file':str(token)}
        with patch('avionmesh_pi.service.BlueZTransport') as transport,patch('avionmesh_pi.service.MqttService') as broker:
            with self.assertRaises(RuntimeError):await serve(config)
            transport.assert_not_called();broker.assert_not_called()
