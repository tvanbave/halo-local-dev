"""Synthetic ESP MTL timeout parity and complete MASP exchange over fake GATT."""
import asyncio
import unittest
from avionmesh_pi.bluez import BlueZTransport, LOW, HIGH
from avionmesh_pi.association import Association, envelope, open_envelope
from avionmesh_pi.transport import FragmentAssembler
from test_service import FakeClient, FakeScanner
from test_association import Peer, IDENTITY, KEY

class PollTests(unittest.TestCase):
    def test_low_timeout_matches_esp_and_is_delivered_once(self):
        a=FragmentAssembler();frame=envelope(b'\x03'+bytes(4)+b'\0')
        a.feed('low',frame,10)
        self.assertIsNone(a.poll(209));self.assertEqual(a.poll(210),frame)
        self.assertIsNone(a.poll(211));self.assertEqual(a.dropped,0)
        self.assertEqual(a.feed('high',b'next',211),b'next')
    def test_pair_before_timeout_cancels_pending_delivery(self):
        a=FragmentAssembler();a.feed('low',bytes(20),0)
        self.assertEqual(a.feed('high',bytes(3),199),bytes(23))
        self.assertIsNone(a.poll(200))
    def test_old_partial_flush_is_not_spliced_into_later_reply(self):
        a=FragmentAssembler();a.feed('low',bytes(20),0)
        self.assertEqual(a.poll(200),bytes(20))
        with self.assertRaises(ValueError):open_envelope(bytes(20))
        valid=envelope(b'\x03'+bytes(4)+b'\0')
        self.assertEqual(a.feed('high',valid,200),valid)
    def test_poll_clock_reversal_discards_pending(self):
        a=FragmentAssembler();a.feed('low',b'part',10)
        with self.assertRaises(ValueError):a.poll(9)
        self.assertIsNone(a.poll(210))

class BluetoothMTLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now=1000
        self.t=BlueZTransport('00:11:22:33:44:55',scanner=FakeScanner,client_factory=FakeClient,clock=lambda:self.now)
        await self.t.connect()
    async def asyncTearDown(self):await self.t.disconnect()
    def notify(self,char,data):self.t.client.notifications[char](None,data)
    def expire(self):
        pending=self.t.assembler.pending;self.t._cancel_low_timer();self.now+=200
        self.t._poll_low(self.t.client,pending)
    async def test_real_timer_flushes_without_another_notification(self):
        import time
        self.t.clock=lambda:int(time.monotonic()*1000)
        frame=envelope(b'\x03'+bytes(4)+b'\0');before=self.t.clock()
        self.notify(LOW,frame)
        result,observed=await asyncio.wait_for(self.t.queue.get(),1)
        self.assertEqual(result,frame);self.assertLess(observed-before,100)
        self.assertGreaterEqual(self.t.clock()-observed,200)
        self.assertEqual(self.t.counts['standalone_low'],1)
    async def test_complete_crypto_exchange_short_replies_on_low(self):
        for confirm in (0,1):
            protocol=Association(IDENTITY,40001,KEY);peer=Peer(confirm)
            request=protocol.start();sent=0
            while request:
                await self.t.send(request);sent+=1
                reply=peer.respond(request)
                if len(reply)<=20:
                    self.notify(LOW,reply);self.expire()
                else:
                    self.notify(LOW,reply[:20]);self.now+=10;self.notify(HIGH,reply[20:])
                frame,when=await asyncio.wait_for(self.t.queue.get(),1)
                request=protocol.process(frame)
            self.assertEqual(protocol.state,'complete');self.assertEqual(sent,12)
            self.assertEqual(peer.target,40001)
        self.assertGreater(self.t.counts['standalone_low'],0)
    async def test_delayed_timer_flushes_before_new_notification(self):
        frame=envelope(b'\x03'+bytes(4)+b'\0')
        self.notify(LOW,frame);self.now+=250;self.notify(HIGH,frame)
        self.assertEqual(self.t.queue.get_nowait(),(frame,1000))
        self.assertEqual(self.t.queue.get_nowait(),(frame,1250))
        self.assertIsNone(self.t._low_timer)
    async def test_low_replacement_only_delivers_new_packet(self):
        self.notify(LOW,b'old');self.now+=1;self.notify(LOW,b'new');self.expire()
        self.assertEqual(self.t.queue.get_nowait(),(b'new',1001))
        self.assertTrue(self.t.queue.empty());self.assertEqual(self.t.counts['replaced_low'],1)
    async def test_disconnect_and_old_timer_cannot_inject_into_new_connection(self):
        old=self.t.client;self.notify(LOW,b'old');pending=self.t.assembler.pending
        await self.t.disconnect();self.assertIsNone(self.t._low_timer)
        await self.t.connect();self.now+=300;self.t._poll_low(old,pending)
        self.assertTrue(self.t.queue.empty());self.assertTrue(self.t.ready)
    async def test_timer_queue_overflow_closes_path_without_more_delivery(self):
        for _ in range(64):self.notify(HIGH,b'x')
        self.notify(LOW,b'y');self.expire()
        self.assertFalse(self.t.ready);self.assertTrue(self.t.closed.is_set())
        self.assertEqual(self.t.counts['queue_drop'],1)
        self.notify(HIGH,b'z');self.assertEqual(self.t.counts['queue_drop'],1)
