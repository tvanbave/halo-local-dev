import asyncio
import tempfile
import unittest
from avionmesh_pi.keypad import KeypadService
from avionmesh_pi.onboarding import KeypadOnboarding
from avionmesh_pi.protocol import encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_editor import EditorTransport
from test_service import KEY

class BatteryTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_wakes_and_cooldowns_guard_every_table_round(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory,KEY)
            try:
                store.import_inventory({'devices':[{'device_id':40001,'product_id':126,'name':'Synthetic battery keypad','exposed':False}],'groups':[]})
                now=[10];wake_times=[]
                transport=EditorTransport()
                runtime=Runtime(store,KEY,transport,clock=lambda:now[0])
                transport.runtime=runtime
                runtime.mesh_ready=runtime.mqtt_ready=True
                runtime.capabilities={'battery_onboarding':True,'inventory_complete':True}
                runtime.session()
                async def simulated_sleep(seconds):
                    now[0]+=round(seconds*1000)+1
                    if keypad.progress.get(40001) == 'settling':
                        return
                    def wake():
                        wake_times.append(now[0])
                        runtime.observe(encrypt_mcp(b'\0\x80\x73\x0b\x1c'+bytes(5),KEY,transport.sequence,40001),now[0])
                        transport.sequence+=1
                    asyncio.get_running_loop().call_soon(wake)
                keypad=KeypadService(runtime,sleeper=simulated_sleep)
                result=await KeypadOnboarding(runtime,keypad).setup(40001)
                self.assertTrue(result['ready'])
                self.assertEqual(len(transport.writes),6)
                self.assertGreaterEqual(len(wake_times),8)
                self.assertTrue(all(b-a>=20000 for a,b in zip(wake_times,wake_times[1:])))
            finally:store.close()
