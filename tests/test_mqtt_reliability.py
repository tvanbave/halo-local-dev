"""MQTT admission and reconnect regressions with synthetic state and no sockets."""
import asyncio
from contextlib import asynccontextmanager
import json
import sys
import types
import unittest
from unittest.mock import patch

from avionmesh_pi.mqtt import COMMAND_LIFETIME_MS, MqttService
from avionmesh_pi.errors import OperationNotStarted
from avionmesh_pi.protocol import decrypt_mcp
import test_observation_refresh as observations


class Messages:
    def __init__(self):
        self.queue = asyncio.Queue()
    def __aiter__(self):
        return self
    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class Broker:
    def __init__(self, **kwargs):
        self.messages, self.published = Messages(), []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def subscribe(self, topic):
        pass
    async def publish(self, topic, payload, **kwargs):
        self.published.append((topic, payload, kwargs))


class MqttReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await observations.ObservationRefreshTests.asyncSetUp(self)
        self.mqtt = MqttService(self.runtime, {'host': 'synthetic.invalid', 'tls': False})
        self.runtime.sink = self.mqtt.outbox.put
        self.mqtt._connection(True)
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.assertEqual(self.runtime.waiters, [])
        self.assertEqual(self.runtime._foreground_requests, 0)
        self.assertEqual(self.runtime._foreground_waiters, 0)
        self.assertIsNone(self.runtime._background_read_task)
        self.assertFalse(self.runtime.operation.locked())
        await observations.ObservationRefreshTests.asyncTearDown(self)

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def until(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail('Expected asynchronous boundary was not reached')

    def enqueue(self, brightness, target=40001):
        self.mqtt.ingest(self.runtime.core.prefix + '/light/%d/set' % target,
                         json.dumps({'brightness': brightness}).encode(), False)

    def setters(self):
        return [p for _, p in self.mesh.frames if p.payload[0] == 0]

    def send_values(self, target, **values):
        self.mqtt.ingest(f'{self.runtime.core.prefix}/light/{target}/set', json.dumps(values).encode(), False)

    async def test_busy_light_and_group_keep_power_and_independent_colour(self):
        inventory = self.store.inventory()
        inventory['groups'].append(dict(group_id=300, name='Room', members=[40001,40003], protected=False, exposed=True))
        self.store.import_inventory(inventory)
        self.mesh.silent.add(0)  # a group broadcast does not reply as a device
        await self.blocked_background()
        for target in (40001,300):
            self.send_values(target, state='ON', brightness=111)
            self.send_values(target, color_temp=4000)
        self.task(self.mqtt.command_loop())
        self.finish_background()
        await self.until(lambda: self.mqtt.counts['commands_sent'] == 2)
        self.assertEqual([p.payload[1] for p in self.setters()], [29, 10, 29, 10])
        self.assertEqual([p.payload[5] for p in self.setters() if p.payload[1] == 10], [111,111])

    async def test_off_supersedes_unsent_level_and_new_on_uses_its_own_level(self):
        self.send_values(40001, state='ON', brightness=111, color_temp=4000)
        self.send_values(40001, state='OFF')
        self.assertEqual(self.mqtt.commands[40001].values, {'state':'OFF'})
        self.send_values(40001, brightness=70)
        self.assertEqual(self.mqtt.commands[40001].values, {'brightness':70})
        self.send_values(40001, state='ON')
        self.assertEqual(self.mqtt.commands[40001].values, {'state':'ON'})

    async def test_unsupported_adjustments_cannot_poison_switch_or_group_power(self):
        from avionmesh_pi.lifecycle import reload_inventory
        inventory = self.store.inventory()
        inventory['devices'][0]['product_id'] = 167
        inventory['groups'].append(dict(group_id=300, name='Room', members=[40001,40003], protected=False, exposed=True))
        self.store.import_inventory(inventory)
        reload_inventory(self.runtime)
        for target in (40001,300):
            self.send_values(target, brightness=210)
            self.send_values(target, state='ON')
            self.send_values(target, color_temp=4000)
            self.assertEqual(self.mqtt.commands[target].values, {'state':'ON'})
        self.assertEqual(self.mqtt.counts['invalid_command'], 4)

    async def test_new_colour_does_not_extend_expired_brightness_at_dispatch(self):
        await self.blocked_background()
        self.send_values(40001, brightness=111)
        self.now += 2000
        self.send_values(40001, color_temp=4000)
        self.task(self.mqtt.command_loop())
        await self.until(lambda: self.runtime._foreground_waiters == 1)
        self.now += 3200
        self.finish_background()
        await self.until(lambda: self.mqtt.counts['commands_sent'] == 1)
        self.assertEqual([p.payload[1] for p in self.setters()], [29])

    async def blocked_background(self):
        self.mesh.silent.add(40001)
        async def read():
            async with self.runtime._background_read() as admitted:
                self.assertTrue(admitted)
                return await self.runtime.read_light(40001)
        task = self.task(read())
        await self.until(lambda: bool(self.runtime.waiters))
        return task

    def finish_background(self):
        self.mesh.silent.remove(40001)
        self.mesh.reply(40001, b'\x01\x0a\x00\x80')

    async def test_command_survives_three_second_background_read_and_dispatches_once(self):
        await self.blocked_background()
        self.enqueue(77)
        self.task(self.mqtt.command_loop())
        await self.until(lambda: self.runtime._foreground_waiters == 1)
        self.now += 3000
        self.finish_background()
        await self.until(lambda: self.mqtt.counts['commands_sent'] == 1)
        self.assertEqual([p.payload[5] for p in self.setters()], [77])
        self.assertEqual(self.mqtt.counts['command_failed'], 0)

    async def test_latest_coalesced_value_replaces_waiting_intent(self):
        await self.blocked_background()
        self.enqueue(11)
        self.task(self.mqtt.command_loop())
        await self.until(lambda: self.runtime._foreground_waiters == 1)
        self.now += 200
        self.enqueue(99)
        self.finish_background()
        await self.until(lambda: self.mqtt.counts['commands_sent'] == 1)
        self.assertEqual([p.payload[5] for p in self.setters()], [99])
        self.assertEqual(self.mqtt.counts['coalesced_command'], 1)

    async def test_manual_operation_finishes_before_latest_mqtt_intent_dispatches(self):
        await self.runtime.operation.acquire()
        try:
            self.enqueue(11)
            self.task(self.mqtt.command_loop())
            await self.until(lambda: self.runtime._foreground_requests == 1)
            self.assertEqual(self.setters(), [])
            self.now += 2000
            self.enqueue(99)
        finally:
            self.runtime.operation.release()
        await self.until(lambda: self.mqtt.counts['commands_sent'] == 1)
        self.assertEqual([p.payload[5] for p in self.setters()], [99])

    async def test_manual_operation_wait_rechecks_expiry_before_dispatch(self):
        await self.runtime.operation.acquire()
        try:
            self.enqueue(77)
            self.task(self.mqtt.command_loop())
            await self.until(lambda: self.runtime._foreground_requests == 1)
            self.now += COMMAND_LIFETIME_MS + 1
        finally:
            self.runtime.operation.release()
        await self.until(lambda: self.mqtt.counts['expired_command'] == 1)
        self.assertEqual(self.setters(), [])

    async def test_cancelled_mqtt_waiter_does_not_release_manual_owners_lock(self):
        await self.runtime.operation.acquire()
        try:
            self.enqueue(77)
            worker = self.task(self.mqtt.command_loop())
            await self.until(lambda: self.runtime._foreground_requests == 1)
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker
            self.assertTrue(self.runtime.operation.locked())
            self.assertEqual(self.runtime._foreground_requests, 0)
            self.assertEqual(self.setters(), [])
        finally:
            self.runtime.operation.release()

    async def test_mqtt_retains_acquired_lock_when_another_manual_waiter_is_queued(self):
        await self.runtime.operation.acquire()
        self.enqueue(77)
        self.task(self.mqtt.command_loop())
        await self.until(lambda: len(self.runtime.operation._waiters or ()) == 1)
        entered, release = asyncio.Event(), asyncio.Event()
        async def manual():
            async with self.runtime.operation:
                entered.set()
                await release.wait()
        other = self.task(manual())
        await self.until(lambda: len(self.runtime.operation._waiters or ()) == 2)
        self.runtime.operation.release()
        await self.until(entered.is_set)
        try:
            # MQTT was first in line: it must retain ownership through dispatch,
            # not release/reacquire behind this now-indefinitely-held manual lock.
            self.assertEqual(self.mqtt.counts['commands_sent'], 1)
            self.assertEqual(self.runtime._foreground_requests, 0)
            self.assertEqual([p.payload[5] for p in self.setters()], [77])
        finally:
            release.set()
            await other

    async def test_expired_waiting_intent_never_dispatches(self):
        await self.blocked_background()
        self.enqueue(77)
        self.task(self.mqtt.command_loop())
        await self.until(lambda: self.runtime._foreground_waiters == 1)
        self.now += COMMAND_LIFETIME_MS + 1
        self.finish_background()
        await self.until(lambda: self.mqtt.counts['expired_command'] == 1)
        self.assertEqual(self.setters(), [])
        self.assertEqual(self.mqtt.commands, {})

    async def test_expired_first_deadline_does_not_discard_newer_coalesced_intent(self):
        original = self.runtime.foreground_request
        attempts = []
        @asynccontextmanager
        async def admission(**kwargs):
            attempts.append(kwargs['timeout'])
            if len(attempts) == 1:
                self.now += COMMAND_LIFETIME_MS
                self.enqueue(99)
                raise OperationNotStarted('background_read_busy')
            async with original(**kwargs):
                yield
        self.enqueue(11)
        with patch.object(self.runtime, 'foreground_request', side_effect=admission):
            self.task(self.mqtt.command_loop())
            await self.until(lambda: self.mqtt.counts['commands_sent'] == 1)
        self.assertEqual([p.payload[5] for p in self.setters()], [99])
        self.assertEqual(attempts, [5, 5])

    async def test_late_sequence_allocation_rechecks_deadline_before_first_setter(self):
        allocate = self.store.allocate_sequence
        def slow_allocate():
            sequence = allocate()
            self.now += COMMAND_LIFETIME_MS + 1
            return sequence
        self.enqueue(77)
        with patch.object(self.store, 'allocate_sequence', side_effect=slow_allocate):
            self.task(self.mqtt.command_loop())
            await self.until(lambda: self.mqtt.counts['expired_command'] == 1)
        self.assertEqual(self.store.get('sequence'), 1)
        self.assertEqual(self.mesh.frames, [])

    async def test_new_intent_during_allocation_supersedes_old_before_first_setter(self):
        allocate = self.store.allocate_sequence
        def replace_once():
            sequence = allocate()
            if sequence == 1:
                self.enqueue(99)
            return sequence
        self.enqueue(11)
        with patch.object(self.store, 'allocate_sequence', side_effect=replace_once):
            self.task(self.mqtt.command_loop())
            await self.until(lambda: self.mqtt.counts['commands_sent'] == 1)
        self.assertEqual([p.payload[5] for p in self.setters()], [99])
        self.assertEqual(self.mqtt.counts['superseded_command'], 1)

    async def test_broker_epoch_revokes_waiting_intent_without_invalidating_ble(self):
        await self.blocked_background()
        generation = self.runtime.generation
        self.enqueue(77)
        self.task(self.mqtt.command_loop())
        await self.until(lambda: self.runtime._foreground_waiters == 1)
        self.mqtt._connection(False)
        self.mqtt._connection(True)
        self.assertEqual(self.runtime.generation, generation)
        self.assertFalse(self.runtime.waiters[0][1].done())
        self.finish_background()
        await self.until(lambda: self.runtime._foreground_requests == 0)
        self.assertEqual(self.setters(), [])
        self.assertEqual(self.mqtt.counts['broker_commands_dropped'], 1)

    async def test_ble_session_change_revokes_waiting_intent(self):
        background = await self.blocked_background()
        self.enqueue(77)
        self.task(self.mqtt.command_loop())
        await self.until(lambda: self.runtime._foreground_waiters == 1)
        self.runtime.session()
        with self.assertRaises(RuntimeError):
            await background
        await self.until(lambda: self.runtime._foreground_requests == 0)
        self.assertEqual(self.setters(), [])
        self.assertEqual(self.mqtt.commands, {})

    async def test_uncertain_send_is_not_requeued(self):
        sends = []
        async def uncertain(frame):
            sends.append(frame)
            raise RuntimeError('Synthetic uncertain transport submission')
        self.enqueue(77)
        with patch.object(self.mesh, 'send', side_effect=uncertain):
            self.task(self.mqtt.command_loop())
            await self.until(lambda: self.mqtt.counts['command_failed'] == 1)
            for _ in range(5):
                await asyncio.sleep(0)
        self.assertEqual(len(sends), 1)
        self.assertEqual(self.mqtt.commands, {})

    def mqtt_module(self):
        # run() uses only Will when an explicit fake client factory is provided.
        return patch.dict(sys.modules, {'aiomqtt': types.SimpleNamespace(Will=lambda *a, **k: object())})

    async def test_repeated_failed_broker_connections_preserve_inflight_local_read(self):
        self.mesh.silent.add(40001)
        local = self.task(self.runtime.read_light(40001))
        await self.until(lambda: bool(self.runtime.waiters))
        generation, waiter = self.runtime.generation, self.runtime.waiters[0][1]
        attempts = []
        class Failed(Broker):
            async def __aenter__(self):
                attempts.append(True)
                raise OSError('Synthetic broker unavailable')
        self.mqtt.client_factory = Failed
        sleep = asyncio.sleep
        async def fast_backoff(_):
            await sleep(0)
        with self.mqtt_module(), patch('avionmesh_pi.mqtt.asyncio.sleep', side_effect=fast_backoff):
            service = self.task(self.mqtt.run())
            await self.until(lambda: len(attempts) >= 3)
            service.cancel()
            await asyncio.gather(service, return_exceptions=True)
        self.assertEqual(self.runtime.generation, generation)
        self.assertFalse(waiter.done())
        self.finish_background()
        result = await local
        self.assertTrue(result['complete'])
        self.assertTrue(self.runtime.mesh_ready)

    async def test_broker_loss_does_not_cancel_admitted_radio_send_or_replay_queue(self):
        broker = Broker()
        self.mqtt.client_factory = lambda **kwargs: broker
        entered, release = asyncio.Event(), asyncio.Event()
        original_send, cancelled = self.mesh.send, []
        async def slow_setter(frame):
            packet = decrypt_mcp(frame, observations.KEY)
            if packet.payload[0] == 0:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise
            await original_send(frame)
        with self.mqtt_module(), patch.object(self.mesh, 'send', side_effect=slow_setter):
            self.task(self.mqtt.run())
            await self.until(lambda: self.mqtt.broker_epoch >= 2)
            self.enqueue(77)
            await self.until(entered.is_set)
            generation = self.runtime.generation
            self.enqueue(99)
            broker.messages.queue.put_nowait(None)
            await self.until(lambda: not self.runtime.mqtt_ready)
            self.assertEqual(cancelled, [])
            self.assertEqual(self.runtime.generation, generation)
            release.set()
            await self.until(lambda: self.mqtt.counts['commands_sent'] == 1)
        self.assertEqual([p.payload[5] for p in self.setters()], [77])
        self.assertEqual(self.mqtt.commands, {})

    async def test_unexpected_command_worker_failure_stops_mqtt_service(self):
        self.mqtt.client_factory = Broker
        async def broken():
            raise LookupError('Synthetic worker failure')
        sleep = asyncio.sleep
        async def fast_backoff(_):
            await sleep(0)
        with self.mqtt_module(), patch.object(self.mqtt, 'command_loop', side_effect=broken), \
                patch('avionmesh_pi.mqtt.asyncio.sleep', side_effect=fast_backoff):
            with self.assertRaisesRegex(LookupError, 'Synthetic worker failure'):
                await self.mqtt.run()
        self.assertFalse(self.runtime.mqtt_ready)

    async def test_ha_birth_republishes_current_state_without_events_or_new_observation_time(self):
        self.mesh.reply(40001, b'\x01\x0a\x00\x80')
        before, generation = self.runtime.local_light_states.copy(), self.runtime.generation
        self.runtime.controller_events[40100] = ({'event_type': 'on'}, self.now)
        self.mqtt.outbox.states.clear()
        self.mqtt.outbox.events.clear()
        self.mqtt.ingest('homeassistant/status', b'online', False)
        state = self.mqtt.outbox.states[self.runtime.core.prefix + '/light/40001/state']
        self.assertEqual(state['payload']['brightness'], 128)
        self.assertFalse(self.mqtt.outbox.events)
        self.assertEqual(self.runtime.local_light_states, before)
        self.assertEqual(self.runtime.generation, generation)


if __name__ == '__main__':
    unittest.main()
