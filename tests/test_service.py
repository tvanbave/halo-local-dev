import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from avionmesh_pi.bluez import BlueZTransport, HIGH, LOW
from avionmesh_pi.controllers import ControllerRouter, named_inputs
from avionmesh_pi.keypad import KeypadService, plan_edit, snapshot
from avionmesh_pi.mqtt import MqttService, Outbox
from avionmesh_pi.protocol import Packet, decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store

KEY = bytes(range(16))
INVENTORY = {"devices": [
    {"device_id": 40001, "product_id": 127, "name": "Keypad", "exposed": True},
    {"device_id": 40002, "product_id": 134, "name": "A19", "exposed": True}],
    "groups": [{"group_id": 291, "name": "Group", "members": [40002], "exposed": True, "protected": False},
               {"group_id": 292, "name": "Reserved", "members": [], "exposed": False, "protected": True}]}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_sequences_survive_reopen_and_do_not_wrap(self):
        self.assertEqual(self.store.allocate_sequence(), 1)
        self.store.close()
        self.store = Store(self.temp.name, KEY)
        self.assertEqual(self.store.allocate_sequence(), 2)
        self.store.set("sequence", 0xffffff)
        with self.assertRaises(RuntimeError):
            self.store.allocate_sequence()
        self.assertEqual(self.store.get("sequence"), 0xffffff)

    def test_attempt_is_retained_as_uncertain_on_restart(self):
        identifier = self.store.begin("editor", 40001, {"old": "synthetic"})
        self.store.transition(identifier, "prepared", "attempted")
        self.store.close()
        self.store = Store(self.temp.name, KEY)
        self.assertEqual(self.store.journals()[0]["status"], "uncertain")
        with self.assertRaises(ValueError):
            self.store.transition(identifier, "prepared", "attempted")
        with self.assertRaises(ValueError):
            self.store.begin("editor", 40001, {})

    def test_key_mismatch_and_private_permissions(self):
        self.store.close()
        with self.assertRaises(ValueError):
            Store(self.temp.name, bytes(16))
        self.store = Store(self.temp.name, KEY)
        self.assertEqual((Path(self.temp.name) / "bridge.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_second_service_cannot_share_state(self):
        with self.assertRaises(RuntimeError):
            Store(self.temp.name, KEY)

    def test_protected_targets_and_pending_operations_block_import(self):
        self.store.import_inventory(INVENTORY)
        altered = json.loads(json.dumps(INVENTORY))
        altered["groups"][1]["members"] = [40002]
        with self.assertRaises(ValueError):
            self.store.import_inventory(altered)
        altered["groups"] = altered["groups"][:1]
        with self.assertRaises(ValueError):
            self.store.import_inventory(altered)
        self.store.begin("test", 40001, {})
        with self.assertRaises(ValueError):
            self.store.import_inventory(INVENTORY)

    def test_transaction_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.set("test", 1)
                raise RuntimeError("Injected failure")
        self.assertIsNone(self.store.get("test"))


class FakeClient:
    def __init__(self, device, disconnected_callback, **kwargs):
        self.disconnected = disconnected_callback
        self.is_connected = False
        self.services = self
        self.notifications = {}
        self.writes = []
        self.fail = False
        if kwargs.get('bluez') != {'adapter': 'hci0'}:
            raise AssertionError('Expected Bleak 3 BlueZ adapter argument')
    def get_characteristic(self, char):
        return char
    async def connect(self):
        self.is_connected = True
    async def disconnect(self):
        self.is_connected = False
        self.disconnected(self)
    async def start_notify(self, char, callback):
        self.notifications[char] = callback
    async def write_gatt_char(self, char, data, response):
        self.writes.append((char, data))
        if self.fail and char == HIGH:
            raise RuntimeError("Synthetic write failure")


class FakeScanner:
    @staticmethod
    async def find_device_by_address(address, **kwargs):
        if kwargs.get('bluez') != {'adapter': 'hci0'}:
            raise AssertionError('Expected Bleak 3 BlueZ adapter argument')
        return object()


class BlueZTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ble = BlueZTransport("00:11:22:33:44:55", scanner=FakeScanner, client_factory=FakeClient, clock=lambda: 10)
        await self.ble.connect()
    async def asyncTearDown(self):
        await self.ble.disconnect()

    async def test_subscribe_before_ready_and_fragment_send(self):
        self.assertEqual(set(self.ble.client.notifications), {LOW, HIGH})
        frame = bytes(range(27))
        await self.ble.send(frame)
        self.assertEqual(self.ble.client.writes, [(LOW, frame[:20]), (HIGH, frame[20:])])

    async def test_notification_assembly(self):
        callbacks = self.ble.client.notifications
        callbacks[LOW](None, bytes(20))
        callbacks[HIGH](None, bytes(7))
        self.assertEqual(await self.ble.queue.get(), (bytes(27), 10))
        self.assertEqual(dict(self.ble.counts),{'low':1,'high':1,'paired_high':1,'completed_frames':1})

    async def test_partial_write_failure_disconnects_without_retry(self):
        client = self.ble.client
        client.fail = True
        with self.assertRaises(RuntimeError):
            await self.ble.send(bytes(27))
        self.assertEqual(len(client.writes), 2)
        self.assertFalse(self.ble.ready)
        with self.assertRaises(RuntimeError):
            await self.ble.send(bytes(27))

    async def test_notification_overflow_stops_readiness(self):
        callback = self.ble.client.notifications[HIGH]
        for _ in range(65):
            callback(None, bytes(17))
        self.assertEqual(self.ble.dropped, 1)
        self.assertEqual(self.ble.counts['queue_drop'],1)
        self.assertFalse(self.ble.ready)

    async def test_failed_disconnect_blocks_reconnect_until_confirmed(self):
        client = self.ble.client
        async def fail_close():
            raise RuntimeError("Injected disconnect failure")
        client.disconnect = fail_close
        await self.ble.disconnect()
        self.assertFalse(self.ble.ready)
        self.assertIs(self.ble.client, client)
        with self.assertRaises(RuntimeError):
            await self.ble.connect()
        client.is_connected = False
        client.disconnected(client)
        await self.ble.connect()
        self.assertTrue(self.ble.ready)
        self.assertIsNot(self.ble.client, client)

    async def test_previous_connection_callbacks_cannot_affect_new_session(self):
        client = self.ble.client
        notify = client.notifications[HIGH]
        await self.ble.disconnect()
        await self.ble.connect()
        client.disconnected(client)
        notify(None, bytes(17))
        self.assertTrue(self.ble.ready)
        self.assertTrue(self.ble.queue.empty())


class FakeTransport:
    async def disconnect(self):
        self.ready = False
    ready = True
    dropped = 0
    def __init__(self):
        self.frames = []
        self.receiver = None
        self.sequence = 1
        self.fail = False
    async def send(self, frame):
        if self.fail:
            raise RuntimeError("offline")
        self.frames.append(frame)
        p = decrypt_mcp(frame, KEY)
        if self.receiver and p.payload[:2] == b"\x01\x0a":
            response = b"\x00\x80\x73\x01\x0a\x00\x80"
            reply = encrypt_mcp(response, KEY, self.sequence, p.mcp_address)
            self.sequence += 1
            self.receiver(reply, 10)
        if self.receiver and len(p.payload) >= 6 and p.payload[1] == 48:
            slot = p.payload[5] if p.payload[0] == 1 else 0
            payload = bytes.fromhex("0430000104") if not slot else bytes([1, 48, 0, slot, 1, 35, 8, 255]) if slot == 1 else bytes([1,48,0,slot,255,255,0,0])
            reply = encrypt_mcp(b"\x00\x80\x73" + payload, KEY, self.sequence, p.mcp_address)
            self.sequence += 1
            self.receiver(reply, 10)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory(INVENTORY)
        self.transport = FakeTransport()
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: 10)
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.output = []
        self.runtime.sink = self.output.append
        self.runtime.session()
        self.transport.receiver = self.runtime.observe
    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_encrypted_light_command_and_correlated_readback(self):
        result = await self.runtime.control(40002, {"brightness": 128})
        self.assertTrue(result["confirmed"])
        packet = decrypt_mcp(self.transport.frames[0], KEY)
        self.assertEqual(packet.crypto_source, 32768)
        self.assertEqual(packet.payload[5], 128)
        self.assertTrue(any(m["topic"].endswith("/40002/state") for m in self.output))

    async def test_group_control_and_protected_target(self):
        await self.runtime.control(291, {"state": "OFF"}, confirm=False)
        packet = decrypt_mcp(self.transport.frames[0], KEY)
        self.assertEqual(packet.mcp_address, 0)
        self.assertEqual(packet.payload[2:4], b"\x01\x23")
        before = len(self.transport.frames)
        with self.assertRaises(ValueError):
            await self.runtime.control(292, {"state": "ON"})
        self.assertEqual(len(self.transport.frames), before)

    async def test_retained_command_never_transmits(self):
        with self.assertRaises(ValueError):
            await self.runtime.control(40002, {"state": "ON"}, retained=True)
        self.assertEqual(self.transport.frames, [])

    async def test_failed_write_burns_sequence(self):
        self.transport.fail = True
        with self.assertRaises(RuntimeError):
            await self.runtime.control(40002, {"state": "ON"})
        self.assertEqual(self.store.get("sequence"), 1)

    async def test_keypad_two_round_verification_and_mapping(self):
        result = await KeypadService(self.runtime).read(40001)
        self.assertEqual(len(self.transport.frames), 10)
        self.assertEqual(result["slots"][0]["target"], 291)
        self.assertEqual(self.store.bindings()[40001], result)
        packet = Packet(50, 40001, 0, 115, bytes.fromhex("000a012300ff00000000"))
        event = self.runtime.core.controllers.decode(packet, 127, 10)
        self.assertEqual(event["button"], 1)
        self.assertIn(("button_1", "activated"), named_inputs(event))

    async def test_broker_disconnect_does_not_replay_events(self):
        self.runtime.mqtt_ready = False
        self.runtime.session()
        self.output.clear()
        payload = bytes.fromhex("008073000a012300ff00000000")
        self.runtime.observe(encrypt_mcp(payload, KEY, 1, 40001), 10)
        self.assertEqual(self.output, [])


class ControllerTests(unittest.TestCase):
    def test_native_anyplace_direction_and_sequence_guard(self):
        router = ControllerRouter()
        def report(sequence, level):
            return router.decode(Packet(sequence, 40005, 0, 115,
                bytes([0,10,1,35,0,level,0,0,0,0])), 81, 100)
        self.assertEqual(report(1, 128)["event_type"], "brightness")
        self.assertEqual(report(2, 200)["direction"], "raise")
        self.assertIsNone(report(1, 128))

    def test_local_report_baseline_query_and_repeated_endpoint(self):
        router = ControllerRouter()
        router.local_modes.add(40003)
        def report(seq, level, token=0):
            return router.decode(Packet(seq, 40003, 0, 115, bytes([1, 10, token, level])), 91, seq * 10)
        self.assertIsNone(report(1, 128))
        self.assertEqual(report(2, 128)["event_type"], "brightness")
        self.assertEqual(report(3, 255)["direction"], "raise")
        self.assertEqual(report(4, 255)["event_type"], "on")
        self.assertIsNone(report(5, 0, token=1))
        self.assertEqual(report(6, 0)["event_type"], "off")
        router.observe_query(40003, 0, 60)
        self.assertIsNone(report(7, 255))

    def test_preset_plan_preserves_exact_level_and_rejects_unknown(self):
        rows = [bytes.fromhex("01300001012308a2")] + [bytes([1,48,0,i,255,255,0,0]) for i in range(2,5)]
        table = snapshot(bytes.fromhex("0430000104"), rows)
        self.assertFalse(plan_edit(table, 1, "native", 291)["changed"])
        self.assertEqual(plan_edit(table, 1, "native", 291, 200)["desired"], "01300001012308c8")
        with self.assertRaises(ValueError):
            plan_edit(table, 1, "native", 291, 0)
        with self.assertRaises(ValueError):
            plan_edit(table, 2, "native", 291)


class MqttTests(unittest.IsolatedAsyncioTestCase):
    async def test_outbox_coalesces_but_events_expire_and_clear(self):
        clock = [0]
        box = Outbox(lambda: clock[0], capacity=2)
        state = {"topic": "state", "payload": 1, "retain": True, "qos": 0}
        event = {"topic": "event", "payload": 1, "retain": False, "qos": 0}
        box.connection(True)
        box.put(state)
        box.put(dict(state, payload=2))
        box.put(event)
        clock[0] = 1001
        self.assertEqual(box.take()["payload"], 2)
        self.assertEqual(box.counts["expired_events"], 1)
        box.put(event)
        box.connection(False)
        self.assertIsNone(box.take())

    async def test_encrypt_matches_independent_fixture(self):
        vectors = json.loads((Path(__file__).resolve().parents[1] / "fixtures/vectors.json").read_text())
        plain = bytes.fromhex("008073000a012300ff00000000")
        self.assertEqual(encrypt_mcp(plain, KEY, 1, 40001).hex(), vectors["on"])


if __name__ == "__main__":
    unittest.main()

class ReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = RuntimeTests.asyncSetUp
    asyncTearDown = RuntimeTests.asyncTearDown
    async def test_mismatched_brightness_cannot_confirm_off(self):
        result = await self.runtime.control(40002,{'state':'OFF'})
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['readbacks'][0]['brightness'],128)

    async def test_cct_confirmation_requires_cct_readback(self):
        old_exchange = self.runtime.exchange
        async def exchange(command, predicate, timeout=3, **kwargs):
            if command[4] == 29:
                raise asyncio.TimeoutError()
            return await old_exchange(command,predicate,timeout,**kwargs)
        self.runtime.exchange = exchange
        result = await self.runtime.control(40002,{'color_temp':5000})
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['readbacks'],[])

    async def test_group_offline_published_immediately_at_session_boundary(self):
        self.output.clear()
        self.runtime.session()
        records = [m for m in self.output if m['topic'].endswith('/light/291/availability')]
        self.assertEqual(records[-1]['payload'],'offline')

class FragmentSessionRegression(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = BlueZTests.asyncSetUp
    asyncTearDown = BlueZTests.asyncTearDown
    async def test_reconnect_between_low_and_high_never_splits_across_clients(self):
        client = self.ble.client
        entered,resume = asyncio.Event(),asyncio.Event()
        original = client.write_gatt_char
        async def paused(char,data,response):
            await original(char,data,response)
            if char == LOW:
                entered.set()
                await resume.wait()
        client.write_gatt_char = paused
        pending = asyncio.create_task(self.ble.send(bytes(27)))
        await entered.wait()
        await self.ble.disconnect()
        await self.ble.connect()
        replacement = self.ble.client
        resume.set()
        with self.assertRaises(RuntimeError):
            await pending
        self.assertEqual(len(client.writes),1)
        self.assertEqual(replacement.writes,[])
        self.assertTrue(self.ble.ready)

class SwitchCapabilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = RuntimeTests.asyncSetUp
    asyncTearDown = RuntimeTests.asyncTearDown
    async def test_smart_switch_exposes_onoff_and_rejects_dimming(self):
        from avionmesh_pi.lifecycle import reload_inventory
        data = self.store.inventory()
        data['devices'].append({'device_id':40003,'product_id':167,'name':'Switch','exposed':True})
        self.store.import_inventory(data)
        reload_inventory(self.runtime)
        discovery = next(m for m in self.runtime.discovery() if m['topic']=='homeassistant/light/pi_lab_40003/config')
        self.assertEqual(discovery['payload']['supported_color_modes'],['onoff'])
        with self.assertRaises(ValueError):await self.runtime.control(40003,{'brightness':128})
        await self.runtime.control(40003,{'state':'ON'},confirm=False)
        self.assertEqual(decrypt_mcp(self.transport.frames[0],KEY).payload[5],255)

class GroupCapabilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = RuntimeTests.asyncSetUp
    asyncTearDown = RuntimeTests.asyncTearDown

    async def test_group_color_requires_all_fresh_members_and_switch_drops_dimming(self):
        from avionmesh_pi.lifecycle import reload_inventory
        data = self.store.inventory()
        data['devices'].append({'device_id':40003,'product_id':134,'name':'Second','exposed':True})
        data['groups'][0]['members'].append(40003)
        self.store.import_inventory(data)
        reload_inventory(self.runtime)
        def config():
            return next(m['payload'] for m in self.runtime.discovery() if m['topic']=='homeassistant/light/pi_lab_group_291/config')
        self.assertEqual(config()['supported_color_modes'],['color_temp'])
        self.assertTrue(config()['color_temp_kelvin'])
        self.runtime.core.states = {m:{'brightness':(128,10),'color_temp':(3000,10)} for m in (40002,40003)}
        def state():
            self.output.clear()
            self.runtime.publish_groups()
            return next(m['payload'] for m in self.output if m['topic'].endswith('/291/state'))
        self.assertEqual(state()['color_temp'],3000)
        self.runtime.core.states[40003]['color_temp']=(4000,10)
        self.assertIsNone(state()['color_temp'])
        self.runtime.core.states[40003]['color_temp']=(3000,-120000)
        self.assertIsNone(state()['color_temp'])
        data['devices'][-1]['product_id']=167
        self.store.import_inventory(data)
        reload_inventory(self.runtime)
        self.assertEqual(config()['supported_color_modes'],['onoff'])
        self.assertFalse(config()['brightness'])

    async def test_non_load_and_empty_groups_stay_unavailable(self):
        from avionmesh_pi.lifecycle import reload_inventory
        for members in ([40002,40001], []):
            data = self.store.inventory()
            data['groups'][0]['members'] = members
            self.store.import_inventory(data)
            reload_inventory(self.runtime)
            self.runtime.core.states = {40002:{'brightness':(128,10)}}
            self.output.clear()
            self.runtime.publish_groups()
            self.assertIsNone(self.runtime.group_mode(data['groups'][0]))
            availability = next(m['payload'] for m in self.output if m['topic'].endswith('/291/availability'))
            self.assertEqual(availability,'offline')
            self.assertFalse(any(m['topic'].endswith('/291/state') for m in self.output))
            with self.assertRaises(ValueError):
                await self.runtime.control(291,{'state':'ON'},confirm=False)
