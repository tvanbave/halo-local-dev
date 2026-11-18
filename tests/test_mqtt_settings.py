"""MQTT configuration tests use private synthetic state and fake clients only."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from avionmesh_pi.mqtt_settings import MqttSettings, SettingsError
from avionmesh_pi.runtime import Runtime
from support import synthetic_store
from test_service import FakeTransport, KEY


class Messages:
    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Future()


class Client:
    def __init__(self, owner, kwargs):
        self.owner, self.kwargs = owner, kwargs
        self.messages = Messages()
        self.published, self.subscribed = [], []
        self.entered = self.exited = False

    async def __aenter__(self):
        if self.owner.failure:
            raise RuntimeError(self.owner.failure)
        if self.owner.enter_gate is not None:
            await self.owner.enter_gate.wait()
        self.entered = True
        return self

    async def __aexit__(self, *args):
        if self.owner.exit_gate is not None:
            await self.owner.exit_gate.wait()
        self.exited = True

    async def subscribe(self, topic):
        self.subscribed.append(topic)

    async def publish(self, topic, payload, **kwargs):
        self.published.append((topic, payload, kwargs))


class Factory:
    def __init__(self):
        self.clients = []
        self.failure = self.enter_gate = self.exit_gate = None

    def __call__(self, **kwargs):
        client = Client(self, kwargs)
        self.clients.append(client)
        return client


class MqttSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = synthetic_store(self.directory, KEY)
        self.transport = FakeTransport()
        self.runtime = Runtime(self.store, KEY, self.transport)
        self.runtime.mesh_ready = True
        self.factory = Factory()
        self.manager = MqttSettings(self.runtime, self.directory, client_factory=self.factory)
        self.tasks = []
        self.mqtt_stub = patch.dict('sys.modules', {'aiomqtt': types.SimpleNamespace(Will=lambda *a, **k: (a, k))})
        self.mqtt_stub.start()

    async def asyncTearDown(self):
        self.factory.exit_gate = None
        await self.manager.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.mqtt_stub.stop()
        self.assertFalse(self.runtime.operation.locked())
        self.assertEqual(self.runtime.session_observers, [])
        self.assertEqual(self.transport.frames, [])
        self.store.close()
        self.temp.cleanup()

    def payload(self, **changes):
        public = self.manager.public()
        result = {key: public[key] for key in ('revision', 'enabled', 'host', 'port', 'username', 'tls')}
        result.update(password_action='keep')
        result.update(changes)
        return result

    async def until(self, predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail('Expected asynchronous boundary was not reached')

    async def start(self):
        task = asyncio.create_task(self.manager.run())
        self.tasks.append(task)
        await self.until(lambda: self.manager._running)

    async def enable(self, **changes):
        payload = self.payload(enabled=True, host='broker.example.invalid', port=1883, tls=False,
                               username='synthetic-user', password_action='replace', password='synthetic-password')
        payload.update(changes)
        if payload['password_action'] != 'replace':
            payload.pop('password', None)
        result = await self.manager.save(payload)
        if self.manager._running:
            await self.until(lambda: self.runtime.mqtt_ready)
        return result

    async def test_disabled_defaults_do_not_create_clients_or_polling(self):
        await self.start()
        value = self.manager.public()
        self.assertFalse(value['enabled'])
        self.assertEqual((value['tls'], value['port']), (True, 8883))
        self.assertFalse(value['password_set'])
        self.assertEqual(self.factory.clients, [])
        self.assertIsNone(self.manager._poll_task)
        self.assertTrue(self.runtime.commissioning_only)

    async def test_saved_private_override_survives_restart_without_sqlite_secrets(self):
        before = self.store.get('sequence'), self.runtime.generation, self.store.get('state_id')
        result = await self.enable()
        self.assertTrue(result['saved'])
        self.assertEqual(self.manager.public()['source'], 'settings')
        self.assertNotIn('synthetic-password', json.dumps(result))
        path = self.directory / self.manager.filename
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        rows = ' '.join(row[0] for row in self.store.db.execute('SELECT value FROM metadata'))
        self.assertNotIn('synthetic-password', rows)
        public = self.manager.public()
        await self.manager.close()
        self.manager = MqttSettings(self.runtime, self.directory,
                                   initial_config={'host': 'old.example.invalid'}, client_factory=self.factory)
        self.assertEqual(self.manager.public(), public)
        self.assertEqual((self.store.get('sequence'), self.runtime.generation, self.store.get('state_id')), before)
        self.assertFalse(self.runtime.commissioning_only)

    async def test_explicit_disable_overrides_enabled_service_config_after_restart(self):
        await self.enable()
        await self.manager.save(self.payload(enabled=False))
        await self.manager.close()
        self.manager = MqttSettings(self.runtime, self.directory,
                                   initial_config={'host': 'old.example.invalid'}, client_factory=self.factory)
        await self.start()
        self.assertFalse(self.runtime.mqtt_enabled)
        self.assertFalse(self.runtime.mqtt_ready)
        self.assertIsNone(self.manager._task)
        self.assertIsNone(self.manager._poll_task)
        self.assertEqual(self.factory.clients, [])

    async def test_bad_and_stale_requests_never_write_or_connect(self):
        original = self.manager.public()
        bad = [self.payload(enabled=True, host='https://broker'), self.payload(port=True),
               self.payload(tls='true'), self.payload(password_action='bogus'),
               self.payload(password_action='replace', password=''),
               self.payload(password='accidental'), self.payload(revision='stale'),
               self.payload(ca_file='/arbitrary/file'), self.payload(enabled=True, host='')]
        for payload in bad:
            with self.subTest(payload={k:v for k,v in payload.items() if k != 'password'}):
                with self.assertRaises(SettingsError):
                    await self.manager.save(payload)
        self.assertEqual(self.manager.public(), original)
        self.assertFalse((self.directory / self.manager.filename).exists())
        self.assertEqual(self.factory.clients, [])

    async def test_saved_password_cannot_be_sent_to_a_changed_broker_identity(self):
        await self.enable()
        for change in ({'host': 'other.example.invalid'}, {'port': 1884}, {'username': 'other'}, {'tls': True}):
            with self.subTest(change=change):
                with self.assertRaises(SettingsError) as caught:
                    await self.manager.test(self.payload(**change))
                self.assertEqual(caught.exception.code, 'mqtt_password_reentry_required')
        self.assertEqual(self.factory.clients, [])
        await self.manager.save(self.payload(host='other.example.invalid', password_action='clear'))
        self.assertFalse(self.manager.public()['password_set'])

    async def test_probe_uses_distinct_client_and_never_publishes_subscribes_or_saves(self):
        await self.start()
        await self.enable()
        snapshot = self.manager.public()
        content = (self.directory / self.manager.filename).read_bytes()
        result = await self.manager.test(self.payload())
        self.assertTrue(result['connected'])
        probe = self.factory.clients[-1]
        self.assertNotEqual(probe.kwargs['identifier'], self.runtime.core.node)
        self.assertNotIn('will', probe.kwargs)
        self.assertEqual(probe.published, [])
        self.assertEqual(probe.subscribed, [])
        self.assertTrue(probe.exited)
        self.assertEqual(self.manager.public(), snapshot)
        self.assertEqual((self.directory / self.manager.filename).read_bytes(), content)

    async def test_anonymous_probe_and_saved_connection_use_the_same_username_semantics(self):
        await self.start()
        await self.enable(username='', password_action='clear')
        self.assertIsNone(self.factory.clients[-1].kwargs['username'])
        await self.manager.test(self.payload())
        self.assertIsNone(self.factory.clients[-1].kwargs['username'])

    async def test_second_manager_run_does_not_stop_the_owned_connection(self):
        await self.start()
        await self.enable()
        with self.assertRaises(SettingsError):
            await self.manager.run()
        self.assertTrue(self.runtime.mqtt_ready)
        self.assertEqual(len(self.runtime.session_observers), 1)

    async def test_probe_error_and_timeout_are_bounded_and_do_not_disclose_credentials(self):
        await self.enable()
        self.factory.failure = 'server echoed synthetic-password at a private path'
        result = await self.manager.test(self.payload())
        self.assertFalse(result['connected'])
        self.assertNotIn('synthetic-password', json.dumps(result))
        self.factory.failure = None
        self.factory.enter_gate = asyncio.Event()
        self.manager.test_timeout = .01
        result = await asyncio.wait_for(self.manager.test(self.payload()), .2)
        self.assertFalse(result['connected'])

    async def test_save_waits_for_active_mesh_work_before_cancelling_broker_worker(self):
        await self.start()
        await self.enable()
        old = self.factory.clients[-1]
        generation = self.runtime.generation
        await self.runtime.operation.acquire()
        task = asyncio.create_task(self.manager.save(self.payload(enabled=False)))
        await self.until(lambda: self.runtime._foreground_requests > 0)
        self.assertFalse(old.exited)
        self.assertTrue(self.runtime.mqtt_ready)
        self.assertFalse(task.done())
        self.runtime.operation.release()
        await task
        self.assertTrue(old.exited)
        self.assertEqual(self.runtime.generation, generation)
        self.assertFalse(self.runtime.mqtt_ready)
        self.assertEqual(self.runtime.session_observers, [])

    async def test_busy_timeout_preserves_settings_and_existing_connection(self):
        await self.start()
        await self.enable()
        before = self.manager.public()
        self.manager.admission_timeout = .01
        async with self.runtime.operation:
            with self.assertRaises(SettingsError) as caught:
                await self.manager.save(self.payload(enabled=False))
        self.assertEqual(caught.exception.code, 'mesh_busy')
        self.assertEqual(self.manager.public(), before)
        self.assertFalse(self.factory.clients[-1].exited)

    async def test_pending_operation_refuses_settings_before_persistence(self):
        self.store.begin('keypad_edit', 40001, {})
        with self.assertRaises(SettingsError) as caught:
            await self.enable()
        self.assertEqual(caught.exception.code, 'pending_operation')
        self.assertFalse((self.directory / self.manager.filename).exists())

    async def test_concurrent_saves_have_one_winner_and_reconfigure_has_no_observer_leak(self):
        await self.start()
        payload = self.payload(enabled=True, host='broker.example.invalid', port=1883, tls=False)
        results = await asyncio.gather(self.manager.save(payload), self.manager.save(payload), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, SettingsError) and r.code == 'mqtt_settings_changed' for r in results), 1)
        for _ in range(4):
            await self.until(lambda: self.runtime.mqtt_ready)
            await self.manager.save(self.payload())
            self.assertEqual(len(self.runtime.session_observers), 1)
        await self.manager.save(self.payload(enabled=False))
        self.assertEqual(self.runtime.session_observers, [])
        self.assertTrue(all(client.exited for client in self.factory.clients if client.entered))

    async def test_cancelled_http_save_finishes_committed_broker_switch_without_replay(self):
        await self.start()
        await self.enable()
        payload = self.payload(enabled=False)
        self.factory.exit_gate = asyncio.Event()
        task = asyncio.create_task(self.manager.save(payload))
        await self.until(lambda: self.manager.public()['revision'] != payload['revision'])
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.factory.exit_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.runtime.mqtt_ready)
        self.assertFalse(self.runtime.mqtt_enabled)
        self.assertEqual(self.runtime.session_observers, [])
        with self.assertRaises(SettingsError) as caught:
            await self.manager.save(payload)
        self.assertEqual(caught.exception.code, 'mqtt_settings_changed')

    async def test_failed_atomic_replacement_keeps_old_revision_and_removes_tempfile(self):
        await self.enable()
        before = self.manager.public()
        content = (self.directory / self.manager.filename).read_bytes()
        with patch('avionmesh_pi.mqtt_settings.os.replace', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(SettingsError) as caught:
                await self.manager.save(self.payload(enabled=False))
        self.assertEqual(caught.exception.code, 'mqtt_settings_write_failed')
        self.assertEqual(self.manager.public(), before)
        self.assertEqual((self.directory / self.manager.filename).read_bytes(), content)
        self.assertEqual(list(self.directory.glob('.mqtt-settings-*')), [])

    async def test_directory_fsync_failure_reports_uncertain_durability_and_adopts_new_revision(self):
        await self.enable()
        real_fsync = os.fsync
        def fsync(fd):
            if fd == self.manager._fd:
                raise OSError('synthetic directory flush failure')
            real_fsync(fd)
        with patch('avionmesh_pi.mqtt_settings.os.fsync', side_effect=fsync):
            result = await self.manager.save(self.payload(enabled=False))
        self.assertFalse(result['durability_confirmed'])
        stored = json.loads((self.directory / self.manager.filename).read_text())
        self.assertEqual(stored['revision'], self.manager.public()['revision'])
        self.assertFalse(self.runtime.mqtt_enabled)

    async def test_public_or_symlinked_override_is_refused_without_following_it(self):
        await self.enable()
        await self.manager.close()
        path = self.directory / self.manager.filename
        path.chmod(0o644)
        with self.assertRaises(SettingsError):
            MqttSettings(self.runtime, self.directory)
        path.unlink()
        secret = self.directory / 'synthetic-secret'
        secret.write_text('must not be read as settings')
        path.symlink_to(secret)
        with self.assertRaises(OSError):
            MqttSettings(self.runtime, self.directory)
        self.assertEqual(secret.read_text(), 'must not be read as settings')

    async def test_initial_service_configuration_is_not_mutated_or_persisted(self):
        await self.manager.close()
        initial = {'host': 'broker.example.invalid', 'username': 'synthetic', 'password': 'synthetic-password'}
        before = copy.deepcopy(initial)
        self.manager = MqttSettings(self.runtime, self.directory, initial, self.factory)
        self.assertEqual(initial, before)
        self.assertTrue(self.runtime.mqtt_enabled)
        self.assertTrue(self.manager.public()['password_set'])
        self.assertNotIn('synthetic-password', json.dumps(self.manager.public()))
        self.assertFalse((self.directory / self.manager.filename).exists())


if __name__ == '__main__':
    unittest.main()
