"""Actual loopback MQTT peer + simulated GATT; not physical HA qualification."""
import asyncio
import copy
import json
from unittest.mock import patch

from avionmesh_pi.protocol import decrypt_mcp
import test_service_wire as lab


class MqttReliabilityWireTests(lab.WholeServiceTests):
    test_actual_mqtt_http_crypto_and_discovery_round_trip = None
    test_duplicate_packets_authentication_and_broker_reconnect = None
    test_partial_gatt_failure_reconnect_and_http_auth_bounds = None
    test_group_hide_clears_retained_ha_discovery = None

    def hold_first_brightness(self):
        held, original = [], self.relay.emit
        def emit(source, payload, repeat=1):
            if source == 40002 and payload[:2] == b'\x01\x0a' and not held:
                held.append((source, payload, repeat))
                return b''
            return original(source, payload, repeat)
        return held, original, patch.object(self.relay, 'emit', side_effect=emit)

    async def test_mqtt_command_waits_for_read_transmission_but_not_reply_and_keeps_latest(self):
        held, _, intercepted = self.hold_first_brightness()
        entered, release = asyncio.Event(), asyncio.Event()
        original_send = self.runtime.transport.send
        async def send(frame):
            packet = decrypt_mcp(frame, self.key)
            if packet.payload[:2] == b'\x01\x0a' and not entered.is_set():
                entered.set()
                await release.wait()  # a transmission still in progress must finish
            await original_send(frame)
        async def background():
            async with self.runtime._background_read() as admitted:
                self.assertTrue(admitted)
                return await self.runtime.read_light(40002)
        with intercepted, patch.object(self.runtime.transport, 'send', side_effect=send):
            reader = asyncio.create_task(background())
            self.addAsyncCleanup(lab.stop_service_task, reader)
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await self.broker.publish(self.prefix + '/light/40002/set', '{"brightness":77}')
                await lab.eventually(lambda: self.runtime.status()['foreground_waiters'] == 1)
                await self.broker.publish(self.prefix + '/light/40002/set', '{"brightness":99}')
                await lab.eventually(lambda: self.runtime.mqtt_metrics()['counters'].get('coalesced_command') == 1)
                release.set()
                # The query's reply is deliberately withheld. Only its send must
                # finish before MQTT takes over, not the three-second reply wait.
                result = await asyncio.wait_for(reader, .5)
                self.assertTrue(held)
                self.assertFalse(result['complete'])
                await lab.eventually(lambda: self.runtime.mqtt_metrics()['counters'].get('commands_sent') == 1)
            finally:
                release.set()
        setters = [decrypt_mcp(frame, self.key) for frame in self.relay.frames
                   if decrypt_mcp(frame, self.key).payload[0] == 0]
        self.assertEqual([packet.payload[5] for packet in setters], [99])
        await lab.eventually(lambda: json.loads(self.broker.retained.get(self.prefix + '/light/40002/state', b'{}')).get('brightness') == 99)

    async def test_broker_reconnect_preserves_inflight_local_verification(self):
        held, emit, intercepted = self.hold_first_brightness()
        generation, connections = self.runtime.generation, self.broker.connects
        with intercepted:
            reader = asyncio.create_task(self.runtime.read_light(40002))
            self.addAsyncCleanup(lab.stop_service_task, reader)
            await lab.eventually(lambda: bool(held))
            await self.broker.drop()
            await lab.eventually(lambda: self.broker.connects > connections and self.runtime.mqtt_ready)
            self.assertEqual(self.runtime.generation, generation)
            self.assertFalse(reader.done())
            emit(*held[0])
            result = await reader
        self.assertTrue(result['complete'])
        self.assertEqual(result['observed']['brightness'], self.relay.brightness)
        self.assertTrue(self.runtime.mesh_ready)

    async def test_reconnect_and_ha_birth_republish_observations_without_event_replay(self):
        async with self.http.post('/api/device/read', json={'target': 40002}) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())['complete'])
        event_topic = self.prefix + '/controller/40001/input/on'
        self.relay.emit(40001, bytes.fromhex('000a012300ff00000000'))
        await lab.eventually(lambda: any(topic == event_topic for topic, _, _ in self.broker.messages))
        event_count = sum(topic == event_topic for topic, _, _ in self.broker.messages)
        observations = copy.deepcopy(self.runtime.local_light_states)
        generation, frames = self.runtime.generation, len(self.relay.frames)
        topics = [self.prefix + '/light/40002/state', self.prefix + '/light/291/state']
        for topic in topics:
            self.broker.retained.pop(topic, None)
        connections = self.broker.connects
        await self.broker.drop()
        await lab.eventually(lambda: self.broker.connects > connections and self.runtime.mqtt_ready)
        await lab.eventually(lambda: all(topic in self.broker.retained for topic in topics))
        for topic in topics:
            self.assertEqual(json.loads(self.broker.retained.pop(topic))['brightness'], self.relay.brightness)
        await self.broker.publish('homeassistant/status', 'online')
        await lab.eventually(lambda: all(topic in self.broker.retained for topic in topics))
        self.assertEqual(sum(topic == event_topic for topic, _, _ in self.broker.messages), event_count)
        self.assertEqual(self.runtime.local_light_states, observations)
        self.assertEqual(self.runtime.generation, generation)
        self.assertEqual(len(self.relay.frames), frames)


if __name__ == '__main__':
    import unittest
    unittest.main()
