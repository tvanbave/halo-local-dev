import unittest
from avionmesh_pi.diagnostics import Diagnostics
from avionmesh_pi.protocol import Packet
from avionmesh_pi.controllers import ControllerRouter

class DiagnosticTests(unittest.TestCase):
    def test_owner_source_duration_and_size_bounds(self):
        now = [10]
        d = Diagnostics(lambda:now[0])
        started = d.start(40001,1)
        with self.assertRaises(RuntimeError):d.start(40002,1)
        with self.assertRaises(ValueError):d.export('wrong-owner')
        for source,opcode in ((40002,115),(40001,4)):
            d.observe(Packet(1,source,0,opcode,b'\x01\x0a\x00\xff'),10)
        self.assertEqual(d.export(started['id'])['records'],[])
        for i in range(257):d.observe(Packet(i,40001,0,115,b'\x01\x0a\x00\xff'),10)
        result = d.export(started['id'])
        self.assertEqual(len(result['records']),256)
        self.assertEqual(result['dropped'],1)
        now[0] = 1011
        self.assertTrue(d.export(started['id'])['complete'])
        self.assertFalse(result['mesh_key_included'])
        self.assertFalse(result['masp_included'])

    def test_duplicate_target_in_unknown_slot_blocks_named_activation(self):
        router = ControllerRouter()
        router.bindings[40001] = {'slots':[{'slot':1,'target':291,'flags':8,'level':255},
                                         {'slot':2,'target':291,'flags':4,'level':128}]}
        event = router.decode(Packet(1,40001,0,115,bytes.fromhex('000a012300ff00000000')),127,10)
        self.assertNotIn('button',event)
        self.assertNotIn('activation',event)

    def test_owner_session_deadline_and_mixed_local_tx_are_explicit(self):
        now=[10];d=Diagnostics(lambda:now[0])
        first=d.start(40001,1)
        command=(40001).to_bytes(2,'little')+b'\x73\x01\x0a\0\0\0'
        d.transmitted(command,1,10,False,session=d.session,owner=first['id'])
        exported=d.export(first['id'])
        self.assertFalse(exported['authenticated_rx_only'])
        self.assertTrue(exported['records'][0]['local_tx'])
        self.assertFalse(exported['records'][0]['transport_accepted'])
        self.assertFalse(exported['records'][0]['remote_acknowledged'])
        exported['records'].clear()
        self.assertEqual(len(d.export(first['id'])['records']),1)
        d.release(first['id'])
        second=d.start(40001,1)
        d.transmitted(command,2,10,True,session=d.session,owner=first['id'])
        d.transmitted(command,3,10,True,session=d.session,owner=None)
        self.assertEqual(d.export(second['id'])['records'],[])
        now[0]=1010
        d.observe(Packet(2,40001,0,115,b'\x01\x0a\0\xff'),10)
        self.assertEqual(d.export(second['id'])['records'],[])
        third=d.start(40001,1)
        d.boundary()
        self.assertEqual(d.export(third['id'])['stopped'],'session_changed')
        self.assertEqual(d.summary()['tx_failures'],1)

    def test_unexported_expired_capture_cannot_be_overwritten(self):
        now=[0];d=Diagnostics(lambda:now[0]);first=d.start(40001,1)
        now[0]=1000
        with self.assertRaises(RuntimeError):d.start(40001,1)
        d.export(first['id'])
        d.start(40001,1)
