import asyncio
import tempfile
import unittest
from copy import deepcopy

from avionmesh_pi.mqtt import MqttService
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.scene_mqtt import publications, cleanup_delivered
from support import synthetic_store as Store
from test_service import KEY, INVENTORY, FakeTransport

ID = 'a' * 32


class Engine:
    def __init__(self):
        self.scenes = [{'id': ID, 'name': 'Synthetic Evening', 'status': 'verified',
                        'exposed': True, 'activation_ready': True, 'members': [{'target': 40002, 'brightness': 128, 'kelvin': 4000}]}]
        self.operations = []
        self.calls = []
        self.before = None
    def status(self):
        return deepcopy({'scenes': self.scenes, 'operations': self.operations})
    async def activate(self, identifier, *, admission=None):
        if self.before:
            self.before()
        if admission:
            admission()
        self.calls.append(identifier)


class SceneMqttTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name, KEY)
        self.store.import_inventory(INVENTORY)
        self.runtime = Runtime(self.store, KEY, FakeTransport(), clock=lambda: 10)
        self.runtime.scene_engine = Engine()
        self.runtime.mesh_ready = True
        self.mqtt = MqttService(self.runtime, {'host': 'synthetic.invalid', 'tls': False})
        self.mqtt._connection(True)
        self.topic = self.runtime.core.prefix + '/scene/' + ID + '/set'
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_only_exposed_verified_discovery_and_durable_tombstones(self):
        engine = self.runtime.scene_engine
        config = next(m for m in publications(self.runtime, discovery=True) if m['topic'].endswith('/config'))
        self.assertEqual(config['payload']['payload_on'], 'ON')
        self.assertEqual(config['payload']['command_topic'], self.topic)
        self.assertNotIn('state_topic', config['payload'])
        engine.scenes[0]['status'] = 'needs_review'
        pending = publications(self.runtime, discovery=True)
        self.assertIsInstance(next(m['payload'] for m in pending if m['topic'].endswith('/config')), dict)
        self.assertEqual(next(m['payload'] for m in pending if m['topic'].endswith('/availability')), 'offline')
        # An uncertain create that has never been published stays absent.
        engine.scenes.append(dict(engine.scenes[0], id='b' * 32))
        self.assertFalse(any('scene_' + 'b' * 32 in m['topic'] for m in publications(self.runtime, discovery=True)))
        engine.scenes.clear()
        self.assertTrue(any(m['payload'] == '' and m['topic'].endswith('/config')
                            for m in publications(self.runtime, discovery=True)))
        # Persisted known identifiers survive service recreation and republish cleanup.
        other = Runtime(self.store, KEY, FakeTransport(), clock=lambda: 10)
        other.scene_engine = Engine(); other.scene_engine.scenes.clear()
        self.assertTrue(any(m['payload'] == '' and m['topic'].endswith('/config')
                            for m in publications(other, discovery=True)))

    def test_removed_scene_is_kept_until_both_deletions_are_acknowledged(self):
        self.runtime.mqtt_ready = True
        publications(self.runtime, discovery=True)
        self.assertEqual(self.store.get('scene_mqtt_known_v1'), [ID])
        self.runtime.scene_engine.scenes.clear()
        for _ in range(3):
            messages = publications(self.runtime, discovery=True)
            cleared = {m['topic'].rsplit('/', 1)[-1] for m in messages if ID in m['topic'] and m['payload'] == ''}
            self.assertEqual(cleared, {'config', 'availability'})
        self.assertEqual(self.store.get('scene_mqtt_known_v1'), [ID])
        self.assertTrue(all(m['qos'] == 1 for m in messages))
        cleanup_delivered(self.runtime, messages[0], self.mqtt.scene_cleanup_acks)
        self.assertEqual(self.store.get('scene_mqtt_known_v1'), [ID])
        cleanup_delivered(self.runtime, messages[1], self.mqtt.scene_cleanup_acks)
        self.assertEqual(self.store.get('scene_mqtt_known_v1'), [])
        self.assertIsNone(self.store.get('scene_mqtt_cleanup_acknowledged_v1'))
        self.assertFalse(any(ID in m['topic'] for m in publications(self.runtime, discovery=True)))

    async def test_disconnect_before_cleanup_delivery_republishes_after_restart(self):
        self.runtime.sink = self.mqtt.outbox.put
        self.runtime.scene_changed()
        self.mqtt.outbox.states.clear()
        self.runtime.scene_engine.scenes.clear()
        for _ in range(5): self.runtime.scene_changed()
        self.assertEqual(self.store.get('scene_mqtt_known_v1'), [ID])
        self.mqtt._connection(False)
        self.mqtt._connection(True)
        self.runtime.emit(self.runtime.discovery())  # Same reconnect path as MqttService.run.
        self.assertEqual(sum(ID in topic for topic in self.mqtt.outbox.states), 2)

        class FailingClient:
            async def publish(client, topic, payload, **kwargs):
                self.assertEqual(payload, '')
                self.assertEqual(kwargs, {'qos': 1, 'retain': True})
                raise ConnectionError('Synthetic lost acknowledgement')
        # Keep only cleanup messages, as a reconnect also publishes bridge state.
        self.mqtt.outbox.states = type(self.mqtt.outbox.states)(
            (t, m) for t, m in self.mqtt.outbox.states.items() if ID in t)
        with self.assertRaises(ConnectionError):
            await self.mqtt.publish_loop(FailingClient())
        self.assertEqual(self.store.get('scene_mqtt_known_v1'), [ID])

        other = Runtime(self.store, KEY, FakeTransport(), clock=lambda: 10)
        other.scene_engine = Engine(); other.scene_engine.scenes.clear()
        service = MqttService(other, {'host': 'synthetic.invalid', 'tls': False})
        other.sink = service.outbox.put; service._connection(True)
        service.outbox.states.clear(); other.scene_changed()
        calls = []
        class AcknowledgingClient:
            async def publish(client, topic, payload, **kwargs):
                calls.append(topic)
                self.assertEqual(kwargs, {'qos': 1, 'retain': True})
        task = asyncio.create_task(service.publish_loop(AcknowledgingClient()))
        try:
            for _ in range(30):
                if self.store.get('scene_mqtt_known_v1') == []: break
                await asyncio.sleep(.001)
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.store.get('scene_mqtt_known_v1'), [])
            self.assertIsNone(self.store.get('scene_mqtt_cleanup_acknowledged_v1'))
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)

    def test_reconnect_requires_both_cleanup_acks_in_current_connection(self):
        publications(self.runtime, discovery=True)
        self.runtime.scene_engine.scenes.clear()
        messages = publications(self.runtime, discovery=True)
        cleanup_delivered(self.runtime, messages[0], self.mqtt.scene_cleanup_acks)
        # Duplicate ACK handling cannot count as the other deletion.
        cleanup_delivered(self.runtime, messages[0], self.mqtt.scene_cleanup_acks)
        self.assertEqual(self.store.get('scene_mqtt_known_v1'), [ID])
        self.mqtt._connection(False); self.mqtt._connection(True)
        self.assertEqual(self.mqtt.scene_cleanup_acks, {})
        cleanup_delivered(self.runtime, messages[1], self.mqtt.scene_cleanup_acks)
        self.assertEqual(self.store.get('scene_mqtt_known_v1'), [ID])
        cleanup_delivered(self.runtime, messages[0], self.mqtt.scene_cleanup_acks)
        self.assertEqual(self.store.get('scene_mqtt_known_v1'), [])

    def test_changed_broker_cannot_reuse_old_brokers_partial_cleanup_ack(self):
        publications(self.runtime, discovery=True)
        self.runtime.scene_engine.scenes.clear()
        messages = publications(self.runtime, discovery=True)
        by_part = {m['scene_cleanup']['part']: m for m in messages}
        cleanup_delivered(self.runtime, by_part['config'], self.mqtt.scene_cleanup_acks)
        self.assertEqual(self.store.get('scene_mqtt_known_v1'), [ID])
        self.assertIsNone(self.store.get('scene_mqtt_cleanup_acknowledged_v1'))
        # A settings change creates a new MQTT service; a process restart also
        # recreates the runtime. Neither carries the previous partial PUBACK.
        other = Runtime(self.store, KEY, FakeTransport(), clock=lambda: 10)
        other.scene_engine = Engine(); other.scene_engine.scenes.clear()
        service = MqttService(other, {'host': 'other-broker.invalid', 'tls': False})
        service._connection(True)
        cleanup_delivered(other, by_part['availability'], service.scene_cleanup_acks)
        self.assertEqual(self.store.get('scene_mqtt_known_v1'), [ID])
        cleanup_delivered(other, by_part['config'], service.scene_cleanup_acks)
        self.assertEqual(self.store.get('scene_mqtt_known_v1'), [])

    async def test_old_publish_completion_cannot_ack_after_connection_boundary(self):
        publications(self.runtime, discovery=True)
        self.runtime.scene_engine.scenes.clear()
        messages = publications(self.runtime, discovery=True)
        by_part = {m['scene_cleanup']['part']: m for m in messages}
        self.mqtt.outbox.put(by_part['config'])
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class DeferredClient:
            async def publish(client, topic, payload, **kwargs):
                entered.set(); await release.wait(); finished.set()
        task = asyncio.create_task(self.mqtt.publish_loop(DeferredClient()))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.mqtt._connection(False); self.mqtt._connection(True)
            release.set(); await asyncio.wait_for(finished.wait(), 1)
            await asyncio.sleep(0)
            self.assertEqual(self.mqtt.scene_cleanup_acks, {})
            cleanup_delivered(self.runtime, by_part['availability'], self.mqtt.scene_cleanup_acks)
            self.assertEqual(self.store.get('scene_mqtt_known_v1'), [ID])
            cleanup_delivered(self.runtime, by_part['config'], self.mqtt.scene_cleanup_acks)
            self.assertEqual(self.store.get('scene_mqtt_known_v1'), [])
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)

    def test_removed_scene_stays_listed_while_home_assistant_is_disconnected(self):
        self.runtime.mqtt_ready = True
        publications(self.runtime, discovery=True)
        self.runtime.scene_engine.scenes.clear(); self.runtime.mqtt_ready = False
        for _ in range(5): publications(self.runtime, discovery=True)
        self.assertEqual(self.store.get('scene_mqtt_known_v1'), [ID])

    def test_damaged_scene_record_pauses_scene_publication_only(self):
        def broken(): raise RuntimeError('Invalid saved scene identity')
        self.runtime.scene_engine.status = broken
        self.assertEqual(publications(self.runtime, discovery=True), [])
        self.mqtt.ingest(self.topic, b'ON', False)
        self.assertFalse(self.mqtt.commands)

    def test_needs_verification_stays_registered_but_cannot_queue_activation(self):
        self.runtime.scene_engine.scenes[0]['activation_ready'] = False
        messages = publications(self.runtime, discovery=True)
        self.assertIsInstance(next(m['payload'] for m in messages if m['topic'].endswith('/config')), dict)
        self.assertEqual(next(m['payload'] for m in messages if m['topic'].endswith('/availability')), 'offline')
        self.mqtt.ingest(self.topic, b'ON', False)
        self.assertFalse(self.mqtt.commands)

    def test_retained_unexposed_and_wrong_payload_never_queue(self):
        self.mqtt.ingest(self.topic, b'ON', True)
        self.mqtt.ingest(self.topic, b'{"state":"ON"}', False)
        self.runtime.scene_engine.scenes[0]['exposed'] = False
        self.mqtt.ingest(self.topic, b'ON', False)
        self.assertFalse(self.mqtt.commands)
        self.assertFalse(self.runtime.transport.frames)

    async def test_activation_once_and_broker_boundary_during_verification_revokes_send(self):
        self.mqtt.ingest(self.topic, b'ON', False)
        task = asyncio.create_task(self.mqtt.command_loop())
        try:
            for _ in range(30):
                if self.runtime.scene_engine.calls: break
                await asyncio.sleep(.001)
            self.assertEqual(self.runtime.scene_engine.calls, [ID])
            self.runtime.scene_engine.before = lambda: self.mqtt._connection(False)
            self.mqtt.ingest(self.topic, b'ON', False)
            await asyncio.sleep(.02)
            self.assertEqual(self.runtime.scene_engine.calls, [ID])
            self.assertFalse(self.mqtt.commands)
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)

    async def test_exposure_revoked_while_verify_blocks_dispatch(self):
        self.runtime.scene_engine.before = lambda: self.runtime.scene_engine.scenes[0].update(exposed=False)
        self.mqtt.ingest(self.topic, b'ON', False)
        task = asyncio.create_task(self.mqtt.command_loop())
        try:
            await asyncio.sleep(.02)
            self.assertFalse(self.runtime.scene_engine.calls)
            self.assertEqual(self.mqtt.counts['scene_unavailable'], 1)
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
