import json
from pathlib import Path
import unittest
from avionmesh_pi.protocol import model_command,ping_command,plan_light_command
from avionmesh_pi.keypad import table_request,table_write

class EspContractTests(unittest.TestCase):
    def test_pinned_cpp_encoder_output_matches_python(self):
        fixture=json.loads((Path(__file__).resolve().parents[1]/'fixtures/esp-command-contracts.json').read_text())
        actual=[]
        for target in (291,40002):
            actual += [plan_light_command(target,brightness=x).hex() for x in (0,1,128,255)]
            actual += [plan_light_command(target,kelvin=x).hex() for x in (2700,5000)]
        actual += [ping_command(40001,0x81).hex(),model_command(40002,2,3,b'\x01\x23\0').hex(),model_command(40002,5,3,b'\x01\x23\0').hex()]
        actual += [table_request(40001,slot).hex() for slot in range(5)]
        actual += [table_write(40001,slot,7,action,group=291).hex() for slot in range(1,5) for action in ('delete','primary','secondary')]
        self.assertEqual(actual,fixture['wire_plaintexts'])
        self.assertEqual(len(actual),fixture['vectors'])
