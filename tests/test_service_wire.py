import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.bluez import BlueZTransport
from avionmesh_pi.protocol import derive_mcp_key,decrypt_mcp
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.service import serve
from support import synthetic_store
from test_service import INVENTORY
from wire_lab import WireBroker,Relay

ISOLATED=os.environ.get('AVIONMESH_ISOLATED_TESTS')=='1'
DEPENDENCIES=all(importlib.util.find_spec(m) is not None for m in ('aiohttp','aiomqtt'))

async def eventually(condition,seconds=5):
    async def wait():
        while not condition():await asyncio.sleep(.01)
    await asyncio.wait_for(wait(),seconds)

class LoopExceptionGuard:
    """Keep callback errors visible to unittest, including fixture cleanup errors."""
    def __init__(self,loop):
        self.loop=loop
        self.previous=loop.get_exception_handler()
        self.contexts=[]
        loop.set_exception_handler(self.capture)
    def capture(self,loop,context):
        self.contexts.append(dict(context))
    async def finish(self):
        try:
            # Resource cleanups run first. Drain callbacks they scheduled as well.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            self.loop.set_exception_handler(self.previous)
        if self.contexts:
            details=['{}: {!r}'.format(c.get('message','Event-loop error'),c.get('exception'))
                     for c in self.contexts]
            raise AssertionError('Unexpected event-loop exception contexts:\n'+'\n'.join(details))

async def stop_service_task(task):
    task.cancel()
    result,=await asyncio.gather(task,return_exceptions=True)
    if isinstance(result,BaseException) and not isinstance(result,asyncio.CancelledError):
        raise result

class LoopExceptionGuardTests(unittest.TestCase):
    def test_callback_errors_fail_even_during_cleanup_and_restore_handler(self):
        for during_cleanup in (False,True):
            with self.subTest(during_cleanup=during_cleanup):
                observed=[]
                class Probe(unittest.IsolatedAsyncioTestCase):
                    async def asyncSetUp(self):
                        loop=asyncio.get_running_loop()
                        previous=lambda loop,context:None
                        loop.set_exception_handler(previous)
                        self.addCleanup(lambda:observed.append(('restored',loop.get_exception_handler() is previous)))
                        guard=LoopExceptionGuard(loop)
                        self.addAsyncCleanup(guard.finish)
                        async def resource_cleanup():
                            observed.append('resource_closed')
                            if during_cleanup:loop.call_soon(self.bad_callback)
                        self.addAsyncCleanup(resource_cleanup)
                    def bad_callback(self):
                        raise OSError('synthetic closed-socket callback')
                    async def runTest(self):
                        if not during_cleanup:
                            asyncio.get_running_loop().call_soon(self.bad_callback)
                            await asyncio.sleep(0)
                result=unittest.TestResult()
                Probe().run(result)
                self.assertEqual(len(result.failures),1,result.errors)
                self.assertEqual(result.errors,[])
                self.assertIn('synthetic closed-socket callback',result.failures[0][1])
                self.assertEqual(observed,['resource_closed',('restored',True)])

    def test_setup_failure_still_cleans_resources_and_restores_handler(self):
        observed=[]
        class Probe(unittest.IsolatedAsyncioTestCase):
            async def asyncSetUp(self):
                loop=asyncio.get_running_loop()
                previous=loop.get_exception_handler()
                self.addCleanup(lambda:observed.append(('restored',loop.get_exception_handler() is previous)))
                self.addAsyncCleanup(LoopExceptionGuard(loop).finish)
                async def resource_cleanup():observed.append('resource_closed')
                self.addAsyncCleanup(resource_cleanup)
                raise RuntimeError('synthetic partial setup failure')
            async def runTest(self):observed.append('must_not_run')
        result=unittest.TestResult()
        Probe().run(result)
        self.assertEqual(len(result.errors),1,result.failures)
        self.assertEqual(result.failures,[])
        self.assertIn('synthetic partial setup failure',result.errors[0][1])
        self.assertEqual(observed,['resource_closed',('restored',True)])

@unittest.skipUnless(ISOLATED and DEPENDENCIES,'Requires explicit network:none loopback laboratory')
class WholeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Registered first, therefore checked last, after every resource cleanup.
        self.addAsyncCleanup(LoopExceptionGuard(asyncio.get_running_loop()).finish)
        self.assertIn('lo',{name for _,name in socket.if_nameindex()})
        active=[name for _,name in socket.if_nameindex() if name!='lo' and int(Path('/sys/class/net',name,'flags').read_text(),16)&1]
        self.assertEqual(active,[],'Laboratory refuses an active non-loopback interface')
        from aiohttp import ClientSession
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.key=derive_mcp_key('synthetic-wire-lab');self.relay=Relay(self.key)
        self.broker=WireBroker();await self.broker.start()
        self.addAsyncCleanup(self.broker.close)
        self.prefix='avionmesh_pi/wire_lab';self.token='synthetic-api-token-'+('x'*32)
        for name,value in [('key','synthetic-wire-lab'),('token',self.token)]:
            path=self.root/name;path.write_text(value);path.chmod(0o600)
        store=synthetic_store(self.root/'state',self.key);store.import_inventory(INVENTORY);store.close()
        self.runtimes=[];self.sites=[]
        def runtime(*args,**kwargs):
            result=Runtime(*args,**kwargs);self.runtimes.append(result);return result
        def transport(*args,**kwargs):return BlueZTransport('00:11:22:33:44:55',scanner=self.relay,client_factory=self.relay.client)
        from aiohttp import web
        original=web.TCPSite
        def site(*args,**kwargs):
            result=original(*args,**kwargs);self.sites.append(result);return result
        self.patches=[patch('avionmesh_pi.service.Runtime',side_effect=runtime),patch('avionmesh_pi.service.BlueZTransport',side_effect=transport),patch('aiohttp.web.TCPSite',side_effect=site)]
        for p in self.patches:
            p.start();self.addCleanup(p.stop)
        self.config={'relay_address':'00:11:22:33:44:55','node':'wire_lab','state_directory':str(self.root/'state'),
            'mesh_passphrase_file':str(self.root/'key'),'api_token_file':str(self.root/'token'),
            'http_host':'127.0.0.1','http_port':0,'mqtt':{'host':'127.0.0.1','port':self.broker.port,'tls':False}}
        # Retained-on-subscribe command must not cause radio traffic.
        self.broker.retained[self.prefix+'/light/40002/set']=b'{"brightness":199}'
        self.task=asyncio.create_task(serve(self.config))
        self.addAsyncCleanup(stop_service_task,self.task)
        await eventually(lambda:self.runtimes and self.runtimes[-1].mesh_ready and self.runtimes[-1].mqtt_ready)
        self.runtime=self.runtimes[-1]
        port=self.sites[-1]._server.sockets[0].getsockname()[1]
        self.http=ClientSession(base_url='http://127.0.0.1:'+str(port),headers={'Authorization':'Bearer '+self.token})
        self.addAsyncCleanup(self.http.close)
        await eventually(lambda:'homeassistant/light/wire_lab_40002/config' in self.broker.retained)
    async def test_actual_mqtt_http_crypto_and_discovery_round_trip(self):
        self.assertEqual(self.relay.frames,[])
        discovery=json.loads(self.broker.retained['homeassistant/light/wire_lab_40002/config'])
        self.assertEqual(discovery['command_topic'],self.prefix+'/light/40002/set')
        await self.broker.publish(discovery['command_topic'],'{"brightness":123}')
        state_topic=self.prefix+'/light/40002/state'
        await eventually(lambda:state_topic in self.broker.retained and json.loads(self.broker.retained[state_topic])['brightness']==123)
        async with self.http.post('/api/control',json={'target':40002,'values':{'brightness':77,'color_temp':3500}}) as response:
            self.assertEqual(response.status,200);self.assertTrue((await response.json())['confirmed'])
        self.assertEqual((self.relay.brightness,self.relay.kelvin),(77,3500))
        sequences=[decrypt_mcp(f,self.key).sequence for f in self.relay.frames]
        self.assertEqual(sequences,sorted(set(sequences)))
    async def test_duplicate_packets_authentication_and_broker_reconnect(self):
        topic=self.prefix+'/controller/40001/input/on'
        frame=self.relay.emit(40001,bytes.fromhex('000a012300ff00000000'),repeat=3)
        await eventually(lambda:any(t==topic for t,p,r in self.broker.messages))
        self.assertEqual(sum(t==topic for t,p,r in self.broker.messages),1)
        relayed=frame[:-1]+bytes([254]);self.relay.deliver(relayed)
        await asyncio.sleep(.05)
        self.assertEqual(sum(t==topic for t,p,r in self.broker.messages),1)
        corrupted=bytearray(frame);corrupted[-2]^=1;self.relay.deliver(bytes(corrupted))
        await eventually(lambda:self.runtime.core.counts['invalid_frame']>0)
        connections=self.broker.connects;await self.broker.drop()
        await eventually(lambda:self.broker.connects>connections and self.runtime.mqtt_ready)
        self.assertEqual(sum(t==topic for t,p,r in self.broker.messages),1)
        self.assertEqual(self.relay.frames,[])  # Retained command is rejected again.
        await eventually(lambda:sum(t=='homeassistant/light/wire_lab_40002/config' for t,p,r in self.broker.messages)>=2)
        previous=sum(t=='homeassistant/light/wire_lab_40002/config' for t,p,r in self.broker.messages)
        await self.broker.publish('homeassistant/status','online')
        await eventually(lambda:sum(t=='homeassistant/light/wire_lab_40002/config' for t,p,r in self.broker.messages)>previous)
    async def test_partial_gatt_failure_reconnect_and_http_auth_bounds(self):
        self.relay.fail_high=True
        async with self.http.post('/api/control',json={'target':40002,'values':{'brightness':88}}) as response:
            self.assertEqual(response.status,409)
        await eventually(lambda:len(self.relay.clients)>=2 and self.runtime.mesh_ready)
        async with self.http.post('/api/control',json={'target':40002,'values':{'brightness':89}}) as response:
            self.assertTrue((await response.json())['confirmed'])
        self.assertEqual(self.relay.brightness,89)
        before=len(self.relay.frames)
        async with self.http.post('/api/control',headers={'Authorization':'Bearer wrong'},json={'target':40002,'values':{'state':'OFF'}}) as response:self.assertEqual(response.status,401)
        async with self.http.post('/api/control',headers={'Origin':'https://foreign.invalid'},json={'target':40002,'values':{'state':'OFF'}}) as response:self.assertEqual(response.status,403)
        self.assertEqual(len(self.relay.frames),before)
    async def test_group_hide_clears_retained_ha_discovery(self):
        topic='homeassistant/light/wire_lab_group_291/config'
        await eventually(lambda:topic in self.broker.retained)
        async with self.http.get('/api/snapshot') as response:revision=(await response.json())['status']['inventory_revision']
        async with self.http.post('/api/metadata',json={'operation':'update','revision':revision,'kind':'group','target':291,'name':'Hidden','exposed':False}) as response:self.assertEqual(response.status,200)
        await eventually(lambda:topic not in self.broker.retained,2)
