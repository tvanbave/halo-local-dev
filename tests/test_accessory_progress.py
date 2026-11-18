"""Accessory prompts follow the real listener, accepted send, and owning HTTP job."""
import asyncio
import unittest
from aiohttp.test_utils import TestClient, TestServer
from avionmesh_pi.protocol import decrypt_mcp
from avionmesh_pi.server import create_app, _JobAccessoryProgress
import test_accessory as base
import test_accessory_native as native
from test_service import KEY


class PhaseLog(dict):
    def __init__(self):
        super().__init__(); self.history=[]
    def __setitem__(self,target,phase):
        self.history.append(phase);super().__setitem__(target,phase)


class AccessoryProgressTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=base.AccessoryTests.asyncSetUp
    asyncTearDown=base.AccessoryTests.asyncTearDown

    async def test_early_input_during_send_never_prompts_for_another_press(self):
        self.setup.progress=PhaseLog()
        result=await self.setup.setup(40001)
        self.assertTrue(result['native_behavior_observed'])
        self.assertEqual(self.setup.progress.history,['preparing','writing','observed'])
        self.assertEqual(self.setup.progress,{})
        self.assertEqual(len(self.transport.frames),2)

    async def test_failed_dispatch_never_prompts_and_listener_is_removed(self):
        self.setup.progress=PhaseLog();send=self.transport.send
        async def fail(frame):
            if decrypt_mcp(frame,KEY).payload[:2]==b'\0\x1b':
                self.assertTrue(self.runtime.waiters)
                self.assertTrue(self.runtime.packet_observers)
                raise RuntimeError('Synthetic dispatch failure')
            await send(frame)
        self.transport.send=fail
        with self.assertRaises(RuntimeError):await self.setup.setup(40001)
        self.assertNotIn('awaiting_input',self.setup.progress.history)
        self.assertEqual(self.setup.progress,{})
        self.assertEqual(self.runtime.waiters,[])
        self.assertEqual(self.runtime.packet_observers,[])

    async def test_timeout_clears_prompt_without_retry(self):
        self.setup.progress=PhaseLog();self.transport.native=False
        with self.assertRaises(asyncio.TimeoutError):await self.setup.setup(40001,timeout=.01)
        self.assertEqual(self.setup.progress.history,['preparing','writing','awaiting_input'])
        self.assertEqual(self.setup.progress,{})
        self.assertEqual(len(self.transport.frames),2)
        self.assertEqual(self.runtime.waiters,[])

    async def test_progress_rejects_other_task_generation_and_terminal_job(self):
        progress=_JobAccessoryProgress(self.runtime)
        task=asyncio.current_task();job={'id':'synthetic','state':'running'}
        progress[40001]='awaiting_input'
        args=(40001,job,task,'accessory_setup','instance')
        self.assertEqual(progress.for_job(*args)['job_id'],'synthetic')
        other=asyncio.create_task(asyncio.sleep(10))
        try:self.assertIsNone(progress.for_job(40001,job,other,'accessory_setup','instance'))
        finally:
            other.cancel()
            with self.assertRaises(asyncio.CancelledError):await other
        self.assertIsNone(progress.for_job(40001,job,task,'dimmer_edit','instance'))
        self.assertIsNone(progress.for_job(40001,dict(job,state='completed'),task,'accessory_setup','instance'))
        self.runtime.session()
        self.assertIsNone(progress.for_job(*args))


class AccessoryProgressHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await native.NativeAccessoryTests.asyncSetUp(self)
        self.transport.native=False
        self.ping=asyncio.Event();self.assignment=asyncio.Event()
        self.ping_started=asyncio.Event();self.assignment_started=asyncio.Event()
        send=self.transport.send
        async def gated(frame):
            p=decrypt_mcp(frame,KEY).payload
            if p[:2]==b'\x06\xff':
                self.ping_started.set();await self.ping.wait()
            elif p[:2]==b'\0\x1b':
                self.assertTrue(self.runtime.waiters)
                self.assertTrue(self.runtime.packet_observers)
                self.assignment_started.set();await self.assignment.wait()
            await send(frame)
        self.transport.send=gated
        self.client=TestClient(TestServer(create_app(self.runtime,'synthetic-test-token-01234567890123456789')))
        await self.client.start_server()
        self.headers={'Authorization':'Bearer synthetic-test-token-01234567890123456789'}

    async def asyncTearDown(self):
        self.ping.set();self.assignment.set()
        await self.client.close()
        await native.NativeAccessoryTests.asyncTearDown(self)

    async def job(self):
        response=await self.client.get('/api/snapshot',headers=self.headers)
        snapshot=await response.json()
        return snapshot['status']['jobs']['40001'],snapshot['status']['instance']

    async def begin(self,native_mode=False):
        path='/api/controller/setup-native' if native_mode else '/api/controller/setup'
        body={'target':40001,**({'group':260} if native_mode else {})}
        response=await self.client.post(path,headers=self.headers,json=body)
        self.assertEqual(response.status,202,await response.text())
        accepted=await response.json()
        await asyncio.wait_for(self.ping_started.wait(),1)
        return accepted

    async def await_input(self,accepted,operation):
        job,instance=await self.job()
        self.assertEqual(job['progress'],{'phase':'preparing','job_id':accepted['job_id'],
            'target':40001,'product_id':91,'operation':operation,'instance':instance})
        self.ping.set();await asyncio.wait_for(self.assignment_started.wait(),1)
        job,_=await self.job();self.assertEqual(job['progress']['phase'],'writing')
        self.assignment.set()
        for _ in range(20):
            job,_=await self.job()
            if job.get('progress',{}).get('phase')=='awaiting_input':break
            await asyncio.sleep(0)
        self.assertEqual(job['progress']['phase'],'awaiting_input')
        self.assertEqual(job['progress']['job_id'],accepted['job_id'])
        # Another snapshot/new tab has the same server-owned identity.
        again,_=await self.job();self.assertEqual(again['progress'],job['progress'])

    async def finish_input(self):
        self.transport.emit(b'\0\x0a'+self.transport.group+b'\x01\0\xff\0\0\0')
        for _ in range(20):
            job,_=await self.job()
            if job['state']!='running':break
            await asyncio.sleep(0)
        self.assertEqual(job['state'],'completed')
        self.assertIsNone(job['progress'])
        self.assertEqual(len(self.transport.frames),2)

    async def test_ha_prompt_arms_after_accepted_send_and_survives_snapshot_reload(self):
        accepted=await self.begin();await self.await_input(accepted,'accessory_setup')
        await self.finish_input()

    async def test_native_prompt_uses_same_correlated_listener(self):
        accepted=await self.begin(True);await self.await_input(accepted,'accessory_native_setup')
        await self.finish_input()

    async def test_session_loss_removes_prompt_and_does_not_resend(self):
        accepted=await self.begin();await self.await_input(accepted,'accessory_setup')
        self.runtime.session()
        for _ in range(20):
            job,_=await self.job()
            if job['state']!='running':break
            await asyncio.sleep(0)
        self.assertIsNone(job['progress'])
        self.assertEqual(job['state'],'needs_review')
        self.assertEqual(len(self.transport.frames),2)

    async def test_read_only_recovery_prompts_without_another_dispatch(self):
        self.ping.set();self.assignment.set()
        with self.assertRaises(asyncio.TimeoutError):
            await self.setup.setup(40001,timeout=.01)
        receipt=self.store.journals()[-1]
        self.assertEqual(receipt['status'],'uncertain')
        before=len(self.transport.frames)
        self.runtime.mesh_ready=self.transport.ready=True
        self.runtime.session()
        response=await self.client.post('/api/reconcile',headers=self.headers,
            json={'transaction':receipt['id']})
        self.assertEqual(response.status,202)
        accepted=await response.json()
        for _ in range(20):
            job,_=await self.job()
            if job.get('progress') and job['progress']['phase']=='awaiting_input':break
            await asyncio.sleep(0)
        self.assertEqual(job['progress']['operation'],'accessory_recovery')
        self.assertEqual(job['progress']['job_id'],accepted['job_id'])
        self.assertEqual(len(self.transport.frames),before)
        await self.finish_input()
        self.assertEqual(len(self.transport.frames),before)
