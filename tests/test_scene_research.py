"""Synthetic-only scene groundwork and existing controller safety regression checks."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi import scene_research as scene
from avionmesh_pi.__main__ import main
from avionmesh_pi.controllers import ControllerRouter, named_inputs
from avionmesh_pi.diagnostics import Diagnostics
from avionmesh_pi.keypad import plan_edit, snapshot
from avionmesh_pi.protocol import Packet

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/scene-research.json'


def capture():
    return json.loads(FIXTURE.read_text())


def packet(payload, source=40001, address=0, opcode=115):
    return Packet(7, source, address, opcode, bytes.fromhex(payload))


class QueryTests(unittest.TestCase):
    def test_exploratory_count_and_read_wire_shapes(self):
        self.assertEqual(scene.scene_table_query(40002).hex(), '429c7304220000000000')
        self.assertEqual(scene.scene_table_query(40002, 7).hex(), '429c730122000000070000')

    def test_no_group_host_broadcast_or_out_of_range_query(self):
        for target in [0, 291, 32768, 32895, 65408, 65535, True, '40002']:
            with self.subTest(target=target), self.assertRaises(ValueError):
                scene.scene_table_query(target)
        for selector in [0, -1, True, 21, 255, '1']:
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                scene.scene_table_query(40002, selector)

    def test_queries_expose_no_write_verb_or_transport(self):
        for selector in [None, *range(1, 21)]:
            command = scene.scene_table_query(40002, selector)
            self.assertIn(command[3], (1, 4))
            self.assertEqual(command[:3], bytes.fromhex('429c73'))
            self.assertEqual(command[4], 0x22)


class ObservationTests(unittest.TestCase):
    def test_trigger_fields_are_raw_and_not_group_or_scene_ids(self):
        row = scene.describe_scene_packet(packet('00220007000703000000'))
        self.assertEqual(row['kind'], 'scene_trigger_candidate')
        self.assertEqual(row['header_reference_raw'], 7)
        self.assertEqual(row['value_bytes_raw'], '070300')
        for forbidden in ('scene_id', 'group', 'brightness', 'kelvin', 'activation', 'button'):
            self.assertNotIn(forbidden, row)
        self.assertFalse(row['semantics_verified'])

    def test_unknown_scene_formats_are_preserved_without_interpretation(self):
        for payload in ('00220007000704000000', '0222aa', '012200'):
            row = scene.describe_scene_packet(packet(payload))
            self.assertEqual(row['kind'], 'unclassified_scene_message')
            self.assertEqual(row['payload_hex'], payload)

    def test_non_scene_and_non_mcp_packets_are_ignored(self):
        for message in (packet('000a012300ff00000000'), packet('0022', opcode=4), packet('00'),
                        packet('01300002012308ff')):
            self.assertIsNone(scene.describe_scene_packet(message))

    def test_scene_binding_is_distinct_from_brightness_target(self):
        row = scene.describe_scene_packet(packet('0130000200072007'))
        self.assertEqual(row['kind'], 'keypad_scene_binding_candidate')
        self.assertEqual(row['target_reference_raw'], 7)
        self.assertEqual(row['value_raw'], 7)
        self.assertEqual(row['slot'], 2)

    def test_count_is_reported_not_accepted_as_complete_backup(self):
        row = scene.describe_scene_packet(packet('0422000114', source=40002))
        self.assertEqual(row['reported_count_raw'], 1)
        self.assertEqual(row['reported_capacity_raw'], 20)
        self.assertFalse(row['semantics_verified'])

    def test_row_does_not_guess_colour_brightness_or_empty_status(self):
        for payload in ('01220007020fa000', '01220008ffffffff'):
            row = scene.describe_scene_packet(packet(payload, source=40002))
            self.assertEqual(row['kind'], 'scene_row_candidate')
            self.assertEqual(row['row_bytes_raw'], payload[8:])
            self.assertNotIn('brightness', row)
            self.assertNotIn('color_temp', row)
            self.assertNotIn('empty', row)

    def test_unknown_keypad_scene_binding_stays_protected_by_existing_editor(self):
        table = snapshot(bytes.fromhex('0430000104'), [
            bytes.fromhex('0130000100072007'), bytes.fromhex('01300002ffff0000'),
            bytes.fromhex('01300003ffff0000'), bytes.fromhex('01300004ffff0000')])
        for mode in ('native', 'unassigned'):
            with self.assertRaisesRegex(ValueError, 'read-only'):
                plan_edit(table, 1, mode, target=291 if mode == 'native' else None)
        router = ControllerRouter()
        router.bindings[40001] = table
        event = router.decode(packet('00220007000703000000'), 127, 10)
        self.assertNotIn('activation', event)
        self.assertNotIn('button', event)
        self.assertEqual(named_inputs(event, 127), [])


class CaptureTests(unittest.TestCase):
    def test_synthetic_inspection_retains_uncertainty_and_does_not_mutate_input(self):
        source = capture(); before = deepcopy(source)
        result = scene.inspect_capture(source)
        self.assertEqual(source, before)
        self.assertEqual(result['counts'], {'scene_trigger_candidate': 1,
            'keypad_scene_binding_candidate': 1, 'scene_count_candidate': 1,
            'scene_row_candidate': 2, 'unclassified_scene_message': 1})
        self.assertEqual(result['ignored_records'], 1)
        for name in ('scene_backup_complete', 'authentication_revalidated',
                     'programming_supported', 'recall_supported'):
            self.assertFalse(result[name])

    def test_repeated_observations_keep_sequence_and_timing_not_activation_counts(self):
        source = capture()
        source['records'] = [source['records'][0], deepcopy(source['records'][0])]
        source['records'][1]['elapsed_ms'] = 200
        result = scene.inspect_capture(source)
        self.assertEqual(result['counts']['scene_trigger_candidate'], 2)
        self.assertEqual([r['sequence'] for r in result['observations']], [1, 1])
        self.assertEqual([r['elapsed_ms'] for r in result['observations']], [100, 200])
        self.assertTrue(all('activation' not in r for r in result['observations']))

    def test_empty_capture_is_not_evidence_of_empty_scene_table(self):
        source = capture(); source['records'] = []
        result = scene.inspect_capture(source)
        self.assertIn('no_scene_observations_does_not_mean_no_scenes', result['warnings'])
        self.assertFalse(result['scene_backup_complete'])

    def test_incomplete_dropped_session_changed_and_group_filtered_capture_are_flagged(self):
        source = capture()
        source.update(complete=False, dropped=1, truncated=1, stopped='session_changed', scope='group')
        result = scene.inspect_capture(source)
        self.assertEqual(set(result['warnings']), {'capture_still_open', 'capture_dropped_records',
            'capture_truncated_records', 'connection_changed', 'group_filter_excludes_scene_protocol'})

    def test_local_transport_acceptance_is_never_radio_delivery_proof(self):
        source = capture(); row = source['records'][0]
        row.update(local_tx=True, authenticated_rx=False, transport_accepted=True)
        result = scene.inspect_capture(source)['observations'][0]
        self.assertEqual(result['origin'], 'local_tx_attempt')
        self.assertTrue(result['transport_accepted'])
        self.assertNotIn('remote_acknowledged', result)
        self.assertFalse(result['semantics_verified'])

    def test_authentication_metadata_and_session_must_be_consistent(self):
        for changes in ({'local_tx':True}, {'authenticated_rx':False}, {'session':2}, {'session':True},
                        {'source':True}, {'address':-1}, {'sequence':True}, {'sequence_known':False},
                        {'elapsed_ms':-1}, {'original_length':3}, {'payload':'xx'}):
            source = capture(); source['records'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                scene.inspect_capture(source)

    def test_credentials_and_unrelated_fields_are_never_copied_to_report(self):
        source = capture(); source.update(secret='synthetic-private-marker')
        source['records'][0]['other'] = 'synthetic-private-marker'
        self.assertNotIn('synthetic-private-marker', json.dumps(scene.inspect_capture(source)))
        for flag in ('mesh_key_included', 'masp_included'):
            source = capture(); source[flag] = True
            with self.assertRaises(ValueError): scene.inspect_capture(source)

    def test_oversized_document_record_count_and_payload_are_rejected(self):
        source = capture(); source['records'] *= 37
        with self.assertRaises(ValueError): scene.inspect_capture(source)
        source = capture(); source['records'][0].update(payload='0022'+'00'*24, original_length=26)
        with self.assertRaises(ValueError): scene.inspect_capture(source)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'large.json'); path.write_bytes(b' ' * (256 * 1024 + 1))
            with self.assertRaises(ValueError): scene.inspect_file(path)

    def test_scoped_diagnostics_now_retain_scene_rx_and_queries(self):
        now=[0]; d=Diagnostics(lambda:now[0]); start=d.start(40001,1)
        d.observe(packet('00220007000703000000'),0)
        d.observe(packet('00220007000703000000', source=40002),0)
        d.observe(packet('00220007000703000000', opcode=4),0)
        d.transmitted(scene.scene_table_query(40001),8,0,True,session=d.session,owner=start['id'])
        result=d.export(start['id'],stop=True)
        inspection=scene.inspect_capture(result)
        self.assertEqual(len(inspection['observations']),2)
        self.assertEqual(inspection['observations'][0]['kind'],'scene_trigger_candidate')
        self.assertEqual(inspection['observations'][1]['origin'],'local_tx_attempt')
        self.assertFalse(result['mesh_key_included'])
        self.assertFalse(result['masp_included'])

    def test_group_scope_does_not_broaden_to_unknown_scene_targets(self):
        d=Diagnostics(lambda:0); start=d.start_group(291,[40002],1)
        d.observe(packet('00220123000703000000'),0)
        self.assertEqual(d.export(start['id'])['records'],[])

    def test_cli_is_offline_and_outputs_observations(self):
        output=io.StringIO()
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), redirect_stdout(output):
            self.assertEqual(main(['scene-inspect',str(FIXTURE)]),0)
        self.assertFalse(json.loads(output.getvalue())['programming_supported'])

    def test_cli_error_does_not_echo_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory,'input.json');path.write_text('{"secret":"do-not-echo"}')
            output=io.StringIO();errors=io.StringIO()
            with redirect_stdout(output),redirect_stderr(errors):
                self.assertEqual(main(['scene-inspect',str(path)]),2)
            self.assertNotIn('do-not-echo',errors.getvalue()+output.getvalue())



def esp_capture(*, request='synthetic-request-1', session=12, local=False, indexes=(1,)):
    """Invented collector schema, identifiers and scene bytes; no installation data."""
    common = {'request_id': request, 'session': session}
    status = {**common, 'schema': 2, 'status': 'ok', 'active': True,
              'received': 0, 'dropped': 0, 'truncated': 0, 'queued': 0,
              'transport_queue_dropped_start': 7, 'transport_queue_dropped_total': 7}
    records = [{'kind': 'header', 'request_id': request},
               {**status, 'kind': 'start', 'action': 'capture_start'}]
    for index in indexes:
        row = {**common, 'kind': 'packet', 'schema': 2, 'capture_index': index,
               'crypto_source': 40001, 'mcp_source': 0, 'opcode': 115,
               'received_ms': 100 + index, 'direction': 'local_tx' if local else 'rx',
               'sequence': None if local else 100 + index,
               'payload': '00220007000701000000', 'original_length': 10, 'truncated': False}
        if local: row['tx_result'] = 0
        records.append(row)
    received = max(indexes, default=0)
    records.append({**status, 'kind': 'status', 'action': 'capture_complete',
                    'active': False, 'received': received})
    records.append({**common, 'kind': 'collector_summary', 'completion_received': True,
                    'rf_complete': 'unknown', 'externally_stopped': False,
                    'sources': {'40001': len(indexes)} if indexes else {},
                    'dropped': 0, 'association_rows': 0, 'capture_queue_dropped': 0,
                    'transport_queue_dropped_start': 7, 'transport_queue_dropped_total': 7,
                    'transport_queue_dropped_delta': 0, 'final_received': received,
                    'delivery_gap_assessment': 'indexed', **scene._esp_indices(list(indexes), received)})
    return records


class EspCaptureTests(unittest.TestCase):
    def test_owned_schema_two_capture_keeps_origin_and_unknown_rf_scope(self):
        source = esp_capture(); before = deepcopy(source)
        result = scene.inspect_esp_capture(source)
        self.assertEqual(source, before)
        self.assertTrue(result['capture_complete'])
        self.assertEqual(result['rf_complete'], 'unknown')
        self.assertFalse(result['authentication_revalidated'])
        self.assertFalse(result['programming_supported'])
        self.assertFalse(result['scene_backup_complete'])
        row = result['observations'][0]
        self.assertEqual(row['origin'], 'recorder_asserted_authenticated_rx')
        self.assertEqual((row['session'], row['request_id'], row['capture_index']),
                         (12, 'synthetic-request-1', 1))
        self.assertEqual(row['sequence'], 101)
        self.assertEqual(row['received_ms'], 101)
        self.assertEqual(row['flags_raw'], 1)

    def test_local_tx_never_forges_sequence_authentication_or_remote_success(self):
        result = scene.inspect_esp_capture(esp_capture(local=True))
        row = result['observations'][0]
        self.assertEqual(row['origin'], 'local_tx_attempt')
        self.assertIsNone(row['sequence'])
        self.assertFalse(row['sequence_known'])
        self.assertEqual(row['tx_result_raw'], 0)
        self.assertNotIn('transport_accepted', row)
        self.assertNotIn('remote_acknowledged', row)
        self.assertEqual(result['segments'][0]['recorded_local_tx'], 1)

    def test_renewed_segments_can_reuse_session_but_not_request_identity(self):
        source = esp_capture() + esp_capture(request='synthetic-request-2')
        result = scene.inspect_esp_capture(source)
        self.assertEqual(len(result['segments']), 2)
        self.assertTrue(result['capture_complete'])
        with self.assertRaises(ValueError):
            scene.inspect_esp_capture(esp_capture() + esp_capture())

    def test_partial_and_missing_collector_summary_do_not_claim_completion(self):
        for source in (esp_capture()[:-2], esp_capture()[:-1]):
            result = scene.inspect_esp_capture(source)
            self.assertFalse(result['capture_complete'])
            self.assertIn('capture_incomplete', result['warnings'])
            self.assertIsNone(result['segments'][0]['collector_dropped'])
        source = esp_capture()[:-2]
        source.append({'kind': 'incomplete', 'request_id': 'synthetic-request-1',
                       'session': 12, 'completion_received': False, 'dropped': 2,
                       'message': 'private-error-never-copy'})
        result = scene.inspect_esp_capture(source)
        self.assertIn('collector_dropped_records', result['warnings'])
        self.assertNotIn('private-error-never-copy', json.dumps(result))

    def test_loss_and_index_gaps_are_retained_without_rf_completeness(self):
        source = esp_capture(indexes=(2, 4))
        source[-2].update(received=6, dropped=2, truncated=1, transport_queue_dropped_total=9)
        source[-1].update(final_received=6, tail_index_gap=[5, 6], capture_queue_dropped=2,
                          transport_queue_dropped_total=9, transport_queue_dropped_delta=2, dropped=3)
        result = scene.inspect_esp_capture(source)
        self.assertTrue(result['capture_complete'])
        self.assertEqual(result['segments'][0]['prefix_index_gap'], [1, 1])
        self.assertEqual(result['segments'][0]['interior_index_gaps'], [[3, 3]])
        self.assertEqual(result['segments'][0]['tail_index_gap'], [5, 6])
        self.assertTrue({'capture_index_gaps', 'capture_dropped_records', 'collector_dropped_records',
                         'transport_dropped_records', 'capture_truncated_records'} <= set(result['warnings']))

    def test_duplicate_indices_are_preserved_as_warning_not_collapsed(self):
        result = scene.inspect_esp_capture(esp_capture(indexes=(1, 1)))
        self.assertEqual(len(result['observations']), 2)
        self.assertIn('capture_nonincreasing_indices', result['warnings'])

    def test_empty_completed_recording_is_not_an_empty_scene_table(self):
        result = scene.inspect_esp_capture(esp_capture(indexes=()))
        self.assertTrue(result['capture_complete'])
        self.assertFalse(result['scene_backup_complete'])
        self.assertIn('no_scene_observations_does_not_mean_no_scenes', result['warnings'])
        source = esp_capture()[:1]
        source.append({'kind': 'incomplete', 'request_id': 'synthetic-request-1',
                       'session': None, 'completion_received': False, 'dropped': 0})
        result = scene.inspect_esp_capture(source)
        self.assertFalse(result['capture_complete'])
        self.assertIn('capture_not_started', result['warnings'])

    def test_truncated_prefix_is_not_decoded_as_a_complete_scene_trigger(self):
        source = esp_capture()
        source[2].update(truncated=True, original_length=11)
        source[-2]['truncated'] = 1
        result = scene.inspect_esp_capture(source)
        self.assertEqual(result['observations'][0]['kind'], 'truncated_scene_message')
        self.assertNotIn('flags_raw', result['observations'][0])
        self.assertIn('capture_truncated_records', result['warnings'])

    def test_bad_ownership_metadata_phase_and_summary_fail_closed(self):
        changes = [
            (1, {'schema': True}), (1, {'received': 1}), (1, {'active': False}),
            (2, {'session': 13}), (2, {'request_id': 'foreign-request'}),
            (2, {'direction': 'unknown'}), (2, {'sequence': None}),
            (2, {'sequence': True}), (2, {'tx_result': 0}),
            (2, {'capture_index': 0}), (2, {'payload': 'invalid-secret-bytes'}),
            (2, {'original_length': 12}), (2, {'truncated': True}),
            (2, {'received_ms': True}), (2, {'crypto_source': 0}),
            (-2, {'active': True}), (-2, {'queued': 1}), (-2, {'received': 0}),
            (-2, {'association_trace': {'active': False, 'queued': 0, 'session': 99}}),
            (-1, {'completion_received': False}), (-1, {'rf_complete': True}),
            (-1, {'last_capture_index': 2}), (-1, {'indexed_rows': True}),
            (-1, {'sources': {'40001': 2}}), (-1, {'transport_queue_dropped_delta': 99})]
        for index, update in changes:
            source = esp_capture(); source[index].update(update)
            with self.subTest(index=index, update=update), self.assertRaises(ValueError):
                scene.inspect_esp_capture(source)
        for source in (esp_capture()[1:], esp_capture()[:-2] + esp_capture()[-1:],
                       esp_capture()[:2] + [esp_capture()[1]], esp_capture() + [esp_capture()[2]]):
            with self.assertRaises(ValueError): scene.inspect_esp_capture(source)
        source = esp_capture(local=True); source[2]['sequence'] = 3
        with self.assertRaises(ValueError): scene.inspect_esp_capture(source)

    def test_raw_secrets_labels_and_unrelated_fields_are_not_copied(self):
        source = esp_capture()
        for row in source:
            row.update(secret='synthetic-private-marker', label='synthetic-private-marker')
        self.assertNotIn('synthetic-private-marker', json.dumps(scene.inspect_esp_capture(source)))

    def test_file_bounds_explicit_format_and_cli_remain_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'capture.jsonl')
            path.write_text('\n'.join(json.dumps(row) for row in esp_capture()) + '\n')
            with self.assertRaises(ValueError): scene.inspect_file(path)
            with patch('socket.socket', side_effect=AssertionError('network forbidden')):
                self.assertTrue(scene.inspect_file(path, input_format='esp-jsonl')['capture_complete'])
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(['scene-inspect', str(path), '--input-format', 'esp-jsonl']), 0)
            self.assertEqual(json.loads(output.getvalue())['input_format'], 'esp-jsonl')
            for raw in (b'{' + b'private-input', b'\n' * (scene.MAX_ESP_RECORDS + 1),
                        b' ' * (scene.MAX_ESP_BYTES + 1), b'[' * 2000):
                path.write_bytes(raw)
                with self.assertRaises(ValueError): scene.inspect_file(path, input_format='esp-jsonl')
            with self.assertRaises(ValueError): scene.inspect_file(path, input_format='unknown')
        with self.assertRaises(ValueError): scene.inspect_esp_capture([])
        with self.assertRaises(ValueError): scene.inspect_esp_capture([{}] * (scene.MAX_ESP_RECORDS + 1))


if __name__ == '__main__': unittest.main()
