"""Async orchestration with explicit transport/sink seams for offline testing."""
import asyncio
import json
import time
from collections import OrderedDict
from contextlib import asynccontextmanager

from .bridge import Bridge, CONTROLLERS, LIGHTS, COLOR_LIGHTS, load_devices
from .controllers import binding_status, named_inputs
from .diagnostics import Diagnostics
from .relay_identity import RelayIdentity
from .mesh_reports import MeshReports
from .timing import CommandTimings
from .errors import OperationNotStarted
from .scene_mqtt import publications as scene_publications
from .migration import status as sequence_status
from .protocol import decrypt_mcp, encrypt_mcp, model_command, plan_light_command, integer, decode_light, decode_controller


GROUP_COMMAND_SETTLE_MS = 150
TARGETED_FEEDBACK_GAP = 0.15
FEEDBACK_RECHECK_MS = 600
BLUETOOTH_STARTUP_FAILURE_NOTICE = 3
UNANSWERED_POWER_READ_NOTICE = 3


class _BackgroundReadInterrupted(RuntimeError):
    """Foreground work took priority after a read transmission finished."""


class _ReplyTimeout(asyncio.TimeoutError):
    """The transport accepted the request, but the reply wait expired."""


class _OperationLock(asyncio.Lock):
    """Identify the task that acquired the radio for controller availability."""

    acquired_by = None

    async def acquire(self):
        await super().acquire()
        self.acquired_by = asyncio.current_task()
        return True

    def release(self):
        super().release()
        self.acquired_by = None


class Runtime:
    def __init__(self, store, key, transport, node="pi_lab", clock=None):
        self.store, self.key, self.transport = store, key, transport
        self.clock = clock or (lambda: int(time.monotonic() * 1000))
        self.core = Bridge(load_devices(store.inventory()["devices"]), key, node)
        self.core.controllers.local_modes = set(store.get("local_modes", []))
        self.sink = lambda message: None
        self.waiters = []
        self.generation = 0
        self.session_observers = []
        self.operation = _OperationLock()
        # A new runtime owns no prior association task/queue. Only a disconnected
        # transport may establish this startup recovery boundary.
        client = getattr(transport, 'client', None)
        self.claim_isolated = ({j['id'] for j in store.journals(pending=True)}
            if getattr(transport, 'ready', True) is False and not (client and client.is_connected) else set())
        self.mesh_ready = False
        self._bluetooth_startup_failures = None
        self.mqtt_ready = False
        self.mqtt_enabled = False
        self.last_error = None
        self.receive_count = 0
        self.raw_observers = []
        self.packet_observers = []
        self.native_observations = {}
        self.native_sequences = {}
        self._feedback_sequences = {}
        self.controller_events = {}
        self.controller_since = None
        self.local_light_states = {}
        self._display_light_history = {}  # Presentation only; never command or MQTT evidence.
        self._initial_light_deadlines = {}  # Bounded first-status display grace, not evidence.
        self._unanswered_power_reads = {}  # Presentation only, scoped to this mesh session.
        self.startup_read_progress = None
        self.startup_broadcast_progress = None
        self.observation_refresh_progress = None
        self.keypad_binding_refresher = None
        self._observation_task = None
        self._observation_requested = asyncio.Event()
        # Read-only follow-up work is coalesced by light, never a saved setter.
        self._group_refresh = {}
        self._feedback_fields = {}
        self._feedback_checks = {}
        self._group_refresh_revision = 0
        self._group_refresh_due = 0
        self._group_refresh_key = None
        self._foreground_requests = 0
        self._foreground_waiters = 0
        self._foreground_pending = asyncio.Event()
        self._background_read_task = None
        self._background_read_idle = asyncio.Event()
        self._background_read_idle.set()
        self._baseline_token = 0
        self.local_baseline_query = None
        self.reply_seen = OrderedDict()
        self.capabilities = {}
        self.diagnostics = Diagnostics(self.clock)
        self.relay_identity = RelayIdentity(store, lambda source: getattr(self.core.devices.get(source), 'product_id', None))
        self.mesh_reports = MeshReports()
        self.command_timings = CommandTimings(self.clock, self.core.prefix)
        from .scene_engine import SceneEngine
        self.scene_engine = SceneEngine(self)

    def emit(self, messages):
        for message in messages:
            self.sink(message)

    def quarantined(self, journals=None):
        if journals is None:
            journals = self.store.journals(pending=True)
        targets = {j['target'] for j in journals
                if j['kind'] in ('remove', 'external_reset', 'keypad_setup', 'accessory_setup','accessory_native_setup', 'anyplace_setup', 'group_membership', 'dimmer_edit')}
        for journal in journals:
            if journal['kind'] == 'scene':
                targets.update(journal['detail'].get('targets', []))
        return targets

    def quarantined_groups(self):
        return {j['detail']['group'] for j in self.store.journals(pending=True)
                if j['kind'] == 'group_membership'}

    def scene_changed(self):
        """Republish current scene metadata after a durable transition; no radio."""
        self.emit(scene_publications(self, discovery=True))

    def scene_activated(self, scene):
        # Recall is command evidence only. Refresh member observations without
        # fabricating the requested levels or sending per-device setters.
        self._queue_light_refresh(scene['reference'], None,
            [member['target'] for member in scene['members']],
            fields=('brightness', 'color_temp'))

    def session(self, *, preserve_lights=()):
        # A disconnected radio invalidates proof, not the fact that a value was
        # previously observed. Keep a separate, explicitly stale display history.
        self._remember_display_readings()
        # Inventory-only boundaries still invalidate operations/controller proofs,
        # but do not erase unchanged light readings on a healthy physical link.
        retained = {target: self._light_observations(target) for target in preserve_lights
                    if self.mesh_ready and self.transport.ready and target in self.core.devices
                    and self.core.devices[target].product_id in LIGHTS
                    and target not in self.quarantined()}
        self.generation += 1
        self._unanswered_power_reads.clear()
        self._group_refresh.clear()
        self._feedback_fields.clear()
        self._feedback_checks.clear()
        self._group_refresh_due = 0
        self._group_refresh_key = None
        self._baseline_token = 0
        self.local_baseline_query = None
        for _,future,_ in tuple(self.waiters):
            if not future.done():future.set_exception(RuntimeError('Mesh session invalidated'))
        for observer in tuple(self.session_observers):observer()
        self.diagnostics.boundary()
        self.command_timings.boundary()
        self.native_observations.clear()
        self.native_sequences.clear()
        self._feedback_sequences.clear()
        self.controller_events.clear()
        self.controller_since = self.clock() if self.mesh_ready and self.transport.ready else None
        self.local_light_states.clear()
        connection = self.core.connection(self.mesh_ready and self.mqtt_ready, self.clock())
        if retained and self.mesh_ready and self.transport.ready:
            self.local_light_states.update(retained)
            self.core.states.update({target: dict(state) for target, state in retained.items()
                                     if self.core.devices[target].exposed})
            # connection() produced offline light availability before restoration.
            # Publish the actual retained freshness instead, with no offline flash.
            light_topics = {f'{self.core.prefix}/light/{target}/availability' for target in self.core.devices}
            connection = [m for m in connection if m['topic'] not in light_topics]
            connection.extend(self._light_publications(include_state=False))
        self.emit(connection)
        self.store.invalidate_bindings()
        self.publish_groups()
        self.emit(scene_publications(self))
        self._observation_requested.set()

    def mqtt_connection(self, ready):
        """Change publication availability without invalidating the BLE session.

        Broker reconnects republish fresh retained observations, never controller
        events. Physical session boundaries still belong to session().
        """
        if type(ready) is not bool:
            raise ValueError('Expected boolean MQTT ready state')
        previously_ready = self.mqtt_ready
        self.mqtt_ready = ready
        online = ready and self.mesh_ready and self.transport.ready
        if not online:
            self.core.ready_since = None
        elif not previously_ready or self.core.ready_since is None:
            self.core.ready_since = self.clock()
        self.emit([self.core.intent(self.core.prefix + suffix,
            'online' if online else 'offline', True)
            for suffix in ('/availability', '/mesh/availability')])
        if ready:
            self.publish_current_states()

    def _light_observations(self, target):
        """Newest accepted value per field, with its original receipt time."""
        state = dict(self.core.states.get(target, {}))
        for field, observation in self.local_light_states.get(target, {}).items():
            if field not in state or observation[1] >= state[field][1]:
                state[field] = observation
        return state

    def _light_publications(self, *, include_state=True, target=None):
        now, quarantined = self.clock(), self.quarantined()
        online = self.mesh_ready and self.transport.ready and self.mqtt_ready
        messages = []
        for device in self.core.devices.values():
            if (not device.exposed or device.product_id not in LIGHTS
                    or target is not None and device.device_id != target):
                continue
            fresh = {field: value for field, (value, when) in self._light_observations(device.device_id).items()
                     if field in ('state', 'brightness', 'color_temp') and 0 <= now - when < 120000}
            available = online and device.device_id not in quarantined and 'brightness' in fresh
            topic = f'{self.core.prefix}/light/{device.device_id}'
            messages.append(self.core.intent(topic + '/availability', 'online' if available else 'offline', True))
            if available and include_state:
                state = {'state': 'ON' if fresh['brightness'] else 'OFF', 'brightness': fresh['brightness']}
                if device.product_id in COLOR_LIGHTS:
                    state.update(color_temp=fresh.get('color_temp'), color_mode='color_temp')
                messages.append(self.core.intent(topic + '/state', state, True))
        return messages

    def publish_current_states(self):
        """Republish retained state after broker/HA reconnect; no radio or events."""
        if not self.mqtt_ready:
            return
        self.emit(self._light_publications())
        self.publish_groups()
        self.emit(scene_publications(self))

    def status(self):
        return {"mesh_ready": self.mesh_ready, "mqtt_ready": self.mqtt_ready,
                "relay_scan_number": getattr(self.transport, "scan_number", None),
                "relay_identity": self.relay_identity.recorded,
                "relay_extended_scan": self.transport.extended_status() if hasattr(self.transport, "extended_status") else None,
                "bluetooth_starting": self._bluetooth_startup_failures is not None,
                "bluetooth_startup_retrying": (self._bluetooth_startup_failures or 0) >= BLUETOOTH_STARTUP_FAILURE_NOTICE,
                "mqtt_enabled": self.mqtt_enabled,
                "commissioning_only": getattr(self, "commissioning_only", False),
                "preview_only": getattr(self, "preview_only", False),
                "bootstrap": self.bootstrap.status() if hasattr(self, "bootstrap") else {"enabled": False},
                "receive_count": self.receive_count, "diagnostics":self.diagnostics.summary(), "counters": dict(self.core.counts),
                "transport_dropped": self.transport.dropped,
                "transport_global_counters":dict(getattr(self.transport,"counts",{})),
                "command_timings": self.command_timings.summary(),
                "mqtt_metrics":self.mqtt_metrics() if hasattr(self,"mqtt_metrics") else {}, "last_error": self.last_error,
                "pending_transactions": len(self.store.journals(pending=True)),
                "publication_pending": sum(j['kind'] in ('remove', 'external_reset')
                    and j['status'] == 'verified' and j['detail'].get('publication_pending') is True
                    for j in self.store.journals()),
                "sequence_history": sequence_status(self.store),
                "startup_broadcast": dict(self.startup_broadcast_progress) if self.startup_broadcast_progress else None,
                "observation_refresh": dict(self.observation_refresh_progress) if self.observation_refresh_progress else None,
                "keypad_binding_refresh": dict(self.keypad_binding_refresher.progress) if self.keypad_binding_refresher else None,
                "background_read_active": self._background_read_task is not None,
                "foreground_waiters": self._foreground_waiters,
                "foreground_requests": self._foreground_requests,
                "capabilities": {"light_control": True, "group_control": True,
                    "local_control_independent_of_ha": True,
                    "native_scenes": self.capabilities.get("native_scenes", True),
                    "group_membership_readback": self.capabilities.get("group_membership_readback", False),
                    "verified_group_membership": self.capabilities.get("group_membership_readback", False) and self.capabilities.get("group_membership", False),
                    "keypad_reads": True, "keypad_editor": self.capabilities.get("wired_editor", False),
                    "bulk_claim": self.capabilities.get("ordinary_claim", False) and self.capabilities.get("bulk_claim", False),
                    "claim_reset_recovery": self.capabilities.get("claim_reset_recovery", False),
                    "claim": self.capabilities.get("ordinary_claim", False), "unclaim": self.capabilities.get("removal", False),
                    "automatic_controller_setup": any(self.capabilities.get(k,False) for k in ("wired_onboarding","battery_onboarding","accessory_onboarding","anyplace_onboarding")), "cloud_sync": self.capabilities.get("cloud_sync",False)}}

    def note_added_lights(self, targets):
        now = self.clock()
        self._initial_light_deadlines = {t: end for t, end in self._initial_light_deadlines.items()
            if t in self.core.devices and now < end}
        for target in targets:
            device = self.core.devices.get(target)
            if (device and device.product_id in LIGHTS
                    and not {'state', 'brightness'} & self._light_observations(target).keys()):
                self._initial_light_deadlines.setdefault(target, now + 120000)

    def initial_light_status(self):
        """Brief first-reading grace for new lights; survives a page refresh only."""
        now, quarantined = self.clock(), self.quarantined()
        return {str(target): {'remaining_ms': end - now}
            for target, end in self._initial_light_deadlines.items()
            if target in self.core.devices and self.core.devices[target].product_id in LIGHTS
            and target not in quarantined and 0 < end - now <= 120000
            and not {'state', 'brightness'} & self._light_observations(target).keys()}

    def power_read_status(self):
        """Explain missing power readings without inferring reachability from polling order."""
        if not (self.mesh_ready and self.transport.ready):
            return {}
        refresh = self.observation_refresh_progress or {}
        startup = self.startup_read_progress or {}
        polling = (self._observation_task is not None and refresh.get('state') != 'failed'
                   or startup.get('state') in ('waiting', 'running'))
        pending = self.store.journals(pending=True)
        blocked = self.quarantined(pending) | {j['target'] for j in pending}
        result = {}
        for device in self.core.devices.values():
            target = device.device_id
            if (device.product_id not in LIGHTS or target in blocked
                    or {'state', 'brightness'} & self._light_observations(target).keys()):
                continue
            misses = self._unanswered_power_reads.get(target, 0)
            if misses >= UNANSWERED_POWER_READ_NOTICE:
                result[str(target)] = 'no_reply'
            elif misses:
                result[str(target)] = 'retry_pending' if self._observation_task is not None else 'unconfirmed'
            elif polling:
                result[str(target)] = 'waiting'
        return result

    def _remember_display_readings(self):
        history = {}
        quarantined = self.quarantined()
        for device in self.core.devices.values():
            target = device.device_id
            if device.product_id not in LIGHTS or target in quarantined:
                continue
            signature = (device.product_id, device.exposed)
            previous_signature, previous = self._display_light_history.get(target, (None, {}))
            values = dict(previous) if previous_signature == signature else {}
            current = self._light_observations(target)
            if {'state', 'brightness'} & current.keys():
                values.pop('state', None); values.pop('brightness', None)
            for field, reading in current.items():
                if field in ('state', 'brightness', 'color_temp') and (field not in values or reading[1] >= values[field][1]):
                    values[field] = reading
            if values:
                history[target] = (signature, values)
        self._display_light_history = history

    def last_known_device_states(self):
        """Display-only fallback across reconnects; excluded from all control proofs."""
        now, quarantined, result = self.clock(), self.quarantined(), {}
        for target, (signature, values) in self._display_light_history.items():
            device = self.core.devices.get(target)
            if (device is None or device.product_id not in LIGHTS or target in quarantined
                    or signature != (device.product_id, device.exposed)):
                continue
            current = self._light_observations(target)
            # A new power report wins as a whole, including a partial ON/OFF reply.
            fields = {f: v for f, v in values.items() if f not in current
                      and not (f in ('state', 'brightness') and {'state', 'brightness'} & current.keys())}
            if fields:
                result[str(target)] = {f: {'value': value, 'observed_ms': when,
                    'age_ms': now - when, 'fresh': False}
                    for f, (value, when) in fields.items() if now >= when}
        return result

    def device_states(self):
        """Read-only observations, with per-field age; never imply a requested state."""
        now, quarantined, result = self.clock(), self.quarantined(), {}
        for device in self.core.devices.values():
            if device.product_id not in LIGHTS or device.device_id in quarantined:
                continue
            state = self._light_observations(device.device_id)
            if state:
                result[str(device.device_id)] = {
                    field: {"value": value, "observed_ms": when, "age_ms": now - when,
                            "fresh": 0 <= now - when < 120000}
                    for field, (value, when) in state.items()
                    if field in ("state", "brightness", "color_temp")}
        return result

    def observe(self, frame, received_ms):
        now = self.clock()
        if not self.mesh_ready or not self.transport.ready or not 0 <= now - received_ms <= 1000:
            return
        for observer in tuple(self.raw_observers):
            observer(frame, received_ms)
        try:
            packet = decrypt_mcp(frame, self.key)
        except ValueError:
            self.core.counts["invalid_frame"] += 1
            return
        self.receive_count += 1
        self.diagnostics.observe(packet, received_ms, wire_length=len(frame), trailer=frame[-1])
        if not getattr(self, 'maintenance', False):
            try:
                self.relay_identity.observe(packet.crypto_source, packet.sequence, frame[-1])
            except Exception:
                self.core.counts['relay_identity_failed'] += 1  # naming a relay must never disturb receiving
            try:
                self.mesh_reports.observe(packet.crypto_source, packet.sequence, frame[-1], received_ms,
                                          getattr(self.transport, 'address', None))
            except Exception:
                self.core.counts['mesh_report_failed'] += 1  # display readings must never disturb receiving
        while self.reply_seen and now - next(iter(self.reply_seen.values())) >= 5000:
            self.reply_seen.popitem(last=False)
        replay_key = frame[:-1]  # authenticate first; exclude mutable trailer
        duplicate_reply = replay_key in self.reply_seen
        device = self.core.devices.get(packet.crypto_source)
        if (device and device.product_id == 91 and device.device_id in self.core.controllers.local_modes
                and packet.opcode == 115 and len(packet.payload) == 4
                and packet.payload[:2] == b'\x01\x0a' and packet.payload[2] != 0):
            query = self.local_baseline_query
            if (query is None or query[:3] != (packet.crypto_source, packet.payload[2], self.generation)
                    or received_ms < query[3]
                    or packet.mcp_address not in (0x8000, packet.crypto_source)):
                return
        event = decode_controller(packet, device.product_id) if device and device.product_id in CONTROLLERS else None
        if event and device.product_id in (81, 91) and 'brightness' in event:
            # Assignment evidence must be source-ordered before it reaches
            # baselines, contradiction observers, operation waiters or the core.
            # The core has a smaller duplicate ring, so duplicates also stop here.
            previous_sequence = self.native_sequences.get(packet.crypto_source)
            if (duplicate_reply or previous_sequence is not None
                    and not 0 < ((packet.sequence - previous_sequence) & 0xffffff) < 0x800000):
                return
            self.native_sequences[packet.crypto_source] = packet.sequence
        if not duplicate_reply:
            self.reply_seen[replay_key] = now
            if len(self.reply_seen) > 128:
                self.reply_seen.popitem(last=False)
            if event and ('brightness' in event or 'delta' in event):
                self.store.reserve_targets([event.get('target')])
                self.store.observe_target(event.get('target'),packet.crypto_source)
                if (device.product_id in (81,91) and 'brightness' in event
                        and (256 <= event.get('target', 0) <= 24575 or
                             device.product_id == 91 and event.get('target') in self.core.devices
                             and self.core.devices[event['target']].product_id in LIGHTS)):
                    previous = self.native_observations.get(packet.crypto_source)
                    generation = previous[0].get('target_generation',0) if previous else 0
                    if previous and previous[0]['target'] != event['target']:
                        generation += 1
                    event['target_generation'] = generation
                    self.native_observations[packet.crypto_source] = (event,received_ms)
            # Permanent scene ownership tracking is separate from the scoped
            # observer list used and cleaned up by commissioning workflows.
            self.scene_engine.observe(packet, received_ms)
            for observer in tuple(self.packet_observers):
                observer(packet, received_ms)
        if packet.crypto_source == 0x8000 and not duplicate_reply:
            p = packet.payload
            if packet.opcode == 115 and len(p) >= 5 and p[:2] == b"\x01\x0a":
                self.core.controllers.observe_query(packet.mcp_address, p[4], received_ms)
        for predicate, future, since in list(self.waiters):
            if not duplicate_reply and not future.done() and since <= received_ms <= now and now - received_ms <= 1000 and predicate(packet):
                future.set_result(packet)
        if packet.crypto_source in self.quarantined():
            return
        device = self.core.devices.get(packet.crypto_source)
        if duplicate_reply and device and device.product_id in LIGHTS:
            # The core may never have seen this report while MQTT was offline.
            # A repeated envelope cannot refresh or replace a newer observation.
            return
        if event and not duplicate_reply:
            self._refresh_native_command(packet, event, received_ms)
        light_observed = False
        if not duplicate_reply and device and device.product_id in LIGHTS:
            update = decode_light(packet)
            if update and not ("color_temp" in update and device.product_id not in COLOR_LIGHTS):
                self.local_light_states.setdefault(device.device_id, {}).update(
                    {field: (value, received_ms) for field, value in update.items()})
                if {"state", "brightness"} & update.keys():
                    self._initial_light_deadlines.pop(device.device_id, None)
                    self._unanswered_power_reads.pop(device.device_id, None)
                self.command_timings.observe(device.device_id, update, received_ms)
                light_observed = True
        local_source = (device is not None and device.product_id == 91
                        and device.device_id in self.core.controllers.local_modes)
        had_baseline = packet.crypto_source in self.core.controllers.baselines
        def accepted_controller(event, when):
            self.controller_events[event['source']] = (event, when)
        messages = self.core.receive(frame, received_ms, now,
            controller_since=self.controller_since, controller_observer=accepted_controller)
        if device and device.product_id in LIGHTS and any(m['topic'].endswith('/state') for m in messages):
            # Local observations continued during a broker outage. Normal live
            # reports must use that same newest-field view as reconnect snapshots.
            messages = self._light_publications(target=device.device_id)
        if (local_source and device.exposed and self.core.ready_since is not None
                and received_ms >= self.core.ready_since and not had_baseline
                and packet.crypto_source in self.core.controllers.baselines):
            self.emit([self.core.intent(
                f'{self.core.prefix}/controller/{packet.crypto_source}/input/availability', 'online', True)])
        self.emit(messages)
        for message in messages:
            payload = message["payload"]
            if isinstance(payload, dict) and "event_type" in payload:
                product = self.core.devices[payload['source']].product_id
                for name, kind in named_inputs(payload, product):
                    named = dict(payload, event_type=kind, action=name)
                    if name == 'brightness':
                        named['brightness_pct'] = (payload['brightness'] * 100 + 127) // 255
                    self.emit([self.core.intent(f"{self.core.prefix}/controller/{payload['source']}/input/{name}", named)])
        if self.mqtt_ready and (light_observed or any(m["topic"].endswith("/state") for m in messages)):
            self.publish_groups()

    def _queue_light_refresh(self, target, level, members, *, native=False, fields=("brightness",), settle_ms=GROUP_COMMAND_SETTLE_MS, expected=None):
        """Coalesce read-only work; a controller hold cannot postpone its deadline."""
        key = (target, level, tuple(members), native, tuple(sorted(fields)))
        if native or key != self._group_refresh_key or not any(m in self._group_refresh for m in members):
            previous = self._group_refresh_key
            continuing_native_target = (native and previous is not None
                and previous[3] and previous[0] == target
                and previous[2] == tuple(members) and bool(self._group_refresh))
            self._group_refresh_revision += 1
            self._group_refresh = {did: revision for did, revision in self._group_refresh.items()
                                   if did in self.core.devices and self.core.devices[did].product_id in LIGHTS}
            self._group_refresh.update((m, self._group_refresh_revision) for m in members)
            self._feedback_fields = {did: pending for did, pending in self._feedback_fields.items()
                                     if did in self._group_refresh}
            for member in members:
                self._feedback_fields.setdefault(member, set()).update(fields)
                if 'brightness' in fields:
                    self._feedback_checks[member] = {'revision': self._group_refresh_revision,
                        'generation': self.generation, 'expected': (expected or {}).get('brightness'),
                        'since': None, 'due': None}
                elif member in self._feedback_checks and self._feedback_checks[member]['since'] is None:
                    self._feedback_checks[member]['revision'] = self._group_refresh_revision
            due = self.clock() + settle_ms
            if not settle_ms and self._group_refresh_due > self.clock():
                due = self._group_refresh_due
            self._group_refresh_due = min(self._group_refresh_due, due) if continuing_native_target else due
            self._observation_requested.set()
        self._group_refresh_key = key

    def _refresh_native_command(self, packet, event, received_ms):
        # Command evidence only schedules reads. It never becomes light state.
        if (self._observation_task is None or self.controller_since is None
                or received_ms < self.controller_since
                or not ('brightness' in event or 'delta' in event)):
            return
        previous = self._feedback_sequences.get(packet.crypto_source)
        if previous is not None and not 0 < ((packet.sequence - previous) & 0xffffff) < 0x800000:
            return
        self._feedback_sequences[packet.crypto_source] = packet.sequence
        pending = {j['target'] for j in self.store.journals(pending=True)}
        if packet.crypto_source in pending:
            return
        target = event.get('target')
        group = next((g for g in self.store.inventory()['groups'] if g['group_id'] == target), None)
        if group:
            if (packet.mcp_address != 0 or group['protected'] or not group['members']
                    or target == 24566 or target in self.quarantined_groups()):
                return
            members = list(group['members'])
        elif target == 0 and packet.mcp_address in self.core.devices:
            # The documented direct command uses the MCP destination, with zero
            # in the group field. Never reinterpret an unknown group as a light.
            target, members = packet.mcp_address, [packet.mcp_address]
        else:
            return
        if any(m in pending or m in self.quarantined() or m not in self.core.devices
               or self.core.devices[m].product_id not in LIGHTS for m in members):
            return
        timing = self.command_timings.start(target, members, {'brightness': event.get('brightness')},
            source='native', received_ms=received_ms,
            shared=group['exposed'] if group else self.core.devices[target].exposed)
        self.command_timings.dispatch(timing)
        self.command_timings.sent(timing)
        self._queue_light_refresh(target, (event.get('brightness'), event.get('delta')), members, native=True,
            expected={'brightness': event['brightness']} if 'brightness' in event else None)

    async def send(self, plaintext, *, generation=None, admission=None):
        if getattr(self, 'maintenance', False):
            raise RuntimeError('Bridge is restarting for restore')
        generation=self.generation if generation is None else generation
        if generation!=self.generation:raise RuntimeError("Mesh operation belongs to an earlier session")
        if not self.mesh_ready or not self.transport.ready:
            raise RuntimeError("Mesh is offline")
        sequence = self.store.allocate_sequence()  # committed before radio dispatch
        frame = encrypt_mcp(plaintext, self.key, sequence)
        begun, session, accepted = self.clock(), self.diagnostics.session, False
        dispatch_started = False
        owner=self.diagnostics.capture['id'] if self.diagnostics.active() else None
        try:
            # Admission may depend on a short device wake window. Recheck after
            # durable sequence allocation, immediately before radio dispatch.
            if admission is not None:
                admission()
            dispatch_started = True
            await self.transport.send(frame)
            accepted = True
        finally:
            self.diagnostics.transmitted(plaintext,sequence,begun,accepted,session=session,owner=owner,
                wire_length=len(frame),trailer=frame[-1],dispatch_started=dispatch_started)
        if generation!=self.generation:raise RuntimeError('Session changed during dispatch; outcome uncertain')

    async def exchange(self, plaintext, predicate, timeout=3, *, generation=None, admission=None):
        future = asyncio.get_running_loop().create_future()
        waiter = (predicate, future, self.clock())
        self.waiters.append(waiter)  # subscribe before transmitting
        try:
            generation=self.generation if generation is None else generation
            await self.send(plaintext,generation=generation,admission=admission)
            try:
                if (asyncio.current_task() is self._background_read_task
                        and plaintext[2:4] == b'\x73\x01'):
                    # Only background Get replies are expendable, and only AFTER
                    # send() completes. Never interrupt a radio send or a setter.
                    result = await self._wait_background_observation(future, timeout)
                else:
                    result = await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError as error:
                raise _ReplyTimeout() from error
            if generation!=self.generation:raise RuntimeError('Reply crossed a session boundary')
            return result
        finally:
            self.waiters.remove(waiter)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _wait_background_observation(self, future, timeout=None):
        """Yield a reply wait or pacing timer to a still-pending foreground request."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while not future.done():
            if self._foreground_requests:
                raise _BackgroundReadInterrupted()
            remaining = None if deadline is None else max(0, deadline - loop.time())
            priority = asyncio.create_task(self._foreground_pending.wait())
            try:
                done, _ = await asyncio.wait((future, priority), timeout=remaining,
                                            return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise asyncio.TimeoutError()
            finally:
                priority.cancel()
                await asyncio.gather(priority, return_exceptions=True)
            # A cancelled/timed-out foreground request may have released its
            # reservation already. Recheck the count instead of latching a signal.
            # Prefer a reply already received if both became ready together.
        return future.result()

    async def _light_read_gap(self, delay):
        if asyncio.current_task() is not self._background_read_task:
            await asyncio.sleep(delay)
            return
        pause = asyncio.create_task(asyncio.sleep(delay))
        try:
            await self._wait_background_observation(pause)
        except _BackgroundReadInterrupted:
            pass  # the caller checks foreground reservations before the next read
        finally:
            pause.cancel()
            await asyncio.gather(pause, return_exceptions=True)

    @property
    def controller_actions_busy(self):
        # Operator requests already preempt background reads at HTTP admission.
        # A waiting operator or a lock acquired by any other task still takes precedence.
        return bool(self._foreground_requests or (
            self.operation.locked() and self.operation.acquired_by is not self._background_read_task))

    @asynccontextmanager
    async def foreground_request(self, *, timeout=20):
        """Reserve priority for one original request, never replay its handler.

        Only an existing background read transmission is awaited. Its reply wait
        and pacing gap yield to this request. Other foreground operations
        retain their normal busy/pending guards. Waiting never cancels a radio
        write or abandons an in-flight fragmented send.
        """
        if timeout <= 0:
            raise ValueError('Foreground admission timeout must be positive')
        generation = self.generation
        if getattr(self, 'maintenance', False):
            raise OperationNotStarted('mesh_offline')
        self._foreground_requests += 1
        self._foreground_pending.set()
        try:
            if self._background_read_task is not None:
                self._foreground_waiters += 1
                try:
                    await asyncio.wait_for(self._background_read_idle.wait(), timeout)
                except asyncio.TimeoutError:
                    raise OperationNotStarted('background_read_busy') from None
                finally:
                    self._foreground_waiters -= 1
            if generation != self.generation:
                raise OperationNotStarted('session_changed')
            if getattr(self, 'maintenance', False):
                raise OperationNotStarted('mesh_offline')
            yield
        finally:
            self._foreground_requests -= 1
            if not self._foreground_requests:
                self._foreground_pending.clear()
            self._observation_requested.set()

    @asynccontextmanager
    async def _background_read(self):
        """Admit one background read only when foreground reservations are idle."""
        if self._foreground_requests:
            yield False
            return
        if self._background_read_task is not None:
            raise RuntimeError('A background observation is already active')
        self._background_read_task = asyncio.current_task()
        self._background_read_idle.clear()
        try:
            yield True
        finally:
            self._background_read_task = None
            self._background_read_idle.set()

    def _light_read_device(self, target):
        integer(target, 0x8080, 65407, "target")
        device = self.core.devices.get(target)
        if device is None or device.product_id not in LIGHTS:
            raise ValueError("Read requires a known load device")
        if target in self.quarantined() or any(
                journal['target'] == target for journal in self.store.journals(pending=True)):
            raise RuntimeError("Target has an unresolved operation")
        if self.operation.locked():
            raise RuntimeError("Another mesh operation is active")
        return device

    async def _request_light_fields(self, target, fields):
        """Send each read once; Runtime.observe owns asynchronous reply publication."""
        device = self._light_read_device(target)
        generation, requested = self.generation, set()
        async with self.operation:
            for field, noun in (("brightness", 10), ("color_temp", 29)):
                if field not in fields or field == 'color_temp' and device.product_id not in COLOR_LIGHTS:
                    continue
                if requested:
                    await self._light_read_gap(TARGETED_FEEDBACK_GAP)
                if self._foreground_requests:
                    break
                await self.send(model_command(target, 1, noun), generation=generation)
                requested.add(field)
        return requested

    def _missing_light_fields(self, device):
        readings = self._light_observations(device.device_id)
        missing = set()
        if 'brightness' not in readings and 'state' not in readings:
            missing.add('brightness')
        if device.product_id in COLOR_LIGHTS and 'color_temp' not in readings:
            missing.add('color_temp')
        return missing

    async def _startup_broadcast(self, generation):
        """Request startup readings through one relay. Only observe() records replies."""
        lights = [d for d in self.core.devices.values() if d.product_id in LIGHTS]
        if len(lights) < 2:
            return False
        progress = self.startup_broadcast_progress = {
            'state': 'starting', 'generation': generation, 'lights': len(lights),
            'power_sent': False, 'color_sent': False, 'power_known': 0, 'color_known': 0}
        started = self.clock()

        def available():
            return (generation == self.generation and self.mesh_ready and self.transport.ready
                    and not self.operation.locked() and not self._foreground_requests
                    and not self._group_refresh and not self._feedback_checks
                    and self.clock() >= self._group_refresh_due and not self.store.journals(pending=True))

        async def collect(seconds):
            # Group feedback wakes the worker early. Never hold either lock while listening.
            until = self.clock() + int(seconds * 1000)
            while available():
                remaining = until - self.clock()
                if remaining <= 0:
                    break
                self._observation_requested.clear()
                await self._wait_observation_refresh(remaining / 1000)

        try:
            for field, noun, sent in (('brightness', 10, 'power_sent'), ('color_temp', 29, 'color_sent')):
                if not available():
                    break
                if not any(field in self._missing_light_fields(d) for d in lights):
                    continue
                if progress['power_sent']:
                    await collect(1.0)
                if not available():
                    break
                command = bytearray(model_command(0, 1, noun))
                if noun == 10:
                    command[7] = 0xd0  # ESP broadcast tag: dimmer replies are never button presses.
                async with self._background_read() as admitted:
                    if not admitted:
                        break
                    async with self.operation:
                        await self.send(bytes(command), generation=generation)
                progress[sent] = True
                progress['state'] = 'collecting'
            if (progress['power_sent'] or progress['color_sent']) and any(self._missing_light_fields(d) for d in lights):
                await collect(2.0)
        except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
            # An uncertain broadcast never counts as a miss or triggers another broadcast.
            progress['state'] = 'send_failed'
        if generation != self.generation or not self.mesh_ready or not self.transport.ready:
            progress['state'] = 'session_changed'
            return False
        sent_any = progress['power_sent'] or progress['color_sent']
        if progress['state'] != 'send_failed':
            progress['state'] = 'finished' if sent_any else 'skipped'
        progress['power_known'] = sum('brightness' not in self._missing_light_fields(d) for d in lights)
        progress['color_known'] = sum(d.product_id in COLOR_LIGHTS and 'color_temp' not in self._missing_light_fields(d) for d in lights)
        progress['elapsed_ms'] = self.clock() - started
        return sent_any

    async def read_light(self, target, *, field_gap=0, brightness_only=False, missing_only=False):
        """Explicit local read, independent of HA exposure; never sends a setter."""
        device = self._light_read_device(target)
        generation, observed = self.generation, {}
        fields = (("brightness", 10), ("color_temp", 29)) if device.product_id in COLOR_LIGHTS and not brightness_only else (("brightness", 10),)
        async with self.operation:
            for field, noun in fields:
                if self._foreground_requests and self._background_read_task is asyncio.current_task():
                    break
                if field != fields[0][0] and field_gap:
                    await self._light_read_gap(field_gap)
                    if self._foreground_requests and self._background_read_task is asyncio.current_task():
                        break
                if missing_only and field not in self._missing_light_fields(device):
                    continue
                try:
                    reply = await self.exchange(model_command(target, 1, noun),
                        lambda packet, n=noun: packet.crypto_source == target
                        and packet.opcode == 115 and packet.payload[:2] == bytes([1, n])
                        and decode_light(packet) is not None, generation=generation)
                    observed.update(decode_light(reply))
                except _BackgroundReadInterrupted:
                    break
                except _ReplyTimeout:
                    if field == 'brightness' and generation == self.generation and self.mesh_ready and self.transport.ready:
                        if not {'state', 'brightness'} & self._light_observations(target).keys():
                            misses = self._unanswered_power_reads.get(target, 0)
                            self._unanswered_power_reads[target] = min(misses + 1, UNANSWERED_POWER_READ_NOTICE)
                except asyncio.TimeoutError:
                    pass
        return {"target": target, "observed": observed,
                "complete": all(field in observed for field, _ in fields)}

    @asynccontextmanager
    async def _control_operation(self, timeout):
        if timeout is None:
            if self.operation.locked():
                raise RuntimeError('Another mesh operation is active')
            async with self.operation:
                yield
            return
        if timeout <= 0:
            raise OperationNotStarted('mesh_busy')
        acquisition = asyncio.create_task(self.operation.acquire())
        released = False
        def release_owned(task):
            nonlocal released
            if (not released and task.done() and not task.cancelled()
                    and task.exception() is None and task.result()):
                released = True
                self.operation.release()
        try:
            done, _ = await asyncio.wait({acquisition}, timeout=timeout)
            if not done:
                raise OperationNotStarted('mesh_busy')
            acquisition.result()
        except BaseException:
            # Only this acquisition can confer ownership. Its callback also
            # cleans up if cancellation interrupts the cleanup await itself.
            acquisition.add_done_callback(release_owned)
            if not acquisition.done():
                acquisition.cancel()
            release_owned(acquisition)
            await asyncio.gather(acquisition, return_exceptions=True)
            raise
        try:
            # The timeout ends at acquisition, never during a dispatched setter.
            yield
        finally:
            release_owned(acquisition)

    def check_light_features(self, target, values, selected=None):
        if not ('brightness' in values or 'color_temp' in values):
            return
        if selected is None:
            devices = self.core.devices
            if target in devices:
                selected = [devices[target]]
            elif target < 0x8000:
                group = next((g for g in self.store.inventory()['groups'] if g['group_id'] == target), None)
                selected = [devices[m] for m in group['members'] if m in devices] if group else []
            else:
                return
        if 'brightness' in values and any(d.product_id == 167 for d in selected):
            raise ValueError('Switch targets support only ON/OFF')
        if 'color_temp' in values and any(d.product_id not in COLOR_LIGHTS for d in selected):
            raise ValueError('Target does not support color temperature')

    async def control(self, target, values, retained=False, confirm=True, *, generation=None, local=False,
                      admission=None, admission_timeout=None, received_ms=None):
        # Only the authenticated management HTTP route opts into local control.
        # MQTT callers retain the exposure gate; no publication/polling is enabled.
        generation=self.generation if generation is None else generation
        if generation!=self.generation:raise RuntimeError("Command belongs to an earlier session")
        if retained:
            raise ValueError("Retained commands are ignored")
        integer(target, 1, 65407, "target")
        if not isinstance(values, dict) or not values or set(values) - {"state", "brightness", "color_temp"}:
            raise ValueError("Unsupported light command fields")
        if "state" in values and values["state"] not in ("ON", "OFF"):
            raise ValueError("Invalid light state")
        devices = self.core.devices
        inventory = self.store.inventory()
        group = next((g for g in inventory["groups"] if g["group_id"] == target), None)
        if group:
            if target in self.quarantined_groups():
                raise RuntimeError('Group has an unresolved physical membership operation')
            if group["protected"] or (not local and not group["exposed"]) or not group["members"]:
                raise ValueError("Group is protected, unexposed or empty")
            if any(i not in devices for i in group["members"]):
                raise ValueError("Control requires all group members to be known loads")
            selected = [devices[i] for i in group["members"]]
        else:
            selected = [devices[target]] if target in devices and (local or devices[target].exposed) else []
        if not selected or any(d.product_id not in LIGHTS for d in selected):
            raise ValueError("Control requires known load devices")
        self.check_light_features(target, values, selected)
        if any(d.device_id in self.quarantined() for d in selected):
            raise RuntimeError('Target is quarantined by an unresolved operation')
        commands = []
        if "color_temp" in values:
            commands.append(plan_light_command(target, kelvin=values["color_temp"]))
        brightness = values.get("brightness")
        if brightness is not None:
            integer(brightness, 0, 255, "brightness")
        if values.get("state") == "OFF":
            brightness = 0
        elif brightness is None and values.get("state") == "ON":
            brightness = 255
        if brightness is not None:
            commands.append(plan_light_command(target, brightness=brightness))
        if not commands:
            raise ValueError("Empty command")
        timing_expected = {}
        if brightness is not None:
            timing_expected['brightness'] = brightness
        if 'color_temp' in values:
            timing_expected['color_temp'] = values['color_temp']
        timing = self.command_timings.start(target, [d.device_id for d in selected], timing_expected,
            source='ui' if local else 'mqtt' if received_ms is not None else 'bridge',
            received_ms=received_ms, shared=group['exposed'] if group else selected[0].exposed)
        def timed_admission():
            if admission is not None:
                admission()
            self.command_timings.dispatch(timing)
        try:
            async with self._control_operation(admission_timeout):
                if generation != self.generation:
                    raise OperationNotStarted('session_changed')
                if inventory != self.store.inventory():
                    raise OperationNotStarted('inventory_changed')
                if any(d.device_id in self.quarantined() for d in selected):
                    raise RuntimeError('Target is quarantined by an unresolved operation')
                if group and target in self.quarantined_groups():
                    raise RuntimeError('Group has an unresolved physical membership operation')
                for index, command in enumerate(commands):
                    # Admission is checked after durable sequence allocation at the
                    # first setter. Once started, finish this compound request once.
                    await self.send(command,generation=generation,admission=timed_admission if index == 0 else None)
                self.command_timings.sent(timing)
                feedback_after_ms = self.clock()
                reports = []
                expected = {}
                if brightness is not None:
                    expected["brightness"] = brightness
                if "color_temp" in values:
                    expected["color_temp"] = values["color_temp"]
                defer_feedback = not confirm and self._observation_task is not None
                if defer_feedback:
                    settle_ms = GROUP_COMMAND_SETTLE_MS if group and brightness in (0, 255) else 0
                    self._queue_light_refresh(target, (brightness, values.get('color_temp')),
                        [d.device_id for d in selected], fields=tuple(expected), settle_ms=settle_ms, expected=expected)
                else:
                    self._group_refresh_key = None
                for d in (() if defer_feedback else selected):
                    observed = {"device_id": d.device_id}
                    for field, noun in (("brightness", 10), ("color_temp", 29)):
                        if field not in expected:
                            continue
                        if not confirm:
                            await self.send(model_command(d.device_id, 1, noun),generation=generation)
                            continue
                        try:
                            reply = await self.exchange(model_command(d.device_id, 1, noun),
                                lambda p, did=d.device_id, n=noun: p.crypto_source == did
                                and p.opcode == 115 and p.payload[:2] == bytes([1, n])
                                and decode_light(p) is not None,generation=generation)
                            observed.update(decode_light(reply))
                        except asyncio.TimeoutError:
                            pass
                    if len(observed) > 1:
                        reports.append(observed)
        except BaseException:
            self.command_timings.failed(timing)
            raise
        matched = len(reports) == len(selected) and all(
            all(report.get(field) == value for field, value in expected.items()) for report in reports)
        return {"sent": True, "readbacks": reports, "confirmed": confirm and matched,
                **({"feedback_pending": True, "feedback_after_ms": feedback_after_ms} if not confirm else {})}

    def group_mode(self, group):
        products = [self.core.devices[m].product_id for m in group['members'] if m in self.core.devices]
        if not products or len(products) != len(group['members']) or any(p not in LIGHTS for p in products):
            return None
        if all(p in COLOR_LIGHTS for p in products):
            return 'color_temp'
        return 'onoff' if 167 in products else 'brightness'

    def publish_groups(self):
        self.emit(self._group_publications())

    def _group_publications(self):
        now = self.clock()
        quarantined, quarantined_groups = self.quarantined(), self.quarantined_groups()
        mesh_online = self.mesh_ready and self.transport.ready
        messages = []
        for group in self.store.inventory()["groups"]:
            if group["protected"] or not group["exposed"]:
                continue
            observed, colors = [], []
            mode = self.group_mode(group)
            for member in group["members"]:
                if mode is None or not mesh_online:
                    continue
                if group['group_id'] in quarantined_groups:
                    continue
                if member in quarantined:
                    continue
                readings = self._light_observations(member)
                value = readings.get("brightness")
                if value and 0 <= now - value[1] < 120000:
                    observed.append(value[0])
                    colors.append(readings.get('color_temp'))
            topic = f"{self.core.prefix}/light/{group['group_id']}"
            messages.append(self.core.intent(topic + "/availability", "online" if observed else "offline", True))
            if observed:
                mode = self.group_mode(group)
                state = {"state": "ON" if any(observed) else "OFF", "observed_members": len(observed)}
                if mode != 'onoff':
                    state['brightness'] = max(observed)
                if mode == 'color_temp':
                    # HA's Kelvin JSON parser clears its last value on explicit
                    # null. Omitting the field leaves a stale color in the UI.
                    state.update(color_temp=None, color_mode='color_temp')
                    # A group color is known only when every member freshly agrees.
                    if len(observed) == len(group['members']) and all(v and 0 <= now-v[1] < 120000 for v in colors) and len({v[0] for v in colors}) == 1:
                        state.update(color_temp=colors[0][0], color_mode='color_temp')
                messages.append(self.core.intent(topic + "/state", state, True))
        return messages

    def discovery(self):
        messages = self.core.discovery()
        quarantined = self.quarantined()
        for message in messages:
            payload = message['payload']
            if isinstance(payload, dict) and 'unique_id' in payload:
                target = int(payload['unique_id'].rsplit('_', 1)[1])
                topic = f'{self.core.prefix}/device/{target}/availability'
                payload['availability'].append({'topic': topic})
        for device in self.core.devices.values():
            if not device.exposed:
                continue
            messages.append(self.core.intent(f'{self.core.prefix}/device/{device.device_id}/availability',
                'offline' if device.device_id in quarantined else 'online', True))
        for d in self.core.devices.values():
            if not d.exposed or d.product_id not in CONTROLLERS:
                continue
            names = ["on", "off", "raise", "lower"]
            # Frozen ESP profile: both native and local-report dimmers expose a
            # slider. Current ESP's earlier local-only Accessory gate is superseded.
            if d.product_id in (81, 91):
                names.append("brightness")
            if d.product_id in (126, 127):
                names += ["button_" + str(i) for i in range(1, 5)]
            for name in names:
                uid = f"{self.core.node}_{d.device_id}_{name}"
                topic = f"{self.core.prefix}/controller/{d.device_id}/input/{name}"
                event = "activated" if name.startswith("button_") else "command"
                label = name.replace("_", " ").title()
                ready = d.device_id not in quarantined
                payload = {"name": "Full brightness" if d.product_id == 91 and name == "on" else label,
                    "unique_id": uid,
                    "state_topic": topic, "event_types": [event],
                    "availability": [{"topic": self.core.prefix + "/availability"},
                                     {"topic": self.core.prefix + "/mesh/availability"},
                                     {"topic": f'{self.core.prefix}/device/{d.device_id}/availability'}],
                    "availability_mode": "all",
                    "device": {"identifiers": [f"{self.core.node}_{d.device_id}"], "name": d.name,
                               "manufacturer": "Avi-on / HALO Home", "model": CONTROLLERS[d.product_id]}}
                payload.update({"icon": "mdi:tune-vertical"} if name == "brightness" else {"device_class": "button"})
                if d.product_id == 91 and d.device_id in self.core.controllers.local_modes:
                    input_topic = f'{self.core.prefix}/controller/{d.device_id}/input/availability'
                    payload['availability'].append({'topic': input_topic})
                    messages.append(self.core.intent(input_topic,
                        'online' if ready and d.device_id in self.core.controllers.baselines else 'offline', True))
                if name.startswith("button_"):
                    payload["availability"].append({"topic": topic + "/availability"})
                    slot = int(name[-1])
                    status, row = binding_status(self.core.controllers.mapping_for_events(d.device_id), slot)
                    ready = ready and status in ('ready', 'cached')
                    messages.append(self.core.intent(topic + "/availability", "online" if ready else "offline", True))
                    payload['json_attributes_topic'] = topic + '/mapping'
                    mapping = {'mapping_status': 'quarantined' if d.device_id in quarantined else status,
                               'assignment_read_back': ready and status == 'ready',
                               'mapping_evidence': 'cached_table' if ready and status == 'cached' else
                                   'verified_current_session_table' if ready and status == 'ready' else 'unknown'}
                    if row is not None:
                        mapping.update(target_id=row.get('target'), native_brightness=row.get('level'))
                    messages.append(self.core.intent(topic + '/mapping', mapping, True))
                messages.append(self.core.intent(f"homeassistant/event/{uid}/config", payload, True))
                messages.append(self.core.intent(f"homeassistant/device_automation/{uid}/config",
                    {"automation_type": "trigger", "topic": topic, "type": event, "subtype": label,
                     "value_template": "{{ value_json.event_type }}", "payload": event,
                     "device": payload["device"]} if ready else "", True))
        for group in self.store.inventory()["groups"]:
            if group["protected"] or not group["exposed"]:
                continue
            uid = f"{self.core.node}_group_{group['group_id']}"
            topic = f"{self.core.prefix}/light/{group['group_id']}"
            mode = self.group_mode(group) or 'onoff'
            color = {"color_temp_kelvin": True, "min_kelvin": 2700, "max_kelvin": 5000} if mode == 'color_temp' else {}
            messages.append(self.core.intent(f"homeassistant/light/{uid}/config",
                {"name": group["name"], "unique_id": uid, "schema": "json", "brightness": mode != 'onoff', **color,
                 "device": {"identifiers": [uid], "name": group["name"],
                            "manufacturer": "HALO / Avi-on", "model": "Mesh light group"},
                 "command_topic": topic + "/set", "state_topic": topic + "/state",
                 "availability": [{"topic": self.core.prefix + "/availability"}, {"topic": topic + "/availability"}],
                 "availability_mode": "all", "supported_color_modes": [mode]}, True))
        current = {m["topic"] for m in messages}
        # Commit publication intent before any config can leave this process.
        # Its overapproximation survives crashes between metadata changes and reload.
        prior=set(self.store.get('discovery_ledger',[]))|set(self.store.get('discovery_tombstones',[]))
        configs={topic for topic in current if topic.startswith('homeassistant/')}
        ledger=prior|configs
        if len(ledger)>32768:raise RuntimeError('Discovery history capacity exhausted; never discard old identities')
        if ledger!=prior:self.store.set('discovery_ledger',sorted(ledger))
        retired=(prior-configs)|set(self.store.get('discovery_tombstones',[]))
        messages.extend(self.core.intent(topic, "", True) for topic in
                        sorted(retired) if topic not in current)
        # Availability/mapping metadata is retained too. Record it before sending
        # so hiding a controller cannot leave misleading slot metadata behind.
        auxiliary = current - configs
        old_auxiliary = set(self.store.get('discovery_aux_ledger', []))
        auxiliary_ledger = old_auxiliary | auxiliary
        if len(auxiliary_ledger) > 16384:
            raise RuntimeError('Discovery metadata history capacity exhausted')
        if auxiliary_ledger != old_auxiliary:
            self.store.set('discovery_aux_ledger', sorted(auxiliary_ledger))
        messages.extend(self.core.intent(topic, '', True) for topic in sorted(old_auxiliary - auxiliary))
        # Metadata reload restores hidden-member observations before emitting
        # discovery. Recompute their exposed groups without waiting for polling.
        if self.mqtt_ready:
            messages.extend(self._group_publications())
        messages.extend(scene_publications(self, discovery=True))
        return messages

    async def poll(self):
        """MQTT availability work; the observation scheduler owns radio reads."""
        while True:
            await asyncio.sleep(30)
            self.emit(self._light_publications(include_state=False))
            self.publish_groups()

    async def read_controller_baseline(self, target, *, generation=None):
        """Read an opted-in local model91 baseline without synthesizing a button."""
        device = self.core.devices.get(target)
        if (device is None or device.product_id != 91
                or target not in self.core.controllers.local_modes):
            raise ValueError('Baseline read requires enabled local model91 mode')
        if self.operation.locked() or any(j['target'] == target for j in self.store.journals(pending=True)):
            raise RuntimeError('Controller unavailable for baseline read')
        generation = self.generation if generation is None else generation
        if self._baseline_token >= 255:
            raise RuntimeError('Local baseline query token space exhausted for this session')
        self._baseline_token += 1
        token = self._baseline_token
        command = bytearray(model_command(target, 1, 10))
        command[7] = token  # nonzero replies are queries, never local button events
        async with self.operation:
            self.local_baseline_query = (target, token, generation, self.clock())
            try:
                return await self.exchange(bytes(command),
                    lambda packet: packet.crypto_source == target and packet.opcode == 115
                    and packet.mcp_address in (0x8000, target)
                    and len(packet.payload) == 4 and packet.payload[:3] == bytes([1, 10, token]),
                    generation=generation)
            finally:
                self.local_baseline_query = None

    async def _probe_local_controllers(self, generation, schedule):
        if self._foreground_requests or self._group_refresh or self._feedback_checks or self.clock() < self._group_refresh_due:
            return
        missing = {d.device_id for d in self.core.devices.values()
                   if d.product_id == 91 and d.device_id in self.core.controllers.local_modes
                   and d.device_id not in self.core.controllers.baselines}
        for target in set(schedule) - missing:
            del schedule[target]
        for target in sorted(missing):
            schedule.setdefault(target, (0, self.clock() + 5000))
        progress = self.observation_refresh_progress
        if progress is not None:
            progress['controller_probe_tokens'] = self._baseline_token
            progress['controller_probe_paused'] = 'token_exhausted' if self._baseline_token >= 255 and missing else None
        if self._baseline_token >= 255:
            return
        # Earliest-due first and one request per turn keep unavailable controllers
        # from monopolizing a pass or starving other controllers.
        for target in sorted(missing, key=lambda target: (schedule[target][1], target)):
            attempts, due = schedule[target]
            if (self.clock() < due or self.operation.locked()
                    or any(j['target'] == target for j in self.store.journals(pending=True))):
                continue
            if generation != self.generation or not (self.mesh_ready and self.transport.ready):
                return
            try:
                async with self._background_read() as admitted:
                    if not admitted:
                        return
                    await self.read_controller_baseline(target, generation=generation)
            except (RuntimeError, ValueError, asyncio.TimeoutError):
                pass
            attempts = min(3, attempts + 1)
            schedule[target] = (attempts, self.clock() + (5000 if attempts < 3 else 60000))
            return

    def _recheck_feedback(self):
        """One later read for missing/transitioning brightness, never a setter."""
        now = self.clock()
        for target, check in list(self._feedback_checks.items()):
            if check['generation'] != self.generation or target not in self.core.devices:
                del self._feedback_checks[target]
                continue
            if check['since'] is None:
                continue
            value = self._light_observations(target).get('brightness')
            if (value and value[1] >= check['since']
                    and (check['expected'] is None or value[0] == check['expected'])):
                del self._feedback_checks[target]
            elif now >= check['due'] and target not in self._group_refresh:
                # Consume the allowance before dispatch; failed/uncertain reads
                # cannot create an unbounded replay loop.
                del self._feedback_checks[target]
                self._group_refresh[target] = check['revision']
                self._feedback_fields[target] = {'brightness'}
                self._observation_requested.set()

    def _observation_wait_delay(self, tick):
        remaining = (self._group_refresh_due - self.clock()) / 1000
        deadlines = [remaining] if remaining > 0 else []
        deadlines.extend((c['due'] - self.clock()) / 1000 for c in self._feedback_checks.values()
                         if c['due'] is not None and c['due'] > self.clock())
        return min(tick, *deadlines) if deadlines else tick

    async def _wait_observation_refresh(self, delay):
        try:
            await asyncio.wait_for(self._observation_requested.wait(), delay)
        except asyncio.TimeoutError:
            pass
        finally:
            self._observation_requested.clear()

    async def observation_refresh(self, *, cadence=30, settle=5, gap=0.5, tick=1, broadcast_startup=False):
        """One MQTT-independent, paced read-only worker for the current session.

        Each light has its own lock scope and bounded read timeouts. Interactive
        work can run between lights; pending targets are never polled. A session
        boundary abandons the old pass and settles before starting fresh.
        """
        if cadence <= 0 or tick <= 0 or settle < 0 or gap < 0:
            raise ValueError('Invalid observation refresh timing')
        if self._observation_task is not None:
            raise RuntimeError('Observation refresh is already running')
        self._observation_task = asyncio.current_task()
        progress = self.observation_refresh_progress = {
            'state': 'waiting_mesh', 'passes': 0, 'generation': self.generation,
            'lights': 0, 'answered': 0, 'skipped': 0, 'queries_sent': 0,
            'controller_probe_tokens': 0, 'controller_probe_paused': None,
            'last_completed_ms': None, 'last_completed_generation': None}
        generation, next_pass, probes = None, 0, {}
        next_periodic_target = 0
        first_sweep_generation = None
        try:
            while True:
                if generation != self.generation:
                    generation, probes = self.generation, {}
                    next_periodic_target = 0
                    next_pass = self.clock() + int(settle * 1000)
                    progress.update(state='settling', generation=generation)
                self._recheck_feedback()
                ready = self.mesh_ready and self.transport.ready
                if not ready or self.operation.locked() or self._foreground_requests:
                    progress['state'] = ('waiting_mesh' if not ready else
                        'waiting_foreground' if self._foreground_requests else 'waiting_operation')
                elif self.clock() < self._group_refresh_due:
                    progress['state'] = 'settling_command'
                else:
                    if self.clock() < next_pass:
                        progress['state'] = ('idle' if progress['last_completed_generation'] == generation
                                             else 'settling')
                    try:
                        await self._probe_local_controllers(generation, probes)
                        periodic = self.clock() >= next_pass and not self._group_refresh and not self._feedback_checks
                        if periodic and broadcast_startup and generation == self.generation:
                            broadcast_startup = False  # Once per process startup, never per inventory edit.
                            if await self._startup_broadcast(generation):
                                first_sweep_generation = generation
                            continue  # Recheck foreground work, feedback and session before follow-up.
                        if (periodic or self._group_refresh) and generation == self.generation:
                            refresh = dict(self._group_refresh)
                            targets = sorted(d.device_id for d in self.core.devices.values()
                                             if d.product_id in LIGHTS and d.device_id >= next_periodic_target) if periodic else sorted(refresh)
                            progress.update(state='running', lights=len(targets), answered=0, skipped=0, queries_sent=0)
                            preempted = False
                            for target in targets:
                                while ((self._foreground_requests or self.clock() < self._group_refresh_due)
                                        and generation == self.generation
                                        and self.mesh_ready and self.transport.ready):
                                    progress['state'] = ('waiting_foreground' if self._foreground_requests
                                                         else 'settling_command')
                                    await self._wait_observation_refresh(self._observation_wait_delay(tick))
                                if generation != self.generation or not (self.mesh_ready and self.transport.ready):
                                    break
                                if periodic and self._group_refresh:
                                    # Resume here after group feedback, even on a long startup sweep.
                                    next_periodic_target = target
                                    preempted = True
                                    break
                                progress['state'] = 'running'
                                requested = set()
                                fields = set(self._feedback_fields.get(target, {'brightness'}))
                                try:
                                    async with self._background_read() as admitted:
                                        if not admitted:
                                            continue
                                        if periodic:
                                            if first_sweep_generation == generation:
                                                if not self._missing_light_fields(self.core.devices[target]):
                                                    progress['answered'] += 1
                                                    continue
                                                result = await self.read_light(target, field_gap=0.15, missing_only=True)
                                            else:
                                                result = await self.read_light(target, field_gap=0.15)
                                            progress['answered' if result['observed'] else 'skipped'] += 1
                                        else:
                                            begun = self.clock()
                                            requested = await self._request_light_fields(target, fields)
                                            check = self._feedback_checks.get(target)
                                            if ('brightness' in requested and check is not None
                                                    and check['revision'] == refresh.get(target)):
                                                check.update(since=begun, due=self.clock() + FEEDBACK_RECHECK_MS)
                                            progress['queries_sent'] += len(requested)
                                except (RuntimeError, ValueError, asyncio.TimeoutError):
                                    progress['skipped'] += 1
                                    requested = fields  # failed convenience reads are never replayed
                                    check = self._feedback_checks.get(target)
                                    if check and check['revision'] == refresh.get(target):
                                        del self._feedback_checks[target]
                                # A command arriving during this read owns a newer
                                # revision and must retain its subsequent refresh.
                                if target in refresh and self._group_refresh.get(target) == refresh[target]:
                                    self._feedback_fields.get(target, set()).difference_update(requested)
                                    if not self._feedback_fields.get(target):
                                        self._feedback_fields.pop(target, None)
                                        del self._group_refresh[target]
                                await asyncio.sleep(gap if periodic else TARGETED_FEEDBACK_GAP)
                                await self._probe_local_controllers(generation, probes)
                            if generation == self.generation and self.mesh_ready and self.transport.ready:
                                progress.update(state='idle', passes=progress['passes'] + 1,
                                    last_completed_ms=self.clock(), last_completed_generation=generation)
                                if periodic and not preempted:
                                    first_sweep_generation = None
                                    next_periodic_target = 0
                                    next_pass = self.clock() + int(cadence * 1000)
                            if preempted:
                                continue
                        if (generation == self.generation and not self._group_refresh and not self._feedback_checks
                                and self.clock() >= self._group_refresh_due
                                and self.keypad_binding_refresher is not None):
                            await self.keypad_binding_refresher.tick()
                    except Exception:
                        # A failed convenience read must not bring down the bridge
                        # or hot-loop. Keep the next retry bounded by the cadence.
                        progress['state'] = 'failed'
                        next_pass = self.clock() + int(cadence * 1000)
                        if self._group_refresh:
                            self._group_refresh_due = max(self._group_refresh_due, next_pass)
                self._recheck_feedback()
                await self._wait_observation_refresh(self._observation_wait_delay(tick))
        finally:
            progress['state'] = 'stopped'
            self._observation_task = None

    async def startup_read(self, *, settle=5.0, gap=0.5, mesh_wait=600, busy_wait=60, broadcast_startup=False):
        """Read every known light once after this process first reaches the mesh.

        States live only in memory, so a restart forgets them and lights do not report
        unprompted. This sends the same read-only queries as an explicit read (never a
        setter), one light at a time. A light with an unresolved operation, or one that
        does not answer, is skipped and never retried. Other work always goes first.
        """
        progress = self.startup_read_progress = {'state': 'waiting', 'lights': 0, 'answered': 0, 'skipped': 0}
        try:
            return await self._startup_read(progress, settle, gap, mesh_wait, busy_wait, broadcast_startup)
        except Exception:
            # A convenience read must never stop the service it runs beside.
            progress['state'] = 'failed'
            return progress

    async def _startup_read(self, progress, settle, gap, mesh_wait, busy_wait, broadcast_startup):
        waited = 0.0
        while not (self.mesh_ready and getattr(self.transport, 'ready', False)):
            if waited >= mesh_wait:
                progress['state'] = 'mesh_unavailable'
                return progress
            await asyncio.sleep(1)
            waited += 1
        await asyncio.sleep(settle)  # let the new connection settle before radio work
        generation = self.generation
        missing_only = broadcast_startup and await self._startup_broadcast(generation)
        targets = sorted(d.device_id for d in self.core.devices.values() if d.product_id in LIGHTS)
        progress.update(state='running', lights=len(targets))
        for target in targets:
            waited = 0.0
            while self.operation.locked() or not self.mesh_ready or self._foreground_requests:
                if waited >= busy_wait:
                    break
                await asyncio.sleep(1)
                waited += 1
            try:
                if (self.operation.locked() or not self.mesh_ready or self._foreground_requests
                        or target not in self.core.devices):
                    raise RuntimeError('Light unavailable for startup read')
                async with self._background_read() as admitted:
                    if not admitted:
                        raise RuntimeError('Foreground request takes priority')
                    if missing_only and generation == self.generation:
                        if not self._missing_light_fields(self.core.devices[target]):
                            progress['answered'] += 1
                            continue
                        reply = await self.read_light(target, missing_only=True)
                    else:
                        reply = await self.read_light(target)
                progress['answered' if reply['observed'] else 'skipped'] += 1
            except (ValueError, RuntimeError, asyncio.TimeoutError):
                progress['skipped'] += 1
            await asyncio.sleep(gap)
        progress['state'] = 'done'
        return progress

    async def bluetooth_loop(self):
        delay = 1
        self._bluetooth_startup_failures = 0
        while True:
            try:
                await self.transport.connect()
                self.relay_identity.start(getattr(self.transport, 'address', None))
                self.mesh_ready = True
                self._bluetooth_startup_failures = None
                self.last_error = None
                self.session()
                self.emit(self.discovery())
                delay = 1
                while self.transport.ready:
                    try:
                        frame, when = await asyncio.wait_for(self.transport.queue.get(), 0.5)
                    except asyncio.TimeoutError:
                        continue
                    self.observe(frame, when)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = "bluetooth_connection_failed"
                if self._bluetooth_startup_failures is not None:
                    self._bluetooth_startup_failures = min(self._bluetooth_startup_failures + 1, BLUETOOTH_STARTUP_FAILURE_NOTICE)
            finally:
                self.mesh_ready = False
                self.relay_identity.stop()
                await self.transport.disconnect()
                for _, future, _ in self.waiters:
                    if not future.done():
                        future.set_exception(RuntimeError("Mesh disconnected"))
                self.session()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
