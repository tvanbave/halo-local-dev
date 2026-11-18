"""Synthetic acceptance contracts. This is not a running Home Assistant test."""
import asyncio
import json
from unittest.mock import patch
import test_service_wire as lab


class HaAcceptanceWireTests(lab.WholeServiceTests):
    # Reuse isolated full-service setup without rerunning its existing scenarios.
    test_actual_mqtt_http_crypto_and_discovery_round_trip = None
    test_duplicate_packets_authentication_and_broker_reconnect = None
    test_partial_gatt_failure_reconnect_and_http_auth_bounds = None
    test_group_hide_clears_retained_ha_discovery = None

    async def metadata(self, target, exposed):
        async with self.http.get('/api/snapshot') as response:
            revision = (await response.json())['status']['inventory_revision']
        async with self.http.post('/api/metadata', json={
                'operation': 'update', 'revision': revision, 'kind': 'device',
                'target': target, 'name': 'Synthetic light', 'exposed': exposed}) as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    async def test_exposure_is_metadata_only_and_hidden_light_stays_local(self):
        topic = 'homeassistant/light/wire_lab_40002/config'
        before = len(self.relay.frames)
        await self.metadata(40002, False)
        await lab.eventually(lambda: topic not in self.broker.retained)
        self.assertEqual(len(self.relay.frames), before)
        async with self.http.post('/api/control', json={'target': 40002, 'values': {'brightness': 81}}) as response:
            self.assertTrue((await response.json())['confirmed'])
        self.assertEqual(self.relay.brightness, 81)
        self.assertNotIn(topic, self.broker.retained)
        before = len(self.relay.frames)
        await self.metadata(40002, True)
        await lab.eventually(lambda: topic in self.broker.retained)
        self.assertEqual(len(self.relay.frames), before)

    async def test_hidden_controller_tombstones_survive_birth_and_reconnect(self):
        stem = 'wire_lab_40001'
        await lab.eventually(lambda: 'homeassistant/event/'+stem+'_on/config' in self.broker.retained)
        await self.metadata(40001, False)
        def absent():
            return not any(t.startswith('homeassistant/') and stem in t for t in self.broker.retained)
        await lab.eventually(absent)
        before = len(self.relay.frames)
        await self.broker.publish('homeassistant/status', 'online')
        connections = self.broker.connects
        await self.broker.drop()
        await lab.eventually(lambda: self.broker.connects > connections and self.runtime.mqtt_ready)
        await asyncio.sleep(.05)
        self.assertTrue(absent())
        self.assertEqual(len(self.relay.frames), before)

    async def test_hidden_controller_survives_service_restart_with_persisted_history(self):
        from aiohttp import ClientSession
        stem = 'wire_lab_40001'
        await self.metadata(40001, False)
        def absent():
            return not any(t.startswith('homeassistant/') and stem in t for t in self.broker.retained)
        await lab.eventually(absent)
        before = len(self.relay.frames)
        await self.http.close()
        await lab.stop_service_task(self.task)
        self.task = asyncio.create_task(lab.serve(self.config))
        self.addAsyncCleanup(lab.stop_service_task, self.task)
        await lab.eventually(lambda: len(self.runtimes) == 2 and self.runtimes[-1].mqtt_ready)
        self.runtime = self.runtimes[-1]
        port = self.sites[-1]._server.sockets[0].getsockname()[1]
        self.http = ClientSession(base_url='http://127.0.0.1:' + str(port),
                                  headers={'Authorization': 'Bearer ' + self.token})
        self.addAsyncCleanup(self.http.close)
        await self.broker.publish('homeassistant/status', 'online')
        await asyncio.sleep(.05)
        self.assertTrue(absent())
        self.assertFalse(self.runtime.core.devices[40001].exposed)
        self.assertEqual(len(self.relay.frames), before)

    async def test_verified_button_payload_and_mapping_loss_remove_trigger(self):
        trigger = 'homeassistant/device_automation/wire_lab_40001_button_1/config'
        self.assertNotIn(trigger, self.broker.retained)
        self.runtime.core.controllers.bindings[40001] = {'slots': [
            {'slot': 1, 'target': 291, 'flags': 8, 'level': 162}]}
        self.runtime.emit(self.runtime.discovery())
        await lab.eventually(lambda: trigger in self.broker.retained)
        contract = json.loads(self.broker.retained[trigger])
        self.assertEqual((contract['type'], contract['subtype']), ('activated', 'Button 1'))
        self.relay.emit(40001, bytes.fromhex('000a012300a200000000'))
        topic = contract['topic']
        await lab.eventually(lambda: any(t == topic for t, _, _ in self.broker.messages))
        payload, retained = next((json.loads(p), r) for t, p, r in self.broker.messages if t == topic)
        self.assertEqual((payload['event_type'], payload['action']), ('activated', 'button_1'))
        self.assertFalse(retained)
        self.runtime.core.controllers.bindings.clear()
        self.runtime.emit(self.runtime.discovery())
        await lab.eventually(lambda: trigger not in self.broker.retained)
        mapping = json.loads(self.broker.retained[topic + '/mapping'])
        self.assertEqual(mapping['mapping_status'], 'needs_refresh')
        self.assertEqual(self.broker.retained[topic + '/availability'], b'offline')

    async def test_broker_outage_preserves_local_http_control(self):
        self.broker.server.close()
        await self.broker.drop()
        await asyncio.wait_for(self.broker.server.wait_closed(), 2)
        await lab.eventually(lambda: not self.runtime.mqtt_ready)
        self.assertTrue(self.runtime.mesh_ready)
        async with self.http.post('/api/control', json={'target': 40002, 'values': {'brightness': 82}}) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())['confirmed'])
        self.assertEqual(self.relay.brightness, 82)

    async def test_controller_contracts_and_quarantine_on_birth(self):
        topic = 'homeassistant/event/wire_lab_40001_on/config'
        await lab.eventually(lambda: topic in self.broker.retained)
        event = json.loads(self.broker.retained[topic])
        trigger = json.loads(self.broker.retained['homeassistant/device_automation/wire_lab_40001_on/config'])
        self.assertEqual(event['event_types'], ['command'])
        self.assertEqual(trigger['topic'], event['state_topic'])
        self.assertEqual(trigger['payload'], 'command')
        self.assertEqual(trigger['value_template'], '{{ value_json.event_type }}')
        availability = self.prefix + '/device/40001/availability'
        with patch.object(self.runtime, 'quarantined', return_value={40001}):
            await self.broker.publish('homeassistant/status', 'online')
            await lab.eventually(lambda: self.broker.retained.get(availability) == b'offline')
            await lab.eventually(lambda: 'homeassistant/device_automation/wire_lab_40001_on/config' not in self.broker.retained)
            count = sum(t == event['state_topic'] for t, p, r in self.broker.messages)
            self.relay.emit(40001, bytes.fromhex('000a012300ff00000000'))
            await asyncio.sleep(.05)
            self.assertEqual(sum(t == event['state_topic'] for t, p, r in self.broker.messages), count)
        await self.broker.publish('homeassistant/status', 'online')
        await lab.eventually(lambda: self.broker.retained.get(availability) == b'online')
        await lab.eventually(lambda: 'homeassistant/device_automation/wire_lab_40001_on/config' in self.broker.retained)
