"""Pinned ESP-compatible named discovery; no physical button qualification."""
import tempfile
import unittest
from unittest.mock import patch
from avionmesh_pi.controllers import ControllerRouter, binding_status, named_inputs
from avionmesh_pi.protocol import Packet, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store
from test_service import KEY, FakeTransport


class DiscoveryContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = synthetic_store(self.temp.name, KEY)
        self.addCleanup(self.store.close)
        self.store.import_inventory({'devices': [
            {'device_id': 40000 + p, 'product_id': p, 'name': str(p), 'exposed': True}
            for p in (81, 91, 126, 127)], 'groups': []})
        self.runtime = Runtime(self.store, KEY, FakeTransport(), clock=lambda: 10)

    def configs(self):
        return {m['topic']: m['payload'] for m in self.runtime.discovery()}

    def test_all_model_named_event_and_trigger_contracts(self):
        configs = self.configs()
        for product in (81, 91, 126, 127):
            for action in ('on', 'off', 'raise', 'lower'):
                uid = f'pi_lab_{40000 + product}_{action}'
                event = configs['homeassistant/event/' + uid + '/config']
                trigger = configs['homeassistant/device_automation/' + uid + '/config']
                self.assertEqual(event['event_types'], ['command'])
                self.assertEqual(trigger['type'], 'command')
                self.assertEqual(trigger['subtype'], action.title())
                self.assertEqual(trigger['payload'], 'command')
                self.assertEqual(trigger['topic'], event['state_topic'])

    def test_frozen_brightness_profile_and_stable_accessory_trigger(self):
        for local_mode in (False, True):
            if local_mode:
                self.runtime.core.controllers.local_modes.add(40091)
            configs = self.configs()
            for product in (81, 91):
                uid = f'pi_lab_{40000+product}_brightness'
                self.assertEqual(configs[f'homeassistant/event/{uid}/config']['event_types'], ['command'])
                self.assertEqual(configs[f'homeassistant/device_automation/{uid}/config']['subtype'], 'Brightness')
            for product in (126, 127):
                self.assertNotIn(f'homeassistant/event/pi_lab_{40000+product}_brightness/config', configs)
            self.assertEqual(configs['homeassistant/event/pi_lab_40091_on/config']['name'], 'Full brightness')
            self.assertEqual(configs['homeassistant/device_automation/pi_lab_40091_on/config']['subtype'], 'On')

    def test_unverified_and_quarantined_button_triggers_are_tombstones(self):
        topic = 'homeassistant/device_automation/pi_lab_40127_button_1/config'
        self.assertEqual(self.configs()[topic], '')
        self.runtime.core.controllers.bindings[40127] = {'slots': [{'slot': 1, 'target': 300, 'flags': 8, 'level': 255}]}
        self.assertEqual(self.configs()[topic]['payload'], 'activated')
        with patch.object(self.runtime, 'quarantined', return_value={40127}):
            self.assertEqual(self.configs()[topic], '')
        self.runtime.core.controllers.bindings.clear()
        self.assertEqual(self.configs()[topic], '')

    def test_named_payload_contract_keeps_aggregate_semantics(self):
        event = {'event_type': 'brightness', 'direction': 'raise', 'brightness': 100}
        self.assertEqual(named_inputs(event), [('brightness', 'command'), ('raise', 'command')])
        self.assertEqual(event['event_type'], 'brightness')
        self.assertEqual(named_inputs({'event_type': 'on', 'activation': True, 'button': 2}),
                         [('button_2', 'activated'), ('on', 'command')])

    def test_mapping_status_and_activation_share_the_same_gate(self):
        topic = 'homeassistant/event/pi_lab_40127_button_1/config'
        trigger = 'homeassistant/device_automation/pi_lab_40127_button_1/config'
        cases = [
            ({'slot': 1, 'target': 65535, 'flags': 0, 'level': 0}, 'unassigned'),
            ({'slot': 1, 'target': 0, 'flags': 8, 'level': 255}, 'unsupported_binding'),
            ({'slot': 1, 'target': 300, 'flags': 9, 'level': 255}, 'unsupported_binding'),
            ({'slot': 1, 'target': 300, 'flags': 8, 'level': 0}, 'unsupported_binding'),
            ({'slot': 1, 'target': 300, 'flags': 8, 'level': 255}, 'ready')]
        for row, status in cases:
            with self.subTest(status=status, row=row):
                self.runtime.core.controllers.bindings[40127] = {'slots': [row]}
                configs = self.configs()
                mapping_topic = configs[topic]['json_attributes_topic']
                self.assertEqual(configs[mapping_topic]['mapping_status'], status)
                self.assertEqual(isinstance(configs[trigger], dict), status == 'ready')
        rows = [dict(cases[-1][0]), dict(cases[-1][0], slot=2)]
        self.runtime.core.controllers.bindings[40127] = {'slots': rows}
        self.assertEqual(binding_status({'slots': rows}, 1)[0], 'ambiguous_target')
        self.assertEqual(self.configs()[trigger], '')
        event = self.runtime.core.controllers.decode(Packet(1, 40127, 0, 115,
            bytes.fromhex('000a012c00ff00000000')), 127, 10)
        self.assertNotIn('button', event)

    def test_hidden_controller_clears_mapping_and_retains_tombstones_after_restart(self):
        configs = self.configs()
        metadata = {topic for topic in configs if '/controller/40127/' in topic}
        self.assertTrue(any(topic.endswith('/mapping') for topic in metadata))
        from dataclasses import replace
        self.runtime.core.devices[40127] = replace(self.runtime.core.devices[40127], exposed=False)
        hidden = self.configs()
        for topic in metadata:
            self.assertEqual(hidden[topic], '')
        replacement = Runtime(self.store, KEY, FakeTransport(), clock=lambda: 10)
        replacement.core.devices[40127] = self.runtime.core.devices[40127]
        for message in replacement.discovery():
            if message['topic'] in metadata:
                self.assertEqual(message['payload'], '')

    def test_named_runtime_payload_and_endpoint_brightness_are_not_retained(self):
        output = []
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.session()
        self.runtime.sink = output.append
        for source in (40081, 40091, 40127):
            self.runtime.observe(encrypt_mcp(bytes.fromhex('008073000a012300ff00000000'), KEY, 1, source), 10)
        for source in (40081, 40091):
            topic = f'avionmesh_pi/pi_lab/controller/{source}/input/brightness'
            message = next(m for m in output if m['topic'] == topic)
            self.assertFalse(message['retain'])
            self.assertEqual(message['payload']['event_type'], 'command')
            self.assertEqual(message['payload']['action'], 'brightness')
            self.assertEqual(message['payload']['brightness_pct'], 100)
        self.assertFalse(any('/40127/input/brightness' in m['topic'] for m in output))
        aggregate = next(m for m in output if m['topic'].endswith('/event/40091/event'))
        self.assertEqual(aggregate['payload']['event_type'], 'on')
        self.assertNotIn('action', aggregate['payload'])

    def test_local_input_availability_waits_for_baseline_without_firing(self):
        self.runtime.core.controllers.local_modes.add(40091)
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.session()
        topic = 'avionmesh_pi/pi_lab/controller/40091/input/availability'
        self.assertEqual(self.configs()[topic], 'offline')
        output = []
        self.runtime.sink = output.append
        self.runtime.observe(encrypt_mcp(bytes.fromhex('008073010a0080'), KEY, 1, 40091), 10)
        self.assertEqual(output, [self.runtime.core.intent(topic, 'online', True)])
        self.assertEqual(self.configs()[topic], 'online')
        self.runtime.session()
        self.assertEqual(self.configs()[topic], 'offline')

    def test_native_direction_does_not_cross_targets_idle_or_full_brightness(self):
        for product in (81, 91):
            for target, elapsed, level, sequence in ((301, 100, 150, 2), (300, 1501, 150, 2),
                                                     (300, 100, 255, 2), (300, 10001, 150, 1)):
                with self.subTest(product=product, target=target, elapsed=elapsed, level=level):
                    router = ControllerRouter()
                    def packet(seq, group, value):
                        return Packet(seq, 40081, 0, 115, bytes([0, 10]) + group.to_bytes(2, 'big') + bytes([0, value, 0, 0, 0, 0]))
                    router.decode(packet(1, 300, 100), product, 0)
                    event = router.decode(packet(sequence, target, level), product, elapsed)
                    self.assertIsNotNone(event)
                    self.assertNotIn('direction', event)

    def test_native_and_local_report_baselines_are_independent(self):
        router = ControllerRouter()
        router.local_modes.add(40091)
        router.decode(Packet(1, 40091, 0, 115, bytes.fromhex('000a012300ff00000000')), 91, 10)
        self.assertNotIn(40091, router.baselines)
        self.assertIsNone(router.decode(Packet(2, 40091, 0, 115, bytes.fromhex('010a0080')), 91, 20))
        self.assertIn(40091, router.baselines)
        router.reset()
        self.assertEqual((router.baselines, router.native_baselines, router.native_targets), ({}, {}, {}))

    def test_group_cct_unknown_explicitly_clears_previous_kelvin(self):
        from avionmesh_pi.bridge import Bridge, load_devices
        data = {'devices': [
            {'device_id': i, 'product_id': 134, 'name': 'Synthetic bulb', 'exposed': True}
            for i in (40002, 40003)], 'groups': [
            {'group_id': 291, 'name': 'Synthetic room', 'members': [40002, 40003],
             'exposed': True, 'protected': False}]}
        self.store.import_inventory(data)
        self.runtime.core = Bridge(load_devices(data['devices']), KEY)
        self.runtime.mesh_ready = True
        config = self.configs()['homeassistant/light/pi_lab_group_291/config']
        self.assertEqual(config['supported_color_modes'], ['color_temp'])
        self.assertTrue(config['color_temp_kelvin'])
        self.runtime.core.states = {i: {'brightness': (128, 10), 'color_temp': (3000, 10)}
                                    for i in (40002, 40003)}
        output = []
        self.runtime.sink = output.append
        def state():
            output.clear()
            self.runtime.publish_groups()
            return next(m['payload'] for m in output if m['topic'] == config['state_topic'])
        self.assertEqual(state()['color_temp'], 3000)
        for observation in ((4000, 10), (3000, -120000), (3000, 11), None):
            if observation is None:
                self.runtime.core.states[40003].pop('color_temp')
            else:
                self.runtime.core.states[40003]['color_temp'] = observation
            payload = state()
            self.assertEqual(payload['color_mode'], 'color_temp')
            self.assertIn('color_temp', payload)
            self.assertIsNone(payload['color_temp'])
        self.runtime.core.states[40003]['color_temp'] = (3000, 10)
        with patch.object(self.runtime, 'quarantined', return_value={40003}):
            self.assertIsNone(state()['color_temp'])
        self.assertEqual(state()['color_temp'], 3000)

    def test_controller_evidence_is_allowlisted_current_session_only(self):
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.session()
        frame = encrypt_mcp(bytes.fromhex('008073000a012300ff00000000'), KEY, 1, 40081)
        self.runtime.observe(frame, 10)
        event, when = self.runtime.controller_events[40081]
        self.assertEqual(when, 10)
        self.assertEqual(event, {'event_type': 'on', 'source': 40081, 'target': 291, 'brightness': 255})
        self.assertNotIn('payload', event)
        self.runtime.session()
        self.assertEqual(self.runtime.controller_events, {})
        with patch.object(self.runtime, 'quarantined', return_value={40081}):
            self.runtime.observe(encrypt_mcp(bytes.fromhex('008073000a012300ff00000000'), KEY, 2, 40081), 10)
        self.assertEqual(self.runtime.controller_events, {})

    def test_local_observation_is_independent_of_mqtt_and_exposure(self):
        from dataclasses import replace
        for exposed, mqtt in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(exposed=exposed, mqtt=mqtt):
                runtime = Runtime(self.store, KEY, FakeTransport(), clock=lambda: 10)
                runtime.core.devices = {i: replace(d, exposed=exposed)
                                        for i, d in runtime.core.devices.items()}
                runtime.mesh_ready, runtime.mqtt_ready = True, mqtt
                runtime.session()
                runtime.core.controllers.bindings[40127] = {'slots': [
                    {'slot': 1, 'target': 291, 'flags': 8, 'level': 162}]}
                output = []
                runtime.sink = output.append
                with patch.object(runtime.core.controllers, 'decode', wraps=runtime.core.controllers.decode) as decode:
                    for source in (40081, 40091, 40126, 40127):
                        frame = encrypt_mcp(bytes.fromhex('008073000a012300a200000000'), KEY, 1, source)
                        runtime.observe(frame, 10)
                        runtime.observe(frame, 10)
                    self.assertEqual(decode.call_count, 4)
                self.assertEqual(set(runtime.controller_events), {40081, 40091, 40126, 40127})
                self.assertEqual(runtime.controller_events[40127][0]['button'], 1)
                self.assertTrue(runtime.controller_events[40127][0]['activation'])
                self.assertNotIn('button', runtime.controller_events[40126][0])
                self.assertTrue(all('payload' not in e for e, _ in runtime.controller_events.values()))
                if exposed and mqtt:
                    aggregates = [m for m in output if '/event/' in m['topic']]
                    self.assertEqual(len(aggregates), 4)
                    self.assertEqual(sum('/button_1' in m['topic'] for m in output), 1)
                else:
                    self.assertEqual(output, [])
                self.assertEqual(runtime.transport.frames, [])

    def test_local_report_baseline_and_queries_work_without_mqtt_or_exposure(self):
        from dataclasses import replace
        for exposed, mqtt in ((False, False), (False, True), (True, False)):
            with self.subTest(exposed=exposed, mqtt=mqtt):
                runtime = Runtime(self.store, KEY, FakeTransport(), clock=lambda: 10)
                runtime.core.devices[40091] = replace(runtime.core.devices[40091], exposed=exposed)
                runtime.core.controllers.local_modes.add(40091)
                runtime.mesh_ready, runtime.mqtt_ready = True, mqtt
                runtime.session()
                output = []
                runtime.sink = output.append
                def report(sequence, level, token=0):
                    runtime.observe(encrypt_mcp(b'\x00\x80\x73\x01\x0a' + bytes([token, level]),
                                                KEY, sequence, 40091), 10)
                report(1, 80)
                self.assertIn(40091, runtime.core.controllers.baselines)
                self.assertEqual(runtime.controller_events, {})
                report(2, 100, token=1)
                self.assertEqual(runtime.controller_events, {})
                report(3, 110)
                event, when = runtime.controller_events[40091]
                self.assertEqual((event['input_mode'], event['direction'], when), ('local_level_reports', 'raise', 10))
                self.assertNotIn('target', event)
                self.assertEqual(output, [])
                self.assertEqual(runtime.transport.frames, [])

    def test_local_observation_rejects_stale_unknown_quarantined_and_previous_session(self):
        now = [1000]
        runtime = Runtime(self.store, KEY, FakeTransport(), clock=lambda: now[0])
        runtime.mesh_ready = True
        runtime.session()
        output = []
        runtime.sink = output.append
        def frame(sequence, source=40081):
            return encrypt_mcp(bytes.fromhex('008073000a012300a200000000'), KEY, sequence, source)
        runtime.observe(frame(1), 999)
        runtime.observe(frame(1), 1001)
        runtime.observe(frame(1, 49999), 1000)
        bad = bytearray(frame(1))
        bad[-2] ^= 1
        runtime.observe(bytes(bad), 1000)
        with patch.object(runtime, 'quarantined', return_value={40081}):
            runtime.observe(frame(2), 1000)
        self.assertEqual(runtime.controller_events, {})
        self.assertEqual(runtime.core.controllers.native_baselines, {})
        runtime.observe(frame(3), 1000)
        self.assertIn(40081, runtime.controller_events)
        now[0] = 1100
        runtime.session()
        output.clear()
        self.assertEqual(runtime.controller_events, {})
        runtime.observe(frame(4), 1099)
        self.assertEqual(runtime.controller_events, {})
        runtime.observe(frame(5), 1100)
        self.assertEqual(runtime.controller_events[40081][1], 1100)
        now[0] = 2101
        runtime.observe(frame(6), 1100)
        self.assertEqual(runtime.controller_events[40081][1], 1100)
        runtime.mesh_ready = False
        runtime.session()
        output.clear()
        runtime.observe(frame(7), 2101)
        self.assertIsNone(runtime.controller_since)
        self.assertEqual(runtime.controller_events, {})
        self.assertEqual(output, [])
        self.assertEqual(runtime.transport.frames, [])

    def test_bridge_default_call_remains_publication_only(self):
        from avionmesh_pi.bridge import Bridge, Device
        frame = encrypt_mcp(bytes.fromhex('008073000a012300a200000000'), KEY, 1, 40081)
        for exposed, connected in ((False, False), (False, True), (True, False)):
            with self.subTest(exposed=exposed, connected=connected):
                bridge = Bridge([Device(40081, 81, 'Synthetic', exposed)], KEY)
                bridge.connection(connected, 10)
                with patch.object(bridge.controllers, 'decode', wraps=bridge.controllers.decode) as decode:
                    self.assertEqual(bridge.receive(frame, 10, 10), [])
                    decode.assert_not_called()
                self.assertEqual(bridge.controllers.native_baselines, {})

    def test_bridge_observer_gets_copy_and_cannot_change_mqtt_payload(self):
        from avionmesh_pi.bridge import Bridge, Device
        bridge = Bridge([Device(40081, 81, 'Synthetic', True)], KEY)
        bridge.connection(True, 10)
        observed = []
        def observer(event, when):
            observed.append((dict(event), when))
            event['brightness'] = 1
        frame = encrypt_mcp(bytes.fromhex('008073000a012300a200000000'), KEY, 1, 40081)
        messages = bridge.receive(frame, 10, 10, controller_since=10, controller_observer=observer)
        self.assertEqual(len(observed), 1)
        self.assertNotIn('payload', observed[0][0])
        self.assertEqual(messages[0]['payload']['brightness'], 162)
