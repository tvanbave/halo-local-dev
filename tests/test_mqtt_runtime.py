import asyncio
import importlib.util
import tempfile
import unittest

from avionmesh_pi.mqtt import MqttService
from avionmesh_pi.runtime import Runtime
from support import synthetic_store as Store
from test_service import KEY, INVENTORY, FakeTransport


class Messages:
    def __init__(self):
        self.queue = asyncio.Queue()
    def __aiter__(self):
        return self
    async def __anext__(self):
        return await self.queue.get()


class Broker:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.messages = Messages()
        self.subscriptions = []
        self.published = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def subscribe(self, topic):
        self.subscriptions.append(topic)
    async def publish(self, topic, payload, **kwargs):
        self.published.append((topic, payload, kwargs))


@unittest.skipIf(importlib.util.find_spec('aiomqtt') is None, "MQTT dependency tested in container")
class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_subscription_encrypted_send_and_graceful_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, KEY)
            store.import_inventory(INVENTORY)
            transport = FakeTransport()
            runtime = Runtime(store, KEY, transport, clock=lambda: 10)
            transport.receiver = runtime.observe
            runtime.mesh_ready = True
            clients = []
            def factory(**kwargs):
                client = Broker(**kwargs)
                clients.append(client)
                return client
            mqtt = MqttService(runtime, {"host": "synthetic.invalid", "tls": False}, factory)
            runtime.sink = mqtt.outbox.put
            task = asyncio.create_task(mqtt.run())
            try:
                for _ in range(50):
                    if clients and clients[0].subscriptions:
                        break
                    await asyncio.sleep(.01)
                broker = clients[0]
                self.assertEqual(broker.subscriptions, [runtime.core.prefix + '/light/+/set', runtime.core.prefix + '/scene/+/set', 'homeassistant/status'])
                self.assertEqual(broker.options['will'].payload, 'offline')
                mqtt.ingest(runtime.core.prefix + '/light/40002/set', b'{"brightness":128}', True)
                self.assertEqual(mqtt.counts['retained_rejected'], 1)
                self.assertEqual(transport.frames, [])
                mqtt.ingest(runtime.core.prefix + '/light/40002/set', b'{"brightness":128}', False)
                for _ in range(50):
                    if mqtt.counts['commands_sent']:
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(mqtt.counts['commands_sent'], 1)
                self.assertEqual(len(transport.frames), 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                store.close()
            self.assertEqual(broker.published[-1],
                (runtime.core.prefix + '/availability', 'offline', {'qos': 1, 'retain': True}))
