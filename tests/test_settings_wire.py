"""Real loopback MQTT and HTTP, synthetic state/GATT; no installation access."""
import asyncio
import json

import test_service_wire as lab
from avionmesh_pi.mqtt_settings import MqttSettings


class SettingsWireTests(lab.WholeServiceTests):
    test_actual_mqtt_http_crypto_and_discovery_round_trip = None
    test_duplicate_packets_authentication_and_broker_reconnect = None
    test_partial_gatt_failure_reconnect_and_http_auth_bounds = None
    test_group_hide_clears_retained_ha_discovery = None

    async def settings(self):
        async with self.http.get('/api/settings/mqtt') as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    async def save_enabled(self, enabled):
        current = await self.settings()
        payload = {key: current[key] for key in ('revision', 'host', 'port', 'username', 'tls')}
        payload.update(enabled=enabled, password_action='keep')
        async with self.http.post('/api/settings/mqtt', json=payload) as response:
            self.assertEqual(response.status, 200, await response.text())
            return await response.json()

    async def test_disable_reenable_persist_and_connect_probe_leave_bluetooth_session_intact(self):
        generation, sequence = self.runtime.generation, self.runtime.store.get('sequence')
        connections = self.broker.connects
        await self.save_enabled(False)
        self.assertFalse(self.runtime.mqtt_enabled)
        self.assertFalse(self.runtime.mqtt_ready)
        self.assertEqual(self.runtime.generation, generation)
        await lab.eventually(lambda: self.broker.retained.get(self.prefix + '/availability') == b'offline')
        current = await self.settings()
        payload = {key: current[key] for key in ('revision', 'host', 'port', 'username', 'tls')}
        payload.update(enabled=True, password_action='keep')
        messages = len(self.broker.messages)
        async with self.http.post('/api/settings/mqtt/test', json=payload) as response:
            self.assertEqual(response.status, 200)
            result = await response.json()
        self.assertTrue(result['connected'])
        self.assertFalse(result['published'])
        self.assertFalse(result['subscribed'])
        self.assertFalse((await self.settings())['enabled'])
        self.assertEqual(len(self.broker.messages), messages)
        await self.save_enabled(True)
        await lab.eventually(lambda: self.runtime.mqtt_ready and self.broker.connects > connections)
        self.assertEqual(self.runtime.generation, generation)
        self.assertEqual(self.runtime.store.get('sequence'), sequence)
        self.assertEqual(self.relay.frames, [])
        from avionmesh_pi.runtime import Runtime
        restored_runtime = Runtime(self.runtime.store, self.key, self.runtime.transport)
        restored = MqttSettings(restored_runtime, self.config['state_directory'])
        try:
            self.assertTrue(restored.public()['enabled'])
            self.assertEqual(restored.public()['port'], self.broker.port)
        finally:
            await restored.close()
        self.assertTrue(self.runtime.mqtt_ready)

    async def test_disable_waits_for_inflight_local_read_without_cancelling_it(self):
        original, held = self.relay.emit, []
        def hold(source, payload, repeat=1):
            if source == 40002 and payload[:2] == b'\x01\x0a' and not held:
                held.append((source, payload, repeat))
                return b''
            return original(source, payload, repeat)
        from unittest.mock import patch
        with patch.object(self.relay, 'emit', side_effect=hold):
            reader = asyncio.create_task(self.runtime.read_light(40002))
            self.addAsyncCleanup(lab.stop_service_task, reader)
            await lab.eventually(lambda: bool(held))
            changing = asyncio.create_task(self.save_enabled(False))
            self.addAsyncCleanup(lab.stop_service_task, changing)
            await asyncio.sleep(.03)
            self.assertFalse(reader.done())
            self.assertFalse(changing.done())
            original(*held[0])
            self.assertTrue((await reader)['complete'])
            await changing
        self.assertFalse(self.runtime.mqtt_enabled)
        self.assertTrue(self.runtime.mesh_ready)

    async def test_trusted_lan_commissioning_can_enable_mqtt_and_restore_it_after_restart(self):
        from aiohttp import ClientSession
        from aiohttp.web_runner import TCPSite
        from unittest.mock import patch
        from avionmesh_pi.service import serve
        inventory = self.runtime.store.inventory()
        await self.http.close()
        await lab.stop_service_task(self.task)
        config = dict(self.config, commissioning_only=True, mqtt=None,
                      trusted_lan_no_auth=True, allow_lan_http=True,
                      http_host='192.168.50.20', http_port=18089,
                      capabilities={'observation_refresh': False, 'startup_light_read': False})
        # Only the synthetic listener is rebound. The actual production access
        # validator and Host/Origin contract use the configured private address.
        def local_site(runner, *args, **kwargs):
            site = TCPSite(runner, '127.0.0.1', 0)
            self.sites.append(site)
            return site
        async def start():
            count = len(self.runtimes)
            self.task = asyncio.create_task(serve(config))
            self.addAsyncCleanup(lab.stop_service_task, self.task)
            await lab.eventually(lambda: len(self.runtimes) > count and self.runtimes[-1].mesh_ready)
            self.runtime = self.runtimes[-1]
            port = self.sites[-1]._server.sockets[0].getsockname()[1]
            self.http = ClientSession(base_url='http://127.0.0.1:' + str(port),
                headers={'Host': '192.168.50.20:18089', 'Origin': 'http://192.168.50.20:18089'})
            self.addAsyncCleanup(self.http.close)
        with patch('aiohttp.web.TCPSite', side_effect=local_site):
            await start()
            current = await self.settings()
            self.assertFalse(current['enabled'])
            self.assertTrue(self.runtime.commissioning_only)
            async with self.http.get('/') as response:
                self.assertEqual(response.status, 200)
                self.assertIn('/settings.js', await response.text())
            async with self.http.post('/api/settings/mqtt', json={
                'revision': current['revision'], 'enabled': True, 'host': '127.0.0.1',
                'port': self.broker.port, 'username': '', 'tls': False, 'password_action': 'clear'}) as response:
                self.assertEqual(response.status, 200, await response.text())
            await lab.eventually(lambda: self.runtime.mqtt_ready)
            self.assertFalse(self.runtime.commissioning_only)
            saved_revision = (await self.settings())['revision']
            await self.http.close()
            await lab.stop_service_task(self.task)
            await start()
            await lab.eventually(lambda: self.runtime.mqtt_ready)
            restored = await self.settings()
            self.assertEqual(restored['revision'], saved_revision)
            self.assertTrue(restored['enabled'])
            self.assertEqual(self.runtime.store.inventory(), inventory)
            self.assertFalse(self.runtime.commissioning_only)
            self.assertIsNone(config['mqtt'])

