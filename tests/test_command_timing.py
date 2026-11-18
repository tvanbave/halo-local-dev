"""Deterministic stage timing and socket-free runtime/MQTT correlation tests."""
import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.timing import CommandTimings
from avionmesh_pi.mqtt import Outbox, MqttService
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.protocol import encrypt_mcp
from support import synthetic_store as Store
from test_service import KEY, INVENTORY, FakeTransport


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.t = CommandTimings(lambda: self.now, 'avionmesh_pi/test', capacity=3)

    def start(self, target=291, members=(40002, 40003), expected=None, source='mqtt'):
        return self.t.start(target, members, expected or {'brightness':255}, source=source, received_ms=50, shared=True)

    def message(self, value=255):
        return {'topic':'avionmesh_pi/test/light/291/state','payload':{'state':'ON' if value else 'OFF','brightness':value},'retain':True,'qos':0}

    def test_stage_durations_and_no_ha_receipt_claim(self):
        i=self.start();self.t.dispatch(i);self.now=105;self.t.sent(i)
        self.t.observe(40002,{'brightness':255},150)
        self.now=150;early=self.t.publication_tokens(self.message())
        self.t.observe(40003,{'brightness':255},180)
        self.now=180;full=self.t.publication_tokens(self.message())
        self.t.published(early,150,200,230);self.t.published(full,180,250,260)
        r=self.t.summary()['recent'][0]
        self.assertEqual((r['queue_ms'],r['dispatch_ms'],r['first_report_ms'],r['all_reports_ms']),(50,5,100,130))
        self.assertEqual((r['mqtt_sent_ms'],r['mqtt_all_reports_sent_ms'],r['mqtt_queue_ms'],r['mqtt_publish_call_ms']),(180,210,50,30))
        self.assertEqual(r['members_matched'],2)
        self.assertFalse(self.t.summary()['ha_receipt_measured'])
        self.assertFalse(self.t.summary()['physical_delivery_proven'])

    def test_old_wrong_member_and_wrong_field_reports_do_not_finish(self):
        i=self.start();self.t.dispatch(i)
        for member,update,when in [(40002,{'brightness':255},99),(40004,{'brightness':255},110),(40002,{'color_temp':4000},110)]:
            self.t.observe(member,update,when)
        self.assertIsNone(self.t.summary()['recent'][0]['first_report_ms'])
        self.t.observe(40002,{'brightness':0},120)
        self.assertEqual(self.t.summary()['recent'][0]['first_report_ms'],70)
        self.assertEqual(self.t.publication_tokens(self.message(0)),())
        self.assertIsNone(self.t.summary()['recent'][0]['all_reports_ms'])

    def test_same_value_newer_dispatch_revokes_old_report_and_publish_correlation(self):
        a=self.start();self.t.dispatch(a);self.t.observe(40002,{'brightness':255},110)
        tokens=self.t.publication_tokens(self.message())
        b=self.start();self.now=120;self.t.dispatch(b)
        self.t.published(tokens,110,125,130)
        recent=self.t.summary()['recent']
        self.assertEqual(recent[1]['status'],'superseded')
        self.assertIsNone(recent[1]['mqtt_sent_ms'])
        self.assertIsNone(recent[0]['first_report_ms'])

    def test_queued_command_does_not_invalidate_an_active_command_until_dispatch(self):
        a=self.start();self.t.dispatch(a);self.start()
        self.t.observe(40002,{'brightness':255},120)
        self.assertEqual(self.t.summary()['recent'][1]['members_reported'],1)

    def test_mesh_boundary_and_expiry_prevent_late_reports(self):
        a=self.start();self.t.dispatch(a);self.t.boundary()
        self.t.observe(40002,{'brightness':255},150)
        self.assertEqual(self.t.summary()['recent'][0]['status'],'session_changed')
        self.assertEqual(self.t.summary()['recent'][0]['members_reported'],0)
        b=self.start();self.t.dispatch(b);self.now=40000
        self.t.observe(40002,{'brightness':255},40000)
        self.assertEqual(self.t.summary()['recent'][0]['status'],'expired')

    def test_failure_distinguishes_never_dispatched_from_uncertain(self):
        a=self.start();self.t.failed(a)
        b=self.start();self.t.dispatch(b);self.t.failed(b)
        self.assertEqual([x['status'] for x in self.t.summary()['recent']],['dispatch_uncertain','not_dispatched'])

    def test_capacity_copies_and_redacted_data(self):
        for _ in range(8):self.start()
        self.assertEqual(len(self.t.records),3);self.assertEqual(self.t.summary()['overwritten'],5)
        result=self.t.summary();result['recent'].clear()
        self.assertEqual(len(self.t.summary()['recent']),3)
        self.assertNotIn('expected',json.dumps(self.t.summary()))
        self.assertNotIn('reports"',json.dumps(self.t.summary()))

    def test_compound_reports_wait_for_both_fields_from_each_member(self):
        i=self.start(expected={'brightness':255,'color_temp':4000});self.t.dispatch(i)
        for member in (40002,40003):self.t.observe(member,{'brightness':255},110)
        self.t.observe(40002,{'color_temp':4000},120)
        self.assertIsNone(self.t.summary()['recent'][0]['all_reports_ms'])
        self.t.observe(40003,{'color_temp':4000},130)
        self.assertEqual(self.t.summary()['recent'][0]['all_reports_ms'],80)

    def test_native_relative_input_measures_reports_without_claiming_bridge_send(self):
        i=self.start(expected={'brightness':None},source='native');self.t.dispatch(i);self.t.sent(i)
        for member in (40002,40003):self.t.observe(member,{'brightness':121},110)
        self.assertTrue(self.t.publication_tokens(self.message(121)))
        r=self.t.summary()['recent'][0]
        self.assertIsNone(r['queue_ms']);self.assertIsNone(r['dispatch_ms'])
        self.assertEqual(r['members_matched'],2)

    def test_native_reports_queued_before_processing_use_receive_time_boundary(self):
        self.now=120
        i=self.t.start(291,[40002],{'brightness':255},source='native',received_ms=100)
        self.t.dispatch(i);self.t.sent(i)
        self.t.observe(40002,{'brightness':255},110)
        self.assertEqual(self.t.summary()['recent'][0]['first_report_ms'],10)
        self.assertEqual(self.t.summary()['recent'][0]['all_reports_ms'],10)

    def test_outbox_replacement_freezes_newest_metadata_and_never_alters_message(self):
        b=Outbox(lambda:self.now,timings=self.t);b.connection(True)
        i=self.start();self.t.dispatch(i);self.t.observe(40002,{'brightness':255},110)
        first=self.message();b.put(first)
        self.now=200;self.t.observe(40003,{'brightness':255},200)
        second=self.message();b.put(second)
        self.assertIs(b.take(),second);self.assertEqual(b.delivery,(200,((i,True),)))
        self.assertEqual(set(second),{'topic','payload','retain','qos'})
        self.assertEqual(b.state_delivery,{})
        b.put(second);b.connection(False)
        self.assertEqual(b.state_delivery,{})


class RuntimeTimingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name,KEY)
        self.store.import_inventory(INVENTORY);self.now=100
        self.transport=FakeTransport();self.runtime=Runtime(self.store,KEY,self.transport,clock=lambda:self.now)
        self.transport.receiver=lambda frame, _:self.runtime.observe(frame,self.now)
        self.runtime.mesh_ready=self.runtime.mqtt_ready=True;self.runtime.session()

    async def asyncTearDown(self):
        self.store.close();self.temp.cleanup()

    async def test_real_control_and_readback_measure_without_extra_frames(self):
        result=await self.runtime.control(40002,{'brightness':128},local=True,received_ms=60)
        self.assertTrue(result['confirmed']);self.assertEqual(len(self.transport.frames),2)
        t=self.runtime.status()['command_timings']['recent'][0]
        self.assertEqual(t['source'],'ui');self.assertEqual(t['queue_ms'],40)
        self.assertEqual(t['members_matched'],1)

    async def test_invalid_control_has_no_timing_or_radio_work(self):
        with self.assertRaises(ValueError):await self.runtime.control(999,{'state':'ON'})
        self.assertEqual(self.runtime.command_timings.summary()['recent'],[])
        self.assertEqual(self.transport.frames,[])

    async def test_failed_final_admission_never_marks_dispatch(self):
        def refuse():raise RuntimeError('synthetic refusal')
        with self.assertRaises(RuntimeError):await self.runtime.control(40002,{'state':'ON'},admission=refuse)
        r=self.runtime.command_timings.summary()['recent'][0]
        self.assertEqual(r['status'],'not_dispatched');self.assertIsNone(r['queue_ms'])
        self.assertEqual(self.transport.frames,[])

    async def test_publish_completion_is_measured_but_not_sent_in_payload(self):
        mqtt=MqttService(self.runtime,{'host':'synthetic.invalid','tls':False});mqtt._connection(True)
        self.runtime.sink=mqtt.outbox.put
        await self.runtime.control(40002,{'brightness':128},confirm=False,received_ms=70)
        self.now=200;sent=[]
        class Client:
            async def publish(inner,topic,payload,**kwargs):
                sent.append((topic,json.loads(payload) if payload.startswith('{') else payload))
                self.now+=20
        task=asyncio.create_task(mqtt.publish_loop(Client()))
        try:
            for _ in range(50):
                if self.runtime.command_timings.summary()['recent'][0]['mqtt_sent_ms'] is not None:break
                await asyncio.sleep(0)
            t=self.runtime.command_timings.summary()['recent'][0]
            self.assertIsNotNone(t['mqtt_sent_ms']);self.assertEqual(t['mqtt_publish_call_ms'],20)
            self.assertTrue(sent)
            self.assertNotIn('timing',json.dumps(sent));self.assertNotIn('queue_ms',json.dumps(sent))
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_publish_failure_never_records_success_and_is_not_requeued(self):
        mqtt=MqttService(self.runtime,{'host':'synthetic.invalid','tls':False});mqtt._connection(True)
        self.runtime.sink=mqtt.outbox.put
        await self.runtime.control(40002,{'brightness':128},confirm=False)
        calls=[]
        class Client:
            async def publish(inner,topic,payload,**kwargs):calls.append(topic);raise RuntimeError('offline')
        with self.assertRaises(RuntimeError):await mqtt.publish_loop(Client())
        self.assertIsNone(self.runtime.command_timings.summary()['recent'][0]['mqtt_sent_ms'])
        self.assertEqual(len(calls),1)
