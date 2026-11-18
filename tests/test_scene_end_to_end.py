"""Scenes from HTTP through the real runtime, encrypted radio and MQTT intents.

The only fake is the transport peer. It decrypts actual outbound MCP frames and
responds with authenticated scene records through Runtime.observe. No engine,
send or exchange method is mocked, and no network peer is contacted.
"""
import asyncio
import json
import tempfile
import unittest

import test_http as http
from avionmesh_pi.mqtt import MqttService
from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.scene_engine import KEY as SCENES_KEY
from support import synthetic_store as Store
from test_service import KEY

EMPTY = b'\xff' * 4
INVENTORY = {'devices': [
    {'device_id': 40001, 'product_id': 134, 'name': 'Synthetic bulb', 'exposed': False},
    {'device_id': 40002, 'product_id': 93, 'name': 'Synthetic ceiling', 'exposed': False},
    {'device_id': 40003, 'product_id': 134, 'name': 'Synthetic other room', 'exposed': False}], 'groups': []}


class EncryptedScenePeer:
    ready = True
    dropped = 0

    def __init__(self):
        self.frames = []
        self.packets = []
        self.rows = {}
        self.reply_sequences = {}
        self.receiver = None
        self.stage_colour = False
        self.staged = {}
        self.fail_after_colour_once = False
        self.fail_after_brightness_once = False

    def reply(self, target, payload):
        sequence = self.reply_sequences.get(target, 0) + 1
        self.reply_sequences[target] = sequence
        encrypted = encrypt_mcp(b'\x00\x80\x73' + payload, KEY, sequence, source=target)
        self.receiver(encrypted, 10)

    async def send(self, frame):
        packet = decrypt_mcp(frame, KEY)
        self.frames.append(frame)
        self.packets.append(packet)
        payload, target = packet.payload, packet.mcp_address
        if packet.opcode != 115 or len(payload) != 10 or payload[1] != 34:
            raise AssertionError('Unexpected non-scene command')
        verb, token, selector = payload[0], payload[4], payload[5]
        if verb == 1:
            value = self.rows.get((target, selector), EMPTY)
            self.reply(target, bytes([1, 34, token, selector]) + value)
        elif verb == 2:
            if self.stage_colour and not selector & 32:
                self.staged[(target, selector)] = payload[6:]
            else:
                if self.stage_colour and selector & 32:
                    colour = self.staged.pop((target, selector & 31), None)
                    if colour is not None:
                        self.rows[(target, selector & 31)] = colour
                self.rows[(target, selector)] = payload[6:]
            # Exercise replies from actual lights. Codes alone never confirm a
            # write; the engine must issue independent READs of both properties.
            self.reply(target, bytes([2, 34, token, 1 if selector & 32 else 5]))
            if not selector & 32 and self.fail_after_colour_once:
                self.fail_after_colour_once = False
                raise RuntimeError('Synthetic disconnect after colour stage')
            if selector & 32 and self.fail_after_brightness_once:
                self.fail_after_brightness_once = False
                raise RuntimeError('Synthetic disconnect after preset commit')
        elif verb == 5:
            self.rows.pop((target, selector), None)
            self.rows.pop((target, selector | 32), None)
            self.staged.pop((target, selector), None)
            self.reply(target, bytes([5, 34, token, 1]))
        elif verb == 0:
            if target != 0:
                raise AssertionError('Expected one native broadcast recall')
        else:
            raise AssertionError('Unsupported synthetic scene verb')


@unittest.skipIf(http.create_app is None, 'HTTP dependencies tested in container')
class SceneEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, KEY)
        self.store.import_inventory(INVENTORY)
        self.radio = EncryptedScenePeer()
        self.runtime = Runtime(self.store, KEY, self.radio, clock=lambda: 10)
        self.radio.receiver = self.runtime.observe
        self.runtime.mesh_ready = self.runtime.mqtt_ready = True
        self.runtime.capabilities.update(native_scenes=True, inventory_complete=True)
        self.runtime.session()
        self.runtime.scene_engine.gap = 0
        self.runtime.scene_engine.pair_gap = 0
        self.messages = []
        self.runtime.sink = self.messages.append
        self.headers = {'Authorization': 'Bearer synthetic-scene-e2e-token-0123456789'}
        self.client = http.TestClient(http.TestServer(http.create_app(
            self.runtime, 'synthetic-scene-e2e-token-0123456789')))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self.temp.cleanup()

    async def operation(self, action, body, *, expected_state="complete"):
        response = await self.client.post('/api/scenes/' + action, headers=self.headers, json=body)
        self.assertEqual(response.status, 202, await response.text())
        for _ in range(300):
            response = await self.client.get('/api/scenes', headers=self.headers)
            state = await response.json()
            if state['job'] and state['job']['state'] != 'running':
                self.assertEqual(state['job']['state'], expected_state, state['job'])
                return state
            await asyncio.sleep(.001)
        self.fail('Scene job did not finish through actual runtime ingress')

    def scene_messages(self):
        return [m for m in self.messages if '/scene/' in m['topic'] or 'homeassistant/scene/' in m['topic']]

    async def test_http_recall_overtakes_silent_background_reply_without_extra_scene_reads(self):
        state = await self.operation('create', {'name': 'Priority', 'members': [
            {'target': 40001, 'brightness': 128, 'kelvin': 4000}], 'exposed': False})
        identifier = state['scenes'][0]['id']
        sent = asyncio.Event()
        original = self.radio.send
        async def send(frame):
            packet = decrypt_mcp(frame, KEY)
            if packet.payload[:2] == b'\x01\x0a':
                self.radio.frames.append(frame)
                self.radio.packets.append(packet)
                sent.set()  # read transmitted, but deliberately no reply
                return
            await original(frame)
        self.radio.send = send
        self.radio.frames.clear()
        self.radio.packets.clear()
        async def background():
            async with self.runtime._background_read():
                return await self.runtime.read_light(40001, brightness_only=True)
        worker = asyncio.create_task(background())
        try:
            await asyncio.wait_for(sent.wait(), 1)
            state = await asyncio.wait_for(self.operation('activate', {'id': identifier}), .5)
            result = state['job']['result']
            self.assertEqual((result['verification_reads'], result['recall_commands']), (0, 1))
            self.assertEqual(result['recall_profile'], 'halo_app')
            self.assertEqual([p.payload[:2] for p in self.radio.packets],
                             [b'\x01\x0a', b'\x00\x22'])
            self.assertEqual(self.radio.packets[-1].payload,
                             bytes.fromhex('00220001000101000000'))
            self.assertFalse((await worker)['complete'])
            self.assertEqual(self.runtime.waiters, [])
            self.assertFalse(self.runtime.operation.locked())
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_quiet_comparison_sends_normal_recall_then_resumes_observation_after_three_seconds(self):
        import time
        from avionmesh_pi.server import SCENE_QUIET_TEST_SECONDS
        self.assertEqual(SCENE_QUIET_TEST_SECONDS, 3)
        state = await self.operation('create', {'name': 'Quiet comparison', 'members': [
            {'target': 40001, 'brightness': 128, 'kelvin': 4000}], 'exposed': False})
        identifier = state['scenes'][0]['id']
        rows = dict(self.radio.rows)
        self.runtime.clock = lambda: int(time.monotonic() * 1000)
        self.radio.receiver = lambda frame, _: self.runtime.observe(frame, self.runtime.clock())
        self.radio.frames.clear(); self.radio.packets.clear()
        read_sent = asyncio.Event()
        original_send = self.radio.send
        async def send(frame):
            p = decrypt_mcp(frame, KEY)
            if p.payload[:2] in (b'\x01\x0a', b'\x01\x1d'):
                self.radio.frames.append(frame); self.radio.packets.append(p)
                read_sent.set()
                return
            await original_send(frame)
        self.radio.send = send
        response = await self.client.post('/api/scenes/activate-quiet-test',
            headers=self.headers, json={'id': identifier})
        self.assertEqual(response.status, 202)
        worker = asyncio.create_task(self.runtime.observation_refresh(settle=0, gap=0))
        try:
            await asyncio.sleep(.2)
            self.assertGreater(self.runtime._foreground_requests, 0)
            self.assertFalse(read_sent.is_set())
            self.assertEqual([p.payload for p in self.radio.packets],
                             [bytes.fromhex('00220001000101000000')])
            # Received state is not held while the bridge's own polls are quiet.
            self.radio.reply(40001, b'\x01\x0a\x00\x80')
            self.assertEqual(self.runtime.device_states()['40001']['brightness']['value'], 128)
            await asyncio.wait_for(read_sent.wait(), 4)
            view = await (await self.client.get('/api/scenes', headers=self.headers)).json()
            self.assertEqual(view['job']['state'], 'complete')
            result = view['job']['result']
            self.assertEqual(result['background_read_quiet_ms'], 3000)
            self.assertEqual((result['recall_profile'], result['recall_commands'], result['verification_reads']),
                             ('halo_app', 1, 0))
            self.assertEqual(self.runtime._foreground_requests, 0)
            self.assertEqual(self.radio.rows, rows)
            self.assertEqual(sum(p.payload[:2] == b'\x00\x22' for p in self.radio.packets), 1)
            self.assertFalse(any(p.payload[0] in (2, 5) for p in self.radio.packets))
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_full_http_runtime_radio_and_mqtt_scene_lifecycle(self):
        self.assertEqual(self.radio.frames, [])
        members = [{'target': 40001, 'brightness': 64, 'kelvin': 3000},
                   {'target': 40002, 'brightness': 191, 'kelvin': 4000}]
        state = await self.operation('create', {'name': 'Evening', 'members': members, 'exposed': False})
        self.assertFalse(state['operations'])
        scene = state['scenes'][0]
        identifier, reference = scene['id'], scene['reference']
        self.assertEqual(scene['status'], 'ready')
        self.assertEqual(self.store.get(SCENES_KEY)[0]['members'], members)
        self.assertEqual(self.radio.rows[(40001, reference)], bytes.fromhex('020bb800'))
        self.assertEqual(self.radio.rows[(40002, reference | 32)], bytes.fromhex('bf000000'))
        self.assertNotIn((40003, reference), self.radio.rows)
        self.assertEqual(len([p for p in self.radio.packets if p.payload[0] == 2]), 4)
        self.assertFalse(self.scene_messages(), 'Unexposed scene must not appear in HA')
        # Ownership preflight reads the nonmember too; core sequence routing must
        # not discard its future readbacks as unrelated controller traffic.
        self.assertTrue(any(p.mcp_address == 40003 and p.payload[0] == 1 for p in self.radio.packets))

        edited = [{'target': 40002, 'brightness': 128, 'kelvin': 3500}]
        state = await self.operation('edit', {'id': identifier, 'name': 'Reading', 'members': edited})
        self.assertEqual(state['scenes'][0]['members'], edited)
        self.assertNotIn((40001, reference), self.radio.rows)
        self.assertNotIn((40001, reference | 32), self.radio.rows)
        self.assertEqual(self.radio.rows[(40002, reference)], bytes.fromhex('020dac00'))
        self.assertEqual(self.store.get(SCENES_KEY)[0]['name'], 'Reading')

        before = len(self.radio.frames)
        state = await self.operation('activate', {'id': identifier})
        self.assertEqual(len(self.radio.frames), before + 1)
        packet = self.radio.packets[-1]
        self.assertEqual(packet.mcp_address, 0)
        self.assertEqual(packet.payload[0:2], b'\x00\x22')
        self.assertFalse(state['job']['result']['physical_state_confirmed'])
        timing = state['job']['result']['timing']
        self.assertFalse(timing['physical_response_measured'])
        for key in ('checks_ms', 'prepare_frame_ms', 'dispatch_checks_ms', 'submission_ms',
                    'handler_to_submission_ms', 'request_to_handler_ms', 'request_to_submission_ms'):
            self.assertGreaterEqual(timing[key], 0, key)
        self.assertEqual(self.runtime.device_states(), {}, 'Recall must not invent observed light state')

        before = len(self.radio.frames)
        response = await self.client.post('/api/scenes/exposure', headers=self.headers,
            json={'id': identifier, 'exposed': True})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.radio.frames), before)
        config = next(m for m in reversed(self.scene_messages()) if m['topic'].endswith('/config'))
        self.assertEqual(config['payload']['name'], 'Reading')
        self.assertNotIn('state_topic', config['payload'])
        self.assertTrue(self.store.get(SCENES_KEY)[0]['exposed'])
        command_topic = config['payload']['command_topic']
        self.assertTrue(any(m['topic'].endswith('/availability') and m['payload'] == 'online'
                            for m in self.scene_messages()))

        mqtt = MqttService(self.runtime, {'host': 'synthetic.invalid', 'tls': False})
        mqtt._connection(True)
        before = len(self.radio.frames)
        mqtt.ingest(command_topic, b'ON', False)
        task = asyncio.create_task(mqtt.command_loop())
        try:
            for _ in range(100):
                if len(self.radio.frames) > before:
                    break
                await asyncio.sleep(.001)
            self.assertEqual(len(self.radio.frames), before + 1)
            self.assertEqual(self.radio.packets[-1].payload[0:2], b'\x00\x22')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        state = await self.operation('remove', {'id': identifier})
        self.assertEqual(state['scenes'], [])
        self.assertEqual(state['operations'], [])
        self.assertEqual(self.store.get(SCENES_KEY), [])
        self.assertEqual(self.radio.rows, {})
        self.assertTrue(any(m['topic'] == config['topic'] and m['payload'] == '' for m in self.scene_messages()))
        self.assertTrue(any(m['topic'].endswith('/availability') and m['payload'] == 'offline'
                            for m in self.scene_messages()))
        sequences = [p.sequence for p in self.radio.packets]
        self.assertEqual(len(set(sequences)), len(sequences))
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(self.store.journals(pending=True), [])

    def mutations(self):
        return [p for p in self.radio.packets if p.payload[0] in (2, 5)]

    async def test_restart_http_and_mqtt_activation_skip_all_verification_reads(self):
        from avionmesh_pi.scene_engine import SceneEngine
        members = [{'target': 40001, 'brightness': 64, 'kelvin': 3000},
                   {'target': 40002, 'brightness': 191, 'kelvin': 4000},
                   {'target': 40003, 'brightness': 128, 'kelvin': 4000}]
        state = await self.operation('create', {'name': 'Triple', 'members': members, 'exposed': True})
        identifier = state['scenes'][0]['id']
        self.runtime.scene_engine = SceneEngine(self.runtime)
        self.runtime.session()
        before = len(self.radio.frames)
        state = await self.operation('activate', {'id': identifier})
        self.assertEqual(len(self.radio.frames), before + 1)
        self.assertEqual(state['job']['result']['verification_reads'], 0)
        self.assertEqual(state['job']['result']['recall_commands'], 1)
        mqtt = MqttService(self.runtime, {'host': 'synthetic.invalid', 'tls': False})
        mqtt._connection(True)
        before = len(self.radio.frames)
        mqtt.ingest(self.runtime.core.prefix + '/scene/' + identifier + '/set', b'ON', False)
        task = asyncio.create_task(mqtt.command_loop())
        try:
            for _ in range(100):
                if len(self.radio.frames) > before: break
                await asyncio.sleep(.001)
            self.assertEqual(len(self.radio.frames), before + 1)
            self.assertEqual(self.radio.packets[-1].payload[:2], b'\x00\x22')
            self.assertEqual(self.runtime.device_states(), {})
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)

    async def test_creating_another_scene_preserves_first_activation_proof(self):
        members = [{'target': 40001, 'brightness': 64, 'kelvin': 3000}]
        first = (await self.operation('create', {'name': 'First', 'members': members, 'exposed': False}))['scenes'][0]
        await self.operation('create', {'name': 'Second', 'members': members, 'exposed': False})
        before = len(self.radio.frames)
        await self.operation('activate', {'id': first['id']})
        self.assertEqual(len(self.radio.frames), before + 1)

    def assert_preset_pairs(self, packets):
        for index, packet in enumerate(packets):
            payload = packet.payload
            if payload[0] != 2 or payload[5] & 32:
                continue
            self.assertLess(index + 1, len(packets))
            following = packets[index + 1]
            self.assertEqual(following.mcp_address, packet.mcp_address)
            self.assertEqual(following.payload[:2], b'\x02\x22')
            self.assertEqual(following.payload[5], payload[5] | 32,
                             'No READ may interrupt a staged colour/brightness pair')

    async def test_staged_colour_peer_full_http_and_mqtt_lifecycle(self):
        self.radio.stage_colour = True
        await self.test_full_http_runtime_radio_and_mqtt_scene_lifecycle()
        self.assert_preset_pairs(self.radio.packets)
        self.assertEqual(self.radio.staged, {})

    async def test_colour_only_edit_still_commits_a_complete_preset_pair(self):
        self.radio.stage_colour = True
        members = [{'target': 40002, 'brightness': 100, 'kelvin': 3000}]
        state = await self.operation('create', {'name': 'Warm', 'members': members, 'exposed': False})
        scene = state['scenes'][0]
        before = len(self.radio.packets)
        state = await self.operation('edit', {'id': scene['id'], 'name': 'Neutral',
            'members': [dict(members[0], kelvin=4000)]})
        new_packets = self.radio.packets[before:]
        writes = [p for p in new_packets if p.payload[0] == 2]
        self.assertEqual(len(writes), 2)
        self.assert_preset_pairs(new_packets)
        self.assertEqual(self.radio.rows[(40002, scene['reference'])], bytes.fromhex('020fa000'))
        self.assertEqual(self.radio.rows[(40002, scene['reference'] | 32)], bytes.fromhex('64000000'))
        self.assertEqual(state['scenes'][0]['status'], 'ready')

    async def test_crash_after_colour_keeps_reference_and_resumes_only_untouched_brightness(self):
        self.radio.stage_colour = True
        self.radio.fail_after_colour_once = True
        members = [{'target': 40002, 'brightness': 100, 'kelvin': 4000}]
        state = await self.operation('create', {'name': 'Interrupted', 'members': members,
            'exposed': True}, expected_state='failed')
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(self.radio.rows, {}, 'Colour has been staged but not committed')
        self.assertTrue(self.radio.staged)
        self.assertTrue(state['operations'])
        transaction = state['operations'][0]['transaction']
        self.assertEqual(self.store.get(SCENES_KEY, []), [])
        # Rebuild the engine after a session boundary; the journal, not memory,
        # must retain the staged ambiguity and reserve the reference.
        from avionmesh_pi.scene_engine import SceneEngine
        self.runtime.session()
        self.runtime.scene_engine = SceneEngine(self.runtime)
        self.runtime.scene_engine.gap = 0
        self.runtime.scene_engine.pair_gap = 0
        state = await self.operation('recover', {'transaction': transaction})
        self.assertEqual(len(self.mutations()), 1, 'Read-only recovery cannot resend a setter')
        self.assertTrue(state['operations'])
        self.assertTrue(state['operations'][0]['progress']['recovery_ready'])
        self.assertFalse(any(isinstance(m['payload'], dict) and m['topic'].endswith('/config')
                             for m in self.scene_messages()), 'Partial creation must not appear in HA')
        state = await self.operation('resume', {'transaction': transaction})
        self.assertEqual(len(self.mutations()), 2)
        self.assertEqual(self.mutations()[-1].payload[5] & 32, 32)
        self.assertEqual(state['scenes'][0]['status'], 'ready')
        self.assertFalse(state['operations'])
        self.assertEqual(self.radio.staged, {})
        self.assertEqual(self.radio.rows[(40002, state['scenes'][0]['reference'])], bytes.fromhex('020fa000'))

    async def test_crash_after_pair_committed_recovers_without_any_setter_replay(self):
        self.radio.stage_colour = True
        self.radio.fail_after_brightness_once = True
        state = await self.operation('create', {'name': 'Committed before disconnect',
            'members': [{'target': 40002, 'brightness': 100, 'kelvin': 4000}],
            'exposed': False}, expected_state='failed')
        self.assertEqual(len(self.mutations()), 2)
        transaction = state['operations'][0]['transaction']
        from avionmesh_pi.scene_engine import SceneEngine
        self.runtime.session()
        self.runtime.scene_engine = SceneEngine(self.runtime)
        self.runtime.scene_engine.gap = 0
        self.runtime.scene_engine.pair_gap = 0
        state = await self.operation('recover', {'transaction': transaction})
        self.assertEqual(len(self.mutations()), 2)
        self.assertFalse(state['operations'])
        self.assertEqual(state['scenes'][0]['status'], 'ready')


    def legacy_receipt(self, identifier, *, brightness=100, status='verified'):
        import copy
        engine = self.runtime.scene_engine
        targets = engine._targets()
        desired = {'id': identifier, 'reference': 1, 'name': 'Legacy ' + identifier[:4],
                   'members': [{'target': 40002, 'brightness': brightness, 'kelvin': 4000}],
                   'exposed': False, 'owner': engine._owner()}
        baseline, final = engine._expected(None, targets), engine._expected(desired, targets)
        detail = {'schema': 1, 'scene_id': identifier, 'reference': 1, 'action': 'create',
                  'prior': None, 'desired': desired, 'targets': targets, 'inventory': engine._identity(),
                  'baseline': baseline, 'observed': copy.deepcopy(baseline), 'final': final,
                  'steps': [dict(s, status='not_started') for s in
                            engine._make_steps(1, baseline, final, targets)],
                  'recovery_ready': False, 'completed': False}
        step = detail['steps'][0]
        step['status'] = 'attempted'
        step['attempts'] = [{'before': copy.deepcopy(baseline['40002']),
            'expected': engine._after_step(baseline, step)['40002'], 'request_token': 17,
            'readback': copy.deepcopy(baseline['40002']), 'readback_rounds': 2,
            'readback_matches_expected': False}]
        if status == 'verified':
            detail['outcome'] = 'unchanged'
        detail['plan_sha256'] = engine._plan_digest(detail)
        engine._validate_journal(detail)
        self.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
            (identifier, 'scene', 40002, status, json.dumps(detail)))
        return copy.deepcopy(self.store.journals()[-1])

    async def test_closed_legacy_colour_is_quarantined_without_startup_writes(self):
        original = self.legacy_receipt('a' * 32)
        duplicate = self.legacy_receipt('b' * 32)
        response = await self.client.get('/api/scenes', headers=self.headers)
        state = await response.json()
        self.assertEqual(len(state['operations']), 1)
        progress = state['operations'][0]['progress']
        self.assertTrue(progress['legacy_ambiguous'])
        self.assertFalse(progress['recovery_ready'])
        self.assertEqual(progress['legacy_sources'], [original['id'], duplicate['id']])
        self.assertEqual(state['scenes'][0]['id'], duplicate['id'])
        self.assertEqual(state['scenes'][0]['status'], 'needs_review')
        self.assertIn(1, self.runtime.scene_engine._reserved())
        self.assertEqual(self.store.journals(), [original, duplicate])
        self.assertEqual(self.radio.frames, [])
        with self.assertRaisesRegex(RuntimeError, 'unfinished'):
            await self.runtime.scene_engine.create('Other', [{'target': 40001, 'brightness': 50, 'kelvin': 3000}])
        self.assertEqual(self.radio.frames, [])

    async def test_explicit_legacy_recovery_preserves_receipts_and_only_finishes_brightness(self):
        original = self.legacy_receipt('a' * 32)
        duplicate = self.legacy_receipt('b' * 32)
        self.radio.stage_colour = True
        self.radio.staged[(40002, 1)] = bytes.fromhex('020fa000')
        # Either historical operation URL must resolve the same latest intent.
        state = await self.operation('recover', {'transaction': original['id']})
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.store.journals()[:2], [original, duplicate])
        self.assertEqual(len(state['operations']), 1)
        migrated = state['operations'][0]
        self.assertNotEqual(migrated['transaction'], duplicate['id'])
        self.assertTrue(migrated['progress']['recovery_ready'])
        self.assertEqual(migrated['progress']['recovery_mode'], 'continue_unattempted')
        self.assertEqual(migrated['progress']['legacy_sources'], [original['id'], duplicate['id']])
        state = await self.operation('resume', {'transaction': migrated['transaction']})
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(self.mutations()[0].payload[5], 33)
        self.assertEqual(self.radio.rows[(40002, 1)], bytes.fromhex('020fa000'))
        self.assertEqual(self.radio.rows[(40002, 33)], bytes.fromhex('64000000'))
        self.assertEqual(state['operations'], [])
        self.assertEqual(state['scenes'][0]['status'], 'ready')
        self.assertEqual(self.store.journals()[:2], [original, duplicate])
        # Reopening an old URL cannot create a second continuation.
        with self.assertRaises(ValueError):
            await self.runtime.scene_engine.recover(duplicate['id'])
        self.assertEqual(len(self.store.journals()), 3)
        self.assertEqual(len(self.mutations()), 1)

    async def completed_legacy_recovery(self):
        original = self.legacy_receipt('a' * 32)
        self.legacy_receipt('b' * 32)
        self.radio.stage_colour = True
        self.radio.staged[(40002, 1)] = bytes.fromhex('020fa000')
        state = await self.operation('recover', {'transaction': original['id']})
        transaction = state['operations'][0]['transaction']
        await self.operation('resume', {'transaction': transaction})
        return transaction

    async def test_completed_legacy_recovery_survives_inventory_changes_and_restart(self):
        import copy
        from avionmesh_pi.lifecycle import reload_inventory
        from avionmesh_pi.scene_engine import SceneEngine
        transaction = await self.completed_legacy_recovery()
        before = self.store.journals()
        frames = list(self.radio.frames)
        variants = []
        added = copy.deepcopy(INVENTORY)
        added['devices'].append({'device_id': 40004, 'product_id': 134, 'name': 'New bulb'})
        variants.append(added)
        removed = copy.deepcopy(INVENTORY)
        removed['devices'] = [d for d in removed['devices'] if d['device_id'] != 40002]
        variants.append(removed)
        retyped = copy.deepcopy(INVENTORY)
        retyped['devices'][1]['product_id'] = 91
        variants.append(retyped)
        for inventory in variants:
            with self.subTest(devices=inventory['devices']):
                self.store.import_inventory(inventory)
                reload_inventory(self.runtime)
                self.runtime.scene_engine = SceneEngine(self.runtime)
                response = await self.client.get('/api/scenes', headers=self.headers)
                state = await response.json()
                self.assertEqual(state['operations'], [])
                # Historical completion neither grants current activation proof
                # nor permits an old recovery URL to send another write.
                self.assertFalse(state['scenes'][0]['activation_ready'])
                for identifier in ('a' * 32, 'b' * 32, transaction):
                    with self.assertRaises(ValueError):
                        await self.runtime.scene_engine.recover(identifier)
                self.assertEqual(self.store.journals(), before)
                self.assertEqual(self.radio.frames, frames)

    async def test_historical_completion_does_not_hide_incomplete_or_damaged_recovery(self):
        import copy
        from avionmesh_pi.lifecycle import reload_inventory
        transaction = await self.completed_legacy_recovery()
        baseline = self.store.journals()[-1]
        frames = list(self.radio.frames)
        added = copy.deepcopy(INVENTORY)
        added['devices'].append({'device_id': 40004, 'product_id': 134, 'name': 'New bulb'})
        self.store.import_inventory(added)
        reload_inventory(self.runtime)
        for fault in ('pending', 'incomplete', 'digest', 'owner', 'unverified_step', 'unsent', 'wrong_sources'):
            with self.subTest(fault=fault):
                bad = copy.deepcopy(baseline)
                d = bad['detail']
                if fault == 'pending': bad['status'] = 'uncertain'
                elif fault == 'incomplete': d['completed'] = False
                elif fault == 'digest': d['plan_sha256'] = 'invalid'
                elif fault == 'owner':
                    d['desired']['owner'] = 'different mesh'
                    d['plan_sha256'] = self.runtime.scene_engine._plan_digest(d)
                elif fault == 'unverified_step': d['steps'][0]['status'] = 'attempted'
                elif fault == 'unsent':
                    d['steps'][0]['attempts'][-1]['commands'][1].update(status='not_started', request_token=None)
                elif fault == 'wrong_sources': d['legacy_sources'] = ['unrelated']
                self.store.db.execute('UPDATE journals SET status=?,detail=? WHERE id=?',
                    (bad['status'], json.dumps(d), transaction))
                before = self.store.journals()
                response = await self.client.get('/api/scenes', headers=self.headers)
                state = await response.json()
                self.assertTrue(any(o['progress'].get('legacy_ambiguous') for o in state['operations']))
                with self.assertRaises(RuntimeError):
                    await self.runtime.scene_engine.create('Blocked', [{'target': 40001, 'brightness': 50, 'kelvin': 3000}])
                self.assertEqual(self.store.journals(), before)
                self.assertEqual(self.radio.frames, frames)
        self.store.db.execute('UPDATE journals SET status=?,detail=? WHERE id=?',
            (baseline['status'], json.dumps(baseline['detail']), transaction))
        self.assertEqual(self.runtime.scene_engine.status()['operations'], [])

    async def test_pending_legacy_receipt_transfers_atomically_without_losing_detail(self):
        original = self.legacy_receipt('a' * 32, status='uncertain')
        self.radio.stage_colour = True
        self.radio.staged[(40002, 1)] = bytes.fromhex('020fa000')
        state = await self.operation('recover', {'transaction': original['id']})
        journals = self.store.journals()
        self.assertEqual(journals[0]['detail'], original['detail'])
        self.assertEqual(journals[0]['status'], 'cancelled')
        self.assertEqual(len(self.store.journals(pending=True)), 1)
        self.assertEqual(len(state['operations']), 1)
        self.assertEqual(self.mutations(), [])

    async def test_divergent_legacy_receipts_remain_reserved_without_queries(self):
        self.legacy_receipt('a' * 32)
        self.legacy_receipt('b' * 32, brightness=180)
        before = self.store.journals()
        state = await self.operation('recover', {'transaction': 'b' * 32}, expected_state='failed')
        self.assertEqual(self.store.journals(), before)
        self.assertEqual(self.radio.frames, [])
        self.assertTrue(state['operations'][0]['progress']['legacy_conflict'])
        self.assertIn(1, self.runtime.scene_engine._reserved())

    async def test_invalid_legacy_receipt_fails_before_any_radio_queries(self):
        self.legacy_receipt('a' * 32)
        journal = self.store.journals()[0]
        journal['detail']['steps'][0]['value'] = '020bb800'
        self.store.db.execute('UPDATE journals SET detail=? WHERE id=?',
                              (json.dumps(journal['detail']), journal['id']))
        state = await self.operation('recover', {'transaction': journal['id']}, expected_state='failed')
        self.assertEqual(self.radio.frames, [])
        self.assertEqual(len(self.store.journals()), 1)
        self.assertTrue(state['operations'][0]['progress']['legacy_ambiguous'])

    async def test_explicit_keypad_comparison_preserves_presets_and_normal_activation(self):
        state = await self.operation('create', {'name': 'Comparison',
            'members': [{'target': 40001, 'brightness': 128, 'kelvin': 4000},
                        {'target': 40002, 'brightness': 64, 'kelvin': 2700}], 'exposed': True})
        identifier = state['scenes'][0]['id']
        saved = self.store.get(SCENES_KEY)
        radio_rows = dict(self.radio.rows)
        for action, flag, profile in [('activate-keypad-test',3,'keypad_capture'),
                                      ('activate',1,'halo_app')]:
            before = len(self.radio.frames)
            state = await self.operation(action, {'id': identifier})
            self.assertEqual(len(self.radio.frames), before+1)
            packet = self.radio.packets[-1]
            self.assertEqual(packet.mcp_address, 0)
            self.assertEqual(packet.payload[:2], bytes([0,34]))
            self.assertEqual(packet.payload[6], flag)
            result = state['job']['result']
            self.assertEqual(result['recall_profile'], profile)
            self.assertEqual(result['verification_reads'], 0)
            self.assertGreaterEqual(result['timing']['request_to_submission_ms'], 0)
            self.assertEqual(self.store.get(SCENES_KEY), saved)
            self.assertEqual(self.radio.rows, radio_rows)
        self.runtime.scene_engine._revoke(identifier)
        before = len(self.radio.frames)
        state = await self.operation('activate-keypad-test', {'id':identifier}, expected_state='failed')
        self.assertEqual(len(self.radio.frames), before)
        self.assertIn('verification', state['job']['error'])

    # ---- unpowered lights, restarted lights and owner exits through the real runtime ----
    def silence(self, target):
        original = self.radio.send
        async def send(frame):
            packet = decrypt_mcp(frame, KEY)
            if packet.mcp_address == target and packet.payload[0] == 1:
                self.radio.frames.append(frame); self.radio.packets.append(packet)
                return                                   # transmitted, never answered
            await original(frame)
        self.radio.send = send
        return lambda: setattr(self.radio, 'send', original)

    async def test_unpowered_other_light_is_named_then_left_unchecked_by_the_owner(self):
        self.runtime.scene_engine.read_timeout = .02
        restore = self.silence(40003)
        body = {'name': 'Evening', 'members': [{'target': 40001, 'brightness': 128, 'kelvin': 4000}], 'exposed': False}
        state = await self.operation('create', body, expected_state='failed')
        self.assertEqual(state['job']['unreachable'], 40003)
        self.assertEqual(state['scenes'], []); self.assertEqual(state['operations'], [])
        self.assertFalse(any(p.payload[0] in (2, 5) for p in self.radio.packets))
        self.assertEqual(len(self.store.journals(pending=True)), 0)
        self.radio.packets.clear()
        state = await self.operation('create', dict(body, skip=[40003]))
        self.assertTrue(state['job']['result']['verified'])
        self.assertFalse(any(p.mcp_address == 40003 for p in self.radio.packets))
        scene = state['scenes'][0]
        self.assertEqual(scene['unchecked'], [40003]); self.assertTrue(scene['activation_ready'])
        # The owner cannot leave a light of the scene itself unread.
        response = await self.client.post('/api/scenes/remove', headers=self.headers, json={'id': scene['id'], 'skip': [40001]})
        self.assertEqual(response.status, 202)
        for _ in range(300):
            state = await (await self.client.get('/api/scenes', headers=self.headers)).json()
            if state['job']['state'] != 'running': break
            await asyncio.sleep(.001)
        self.assertEqual(state['job']['state'], 'failed'); self.assertEqual(len(state['scenes']), 1)
        restore()
        state = await self.operation('verify', {'id': scene['id']})
        self.assertEqual(state['scenes'][0]['unchecked'], [])

    async def test_light_whose_reply_count_restarts_is_read_again_through_real_ingress(self):
        self.runtime.scene_engine.read_timeout = .02
        state = await self.operation('create', {'name': 'Evening', 'members': [
            {'target': 40001, 'brightness': 128, 'kelvin': 4000}], 'exposed': False})
        identifier = state['scenes'][0]['id']
        self.radio.reply_sequences[40001] = 0            # synthetic power cut: the light counts from one again
        self.radio.packets.clear()
        state = await self.operation('verify', {'id': identifier})
        self.assertTrue(state['job']['result']['verified'])
        self.assertTrue(all(p.payload[0] == 1 for p in self.radio.packets))   # reads only
        self.assertEqual(sum(p.mcp_address == 40001 for p in self.radio.packets), 5)

    async def test_set_aside_frees_the_bridge_and_keeps_the_receipt(self):
        state = await self.operation('create', {'name': 'Evening', 'members': [
            {'target': 40001, 'brightness': 128, 'kelvin': 4000}], 'exposed': True})
        identifier = state['scenes'][0]['id']
        self.radio.fail_after_colour_once = True
        state = await self.operation('edit', {'id': identifier, 'name': 'Evening', 'members': [
            {'target': 40001, 'brightness': 20, 'kelvin': 3000}]}, expected_state='failed')
        transaction = state['operations'][0]['transaction']
        self.assertEqual(self.runtime.status()['pending_transactions'], 1)
        sent = len(self.radio.frames)
        self.runtime.mesh_ready = False                  # works even while the mesh is unreachable
        response = await self.client.post('/api/scenes/set-aside', headers=self.headers, json={'transaction': transaction})
        self.assertEqual(response.status, 200, await response.text())
        self.assertTrue((await response.json())['result']['reference_reserved'])
        self.assertEqual(len(self.radio.frames), sent)
        self.assertEqual(self.runtime.status()['pending_transactions'], 0)
        state = await (await self.client.get('/api/scenes', headers=self.headers)).json()
        self.assertEqual(state['operations'], []); self.assertIsNone(state['job'])
        self.assertTrue(state['scenes'][0]['set_aside']); self.assertFalse(state['scenes'][0]['activation_ready'])
        receipt = next(j for j in self.store.journals() if j['id'] == transaction)
        self.assertEqual((receipt['status'], receipt['detail']['outcome']), ('retired', 'set_aside'))
        response = await self.client.post('/api/scenes/forget', headers=self.headers, json={'id': identifier})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.store.get(SCENES_KEY), []); self.assertEqual(len(self.radio.frames), sent)
        config = [m for m in self.scene_messages() if m['topic'].endswith('/config')][-1]
        self.assertEqual(config['payload'], '')          # withdrawn from Home Assistant
