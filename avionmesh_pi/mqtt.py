"""Bounded MQTT delivery: retained state coalesces; events expire and never replay."""
import asyncio
from collections import Counter, OrderedDict, deque
import json
import ssl
import re
from typing import NamedTuple

from .errors import OperationNotStarted
from .scene_mqtt import exposed_scene, cleanup_delivered
from .protocol import integer


# A current background read can need three seconds to time out. Commands get a
# bounded five-second admission window; events still expire after one second.
COMMAND_LIFETIME_MS = 5000
# Scene activation first surveys reference ownership. Commands remain revocable
# across broker/session changes until the one recall dispatch.
SCENE_COMMAND_LIFETIME_MS = 120000


class QueuedCommand(NamedTuple):
    received: int
    generation: int
    epoch: int
    values: dict
    field_times: dict


class _CommandNotStarted(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class Outbox:
    def __init__(self, clock, capacity=256, event_capacity=None, timings=None):
        self.clock, self.capacity = clock, capacity
        self.event_capacity=capacity if event_capacity is None else event_capacity
        self.states = OrderedDict()
        self.state_delivery = {}
        self.delivery = (0, ())
        self.timings = timings
        self.events = deque()
        self.online = False
        self.counts = Counter()
        self.wake = asyncio.Event()
        self.burst = 0

    def connection(self, online):
        self.online = online
        self.counts["disconnected_events"] += len(self.events)
        self.events.clear()
        self.state_delivery.clear()
        self.states.clear()  # caller republishes current discovery/state, not old values
        self.wake.set()

    def put(self, message):
        if not self.online:
            self.counts["offline_dropped"] += 1
            return
        topic = message["topic"]
        if message["retain"]:
            if topic not in self.states and len(self.states) >= self.capacity:
                self.counts["state_overflow"] += 1
                return
            self.states[topic] = message
            self.state_delivery[topic] = (self.clock(), self.timings.publication_tokens(message) if self.timings else ())
        elif len(self.events) < self.event_capacity:
            self.events.append((self.clock(), message))
        else:
            self.counts["event_overflow"] += 1
        self.wake.set()

    def take(self):
        while self.events and self.clock() - self.events[0][0] > 1000:
            self.events.popleft()
            self.counts["expired_events"] += 1
        if self.events and (self.burst < 8 or not self.states):
            self.burst += 1
            queued, message = self.events.popleft()
            self.delivery = (queued, ())
            return message
        if self.states:
            self.burst = 0
            topic, message = self.states.popitem(last=False)
            self.delivery = self.state_delivery.pop(topic, (self.clock(), ()))
            return message
        self.wake.clear()
        return None


class MqttService:
    def __init__(self, runtime, config, client_factory=None):
        self.runtime, self.config = runtime, config
        self.client_factory = client_factory
        self.outbox = Outbox(runtime.clock, capacity=65536,event_capacity=256, timings=runtime.command_timings)
        self.commands = OrderedDict()
        self.broker_epoch = 0
        self.scene_cleanup_acks = {}
        self.command_wake = asyncio.Event()
        self.counts = Counter()
        runtime.session_observers.append(self.invalidate_session)
        runtime.mqtt_metrics=lambda:{'counters':dict(self.counts),'outbox':dict(self.outbox.counts),
            'queued_commands':len(self.commands),'queued_states':len(self.outbox.states),
            'queued_events':len(self.outbox.events)}

    def invalidate_session(self):
        self.counts['session_commands_dropped']+=len(self.commands)
        self.commands.clear();self.command_wake.clear()
        self.outbox.counts['session_events_dropped']+=len(self.outbox.events)
        self.outbox.events.clear()

    def _connection(self, ready):
        self.scene_cleanup_acks.clear()
        if ready or self.runtime.mqtt_ready:
            self.broker_epoch += 1
        if not ready:
            self.counts['broker_commands_dropped'] += len(self.commands)
            self.commands.clear()
            self.command_wake.clear()
        self.outbox.connection(ready)
        # Broker delivery and BLE operation generations are independent. Even a
        # failed broker connection must not revoke local verification/readbacks.
        self.runtime.mqtt_connection(ready)

    def ingest(self, topic, payload, retained):
        prefix = self.runtime.core.prefix
        if topic == "homeassistant/status" and payload == b"online":
            if self.runtime.mqtt_ready:
                self.runtime.emit(self.runtime.discovery())
                self.runtime.publish_current_states()
            return
        if retained:
            self.counts["retained_rejected"] += 1
            return
        if not self.runtime.mesh_ready or not self.runtime.mqtt_ready:
            self.counts['offline_command']+=1
            return
        parts = topic.split("/")
        scene_command = topic.startswith(prefix + "/scene/")
        if len(parts) != 5 or parts[-1] != "set":
            return
        if scene_command:
            identifier = parts[-2]
            if (not re.fullmatch(r'[0-9a-f]{32}', identifier) or payload != b'ON'
                    or not exposed_scene(self.runtime, identifier)):
                self.counts['invalid_command'] += 1
                return
            target, values = ('scene', identifier), {'scene': identifier}
        else:
            if not topic.startswith(prefix + "/light/"):
                return
            try:
                if len(payload) > 2048:
                    raise ValueError("Command too large")
                target = int(parts[-2])
                values = json.loads(payload)
                if not isinstance(values, dict) or not values or values.keys() - {'state', 'brightness', 'color_temp'}:
                    raise ValueError("Invalid light command")
                if 'state' in values and values['state'] not in ('ON', 'OFF'):
                    raise ValueError("Invalid power state")
                if 'brightness' in values:
                    integer(values['brightness'], 0, 255, 'brightness')
                if 'color_temp' in values:
                    integer(values['color_temp'], 2700, 5000, 'color temperature')
                self.runtime.check_light_features(target, values)
            except (ValueError, TypeError, RecursionError):
                self.counts["invalid_command"] += 1
                return
        if target not in self.commands and len(self.commands) >= 128:
            self.counts["command_overflow"] += 1
            return
        now = self.runtime.clock()
        field_times = dict.fromkeys(values, now)
        previous = self.commands.get(target)
        if previous is not None:
            self.counts['coalesced_command'] += 1
            if (not scene_command and previous.generation == self.runtime.generation and previous.epoch == self.broker_epoch
                    and values.get('state') != 'OFF' and values.get('brightness') != 0):
                # Power and level describe one adjustment. Colour is independent.
                replaced = set(values)
                if 'state' in values or 'brightness' in values:
                    replaced.update(('state', 'brightness'))
                for field, when in previous.field_times.items():
                    if field not in replaced and 0 <= now - when <= COMMAND_LIFETIME_MS:
                        values[field] = previous.values[field]
                        field_times[field] = when
        self.commands[target] = QueuedCommand(now, self.runtime.generation, self.broker_epoch, values, field_times)
        self.command_wake.set()

    def _command_lifetime(self, target):
        return SCENE_COMMAND_LIFETIME_MS if isinstance(target, tuple) else COMMAND_LIFETIME_MS

    def _admit_command(self, target, intent):
        when, generation, epoch = intent.received, intent.generation, intent.epoch
        if epoch != self.broker_epoch or not self.runtime.mqtt_ready:
            raise _CommandNotStarted('broker_commands_dropped')
        if generation != self.runtime.generation or not self.runtime.mesh_ready:
            raise _CommandNotStarted('session_commands_dropped')
        if isinstance(target, tuple) and not exposed_scene(self.runtime, target[1]):
            raise _CommandNotStarted('scene_unavailable')
        if not 0 <= self.runtime.clock() - when <= self._command_lifetime(target):
            raise _CommandNotStarted('expired_command')
        if target in self.commands and self.commands[target] is not intent:
            raise _CommandNotStarted('superseded_command')
        # An unrelated fresh update cannot extend the lifetime of an older field.
        now = self.runtime.clock()
        fresh = {k: ts for k, ts in intent.field_times.items() if 0 <= now - ts <= self._command_lifetime(target)}
        if len(fresh) != len(intent.field_times):
            if not fresh:
                raise _CommandNotStarted('expired_command')
            self.commands[target] = intent._replace(values={k: intent.values[k] for k in fresh}, field_times=fresh)
            raise _CommandNotStarted('superseded_command')

    async def command_loop(self):
        while True:
            await self.command_wake.wait()
            if not self.commands:
                self.command_wake.clear()
                continue
            # Keep the intent queued while waiting: a newer value can coalesce,
            # and a broker/BLE boundary can discard it before any radio work.
            target, waiting = next(iter(self.commands.items()))
            intent = waiting
            try:
                self._admit_command(target, waiting)
                remaining = max(.001, (self._command_lifetime(target) - (self.runtime.clock() - waiting[0])) / 1000)
                async with self.runtime.foreground_request(timeout=remaining):
                    intent = self.commands.get(target)
                    if intent is None:
                        continue
                    self._admit_command(target, intent)
                    remaining = max(.001, (self._command_lifetime(target) - (self.runtime.clock() - intent[0])) / 1000)
                    def admit_first_setter():
                        self._admit_command(target, intent)
                        if self.commands.get(target) is intent:
                            del self.commands[target]
                    # Actual lock acquisition is bounded and retained by control.
                    # Keep this intent coalescible until the final pre-setter hook;
                    # after that removal it is never requeued after uncertainty.
                    if isinstance(target, tuple):
                        await self.runtime.scene_engine.activate(target[1], admission=admit_first_setter)
                    else:
                        await self.runtime.control(target, intent[3], confirm=False, generation=intent[1],
                            admission=admit_first_setter, admission_timeout=remaining, received_ms=intent[0])
                    self.counts["commands_sent"] += 1
            except _CommandNotStarted as error:
                if self.commands.get(target) is intent:
                    del self.commands[target]
                self.counts[error.reason] += 1
            except OperationNotStarted as error:
                # A replacement intent has its own fresh deadline and remains
                # queued; the original request is not replayed.
                if self.commands.get(target) is intent:
                    del self.commands[target]
                    self.counts['expired_command' if error.code in ('background_read_busy', 'mesh_busy') else 'command_failed'] += 1
            except (ValueError, RuntimeError, asyncio.TimeoutError):
                if self.commands.get(target) is intent:
                    del self.commands[target]
                self.counts["command_failed"] += 1

    async def publish_loop(self, client):
        while True:
            await self.outbox.wake.wait()
            message = self.outbox.take()
            if message is None:
                continue
            payload = message["payload"]
            if not isinstance(payload, str):
                payload = json.dumps(payload, separators=(",", ":"))
            # Pop before sending. Events are never replayed; durable retained
            # scene cleanup is regenerated on reconnect until acknowledged.
            queued_ms, timing_tokens = self.outbox.delivery
            begun_ms = self.runtime.clock()
            epoch = self.broker_epoch
            await client.publish(message["topic"], payload, qos=message["qos"], retain=message["retain"])
            if epoch == self.broker_epoch and self.runtime.mqtt_ready:
                cleanup_delivered(self.runtime, message, self.scene_cleanup_acks)
            self.runtime.command_timings.published(timing_tokens, queued_ms, begun_ms, self.runtime.clock())

    async def receive_loop(self, client):
        async for message in client.messages:
            self.ingest(str(message.topic), bytes(message.payload), message.retain)

    async def _publish_offline(self, client, prefix):
        # Own and retrieve this task even if shutdown interrupts it. Using wait
        # avoids wait_for's completion/cancellation race in older asyncio versions.
        task = asyncio.create_task(client.publish(prefix + '/availability', 'offline', qos=1, retain=True))
        try:
            done, _ = await asyncio.wait([task], timeout=2)
            for completed in done:
                completed.result()
        except Exception:
            pass
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run(self):
        import aiomqtt
        from .mqtt_socket import make_client
        factory = self.client_factory or make_client
        prefix = self.runtime.core.prefix
        cfg = self.config
        context = ssl.create_default_context(cafile=cfg.get("ca_file")) if cfg.get("tls", True) else None
        delay = 1
        # Broker loss must not cancel a command halfway through a GATT frame.
        # One supervised command worker survives reconnects, while its queued
        # intents are revoked on every delivery boundary. Shutdown still owns it.
        command_task = asyncio.create_task(self.command_loop())
        try:
            while True:
                if command_task.done():
                    command_task.result()
                    raise RuntimeError('MQTT command worker stopped')
                tasks = []
                try:
                    async with factory(hostname=cfg["host"], port=cfg.get("port", 8883 if context else 1883),
                        identifier=self.runtime.core.node, username=cfg.get("username"), password=cfg.get("password"),
                        tls_context=context, max_queued_incoming_messages=256,
                        will=aiomqtt.Will(prefix + "/availability", "offline", qos=1, retain=True)) as client:
                        await client.subscribe(prefix + "/light/+/set")
                        await client.subscribe(prefix + "/scene/+/set")
                        await client.subscribe("homeassistant/status")
                        self._connection(True)
                        self.runtime.emit(self.runtime.discovery())
                        delay = 1
                        tasks = [asyncio.create_task(self.publish_loop(client)),
                                 asyncio.create_task(self.receive_loop(client))]
                        try:
                            done, _ = await asyncio.wait([*tasks, command_task], return_when=asyncio.FIRST_COMPLETED)
                            for task in done:
                                task.result()
                        finally:
                            # Revoke queued admission before awaiting connection
                            # cleanup, without touching already-started radio work.
                            self._connection(False)
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                            await self._publish_offline(client, prefix)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.counts["reconnects"] += 1
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    self._connection(False)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        finally:
            self._connection(False)
            command_task.cancel()
            await asyncio.gather(command_task, return_exceptions=True)
