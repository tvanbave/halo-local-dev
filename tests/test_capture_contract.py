import json
from pathlib import Path
import unittest
from avionmesh_pi.diagnostics import Diagnostics
from avionmesh_pi.protocol import Packet

class CaptureContractTests(unittest.TestCase):
    def test_pinned_cpp_shared_capture_fields(self):
        reference=json.loads((Path(__file__).parents[1]/'fixtures/esp-capture-contract.json').read_text())
        d=Diagnostics(lambda:10);owner=d.start(40001,1);payload=bytes([1,10,0,255])
        d.observe(Packet(7,40001,32768,115,payload),10)
        d.observe(Packet(8,40002,32768,115,payload),10)
        d.transmitted((40001).to_bytes(2,'little')+b'\x73'+payload,9,10,False,session=d.session,owner=owner['id'])
        d.export(owner['id'],stop=True)
        d.observe(Packet(10,40001,32768,115,payload),10)
        fields=('capture_index','sequence','local_tx','transport_accepted','source','address','opcode','original_length')
        actual=[[int(row[f] or 0) for f in fields] for row in d.export(owner['id'])['records']]
        self.assertEqual(actual,reference['records'])
