"""Offline scene investigation. Candidate layouts only; no transport or setters.

See docs/SCENES.md for evidence and limits. COUNT/READ framing follows the earlier
local table probe; field meanings beyond that evidence remain explicitly raw.
"""
from collections import Counter
import json
from pathlib import Path
import re

from .protocol import Packet, integer

SCENE_NOUN = 0x22
MAX_RESEARCH_SELECTOR = 20  # Deliberate probe bound, not a universal device capacity.


def scene_table_query(target, selector=None):
    """Build an exploratory direct COUNT/READ, never send or allocate a sequence.

    A future service must check inventory ownership, pending operations and query
    correlation before sending. This helper deliberately provides no recall/write.
    """
    integer(target, 32896, 65407, 'scene query device')
    if selector is None:
        payload = bytes([4, SCENE_NOUN, 0, 0, 0, 0, 0])
    else:
        integer(selector, 1, MAX_RESEARCH_SELECTOR, 'scene research selector')
        payload = bytes([1, SCENE_NOUN, 0, 0, 0, selector, 0, 0])
    return target.to_bytes(2, 'little') + b'\x73' + payload


def describe_scene_packet(packet):
    """Compatibility entry point for conservative, origin-independent decoding."""
    from .scene_protocol import describe_scene_message
    return describe_scene_message(packet)


def inspect_capture(document):
    """Inspect a bounded bridge Diagnostics export; never load a key or emit it.

    Export authentication flags are assertions made by the recorder. This offline
    inspection cannot reauthenticate packets and never upgrades them to proof.
    """
    if (not isinstance(document, dict) or type(document.get('format')) is not int
            or document['format'] not in (2, 3)
            or document.get('mesh_key_included') is not False
            or document.get('masp_included') is not False
            or type(document.get('complete')) is not bool):
        raise ValueError('Expected a credential-free bridge Diagnostics export')
    records = document.get('records')
    if not isinstance(records, list) or len(records) > 256:
        raise ValueError('Expected at most 256 diagnostic records')
    session = integer(document.get('session'), 0, 2**63 - 1, 'capture session')
    dropped = integer(document.get('dropped'), 0, 2**63 - 1, 'capture dropped count')
    truncated = integer(document.get('truncated'), 0, 2**63 - 1, 'capture truncated count')
    warnings = []
    if not document['complete']: warnings.append('capture_still_open')
    if dropped: warnings.append('capture_dropped_records')
    if truncated: warnings.append('capture_truncated_records')
    if document.get('stopped') == 'session_changed': warnings.append('connection_changed')
    if document.get('scope') == 'group': warnings.append('group_filter_excludes_scene_protocol')
    observations = []
    ignored = 0
    for index, row in enumerate(records):
        if (not isinstance(row, dict) or type(row.get('local_tx')) is not bool
                or type(row.get('authenticated_rx')) is not bool
                or type(row.get('session')) is not int or row['session'] != session):
            raise ValueError('Invalid diagnostic record metadata')
        payload = row.get('payload')
        if (not isinstance(payload, str) or re.fullmatch(r'(?:[0-9a-fA-F]{2}){2,23}', payload) is None
                or type(row.get('original_length')) is not int
                or row['original_length'] != len(payload) // 2):
            raise ValueError('Invalid diagnostic payload')
        if row['local_tx'] and row['authenticated_rx']:
            raise ValueError('Local transmission cannot be authenticated reception')
        integer(row.get('source'), 1, 65535, 'record source')
        integer(row.get('address'), 0, 65535, 'record destination')
        integer(row.get('opcode'), 0, 255, 'record opcode')
        sequence = integer(row.get('sequence'), 0, 0xffffff, 'record sequence')
        elapsed = integer(row.get('elapsed_ms'), 0, 60000, 'record elapsed time')
        if row.get('sequence_known') is not True:
            raise ValueError('Expected an explicit diagnostic sequence')
        if not row['local_tx'] and not row['authenticated_rx']:
            raise ValueError('Unauthenticated receive record is not supported')
        packet = Packet(sequence, row['source'], row['address'], row['opcode'], bytes.fromhex(payload))
        observation = describe_scene_packet(packet)
        if observation is None:
            ignored += 1
            continue
        observation.update(record_index=index, sequence=sequence, elapsed_ms=elapsed,
                           origin='local_tx_attempt' if row['local_tx'] else 'recorder_asserted_authenticated_rx')
        if row['local_tx']:
            accepted = row.get('transport_accepted')
            if type(accepted) is not bool:
                raise ValueError('Invalid local transmission result')
            observation['transport_accepted'] = accepted
        observations.append(observation)
    if not observations: warnings.append('no_scene_observations_does_not_mean_no_scenes')
    return {'format': 1, 'mode': 'offline_scene_inspection',
            'authentication_revalidated': False, 'scene_backup_complete': False,
            'programming_supported': False, 'recall_supported': False,
            'capture_complete': document['complete'], 'capture_dropped': dropped,
            'capture_truncated': truncated, 'warnings': warnings,
            'ignored_records': ignored, 'counts': dict(Counter(o['kind'] for o in observations)),
            'observations': observations}


def inspect_file(path, *, input_format="pi-json"):
    # Explicit formats keep a malformed Pi document from being treated as JSONL.
    if input_format not in ('pi-json', 'esp-jsonl'):
        raise ValueError('Unknown scene capture input format')
    limit = MAX_ESP_BYTES if input_format == 'esp-jsonl' else 256 * 1024
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Scene capture exceeds offline inspection limit')
    if input_format == 'pi-json':
        return inspect_capture(json.loads(raw))
    lines = raw.splitlines()
    if len(lines) > MAX_ESP_RECORDS:
        raise ValueError('Too many ESP capture records')
    try:
        records = [json.loads(line) for line in lines if line.strip()]
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError('Invalid ESP capture JSONL') from None
    return inspect_esp_capture(records)


MAX_ESP_BYTES = 2 * 1024 * 1024
MAX_ESP_RECORDS = 4096


def _esp_request(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is None:
        raise ValueError('Invalid ESP request identity')
    return value


def _esp_counter(row, name):
    return integer(row.get(name), 0, 0xffffffff, 'ESP ' + name)


def _esp_status(row):
    if row.get('schema') != 2 or type(row.get('schema')) is not int or row.get('status') != 'ok':
        raise ValueError('Expected successful ESP schema-2 status')
    if type(row.get('active')) is not bool:
        raise ValueError('Invalid ESP active flag')
    result = {key: _esp_counter(row, key) for key in
              ('received', 'dropped', 'truncated', 'queued',
               'transport_queue_dropped_start', 'transport_queue_dropped_total')}
    if result['transport_queue_dropped_total'] < result['transport_queue_dropped_start']:
        raise ValueError('Invalid ESP transport loss counters')
    return result


def _esp_indices(indexes, received=None):
    last = 0
    gaps = []
    nonincreasing = 0
    for index in indexes:
        if last and index > last + 1:
            gaps.append([last + 1, index - 1])
        if last and index <= last:
            nonincreasing += 1
        last = max(last, index)
    if received is not None and received < last:
        raise ValueError('ESP completion count precedes observed packets')
    return {'indexed_rows': len(indexes), 'last_capture_index': last if indexes else None,
            'prefix_index_gap': [1, indexes[0] - 1] if indexes and indexes[0] > 1 else None,
            'interior_index_gaps': gaps,
            'tail_index_gap': [last + 1, received] if received is not None and received > last else None,
            'nonincreasing_indices': nonincreasing}


def inspect_esp_capture(records):
    """Inspect owned schema-2 ESP collector records, without importing mesh state.

    This reads collector assertions only. RX does not become reauthenticated, and
    local TX has no known mesh sequence or remote acknowledgement. Session IDs
    may repeat after a restart; request IDs must remain unique in this document.
    Raw labels, errors, unrelated traffic and credentials are never copied out.
    """
    if not isinstance(records, list) or not records or len(records) > MAX_ESP_RECORDS:
        raise ValueError('Expected a bounded ESP collector record list')
    segments, observations, requests = [], [], set()
    state = None
    ignored = 0
    for row in records:
        if not isinstance(row, dict):
            raise ValueError('Invalid ESP collector record')
        kind = row.get('kind')
        if kind == 'header':
            if state is not None and not state['closed']:
                raise ValueError('ESP segment changed before collector closure')
            request = _esp_request(row.get('request_id'))
            if request in requests:
                raise ValueError('Repeated ESP capture request identity')
            requests.add(request)
            state = {'request_id': request, 'session': None, 'start': None, 'end': None,
                     'summary': None, 'last_status': None, 'indexes': [], 'sources': Counter(), 'truncated': 0,
                     'local_tx': 0, 'association_rows': 0, 'closed': False,
                     'incomplete': False, 'warnings': []}
            segments.append(state)
            continue
        if state is None or state['closed']:
            raise ValueError('ESP record outside an open collector segment')
        if kind in ('invalid_message', 'controller_transaction'):
            state['warnings'].append('collector_invalid_message' if kind == 'invalid_message'
                                     else 'controller_transaction_during_capture')
            continue
        if _esp_request(row.get('request_id')) != state['request_id']:
            raise ValueError('ESP request identity mismatch')
        if kind == 'start':
            if state['start'] is not None or row.get('action') != 'capture_start':
                raise ValueError('Invalid or repeated ESP capture start')
            state['session'] = _esp_counter(row, 'session')
            start = _esp_status(row)
            if not row['active'] or start['received'] or start['dropped'] or start['queued'] or start['truncated']:
                raise ValueError('ESP start is not a new active capture')
            state['start'] = state['last_status'] = start
            continue
        if kind == 'incomplete':
            session = row.get('session')
            if ((session is not None and type(session) is not int) or session != state['session']
                    or type(row.get('completion_received')) is not bool):
                raise ValueError('Invalid ESP incomplete segment identity')
            if row['completion_received'] != (state['end'] is not None):
                raise ValueError('Contradictory ESP incomplete completion flag')
            state['collector_dropped'] = _esp_counter(row, 'dropped')
            state['closed'] = state['incomplete'] = True
            continue
        if state['start'] is None or _esp_counter(row, 'session') != state['session']:
            raise ValueError('ESP record has no matching capture start')
        if kind == 'packet':
            if state['end'] is not None or type(row.get('schema')) is not int or row['schema'] != 2:
                raise ValueError('Invalid ESP packet schema or phase')
            index = integer(row.get('capture_index'), 1, 0xffffffff, 'ESP packet index')
            source = integer(row.get('crypto_source'), 1, 65535, 'ESP packet source')
            address = integer(row.get('mcp_source'), 0, 65535, 'ESP packet destination')
            opcode = integer(row.get('opcode'), 0, 255, 'ESP packet opcode')
            received = _esp_counter(row, 'received_ms')
            direction, sequence = row.get('direction'), row.get('sequence')
            if direction == 'rx':
                integer(sequence, 0, 0xffffff, 'ESP RX sequence')
                if 'tx_result' in row:
                    raise ValueError('RX record contains a local TX result')
            elif direction == 'local_tx':
                if sequence is not None or 'sequence' not in row:
                    raise ValueError('ESP local TX cannot claim a captured mesh sequence')
                integer(row.get('tx_result'), -0x80000000, 0x7fffffff, 'ESP TX result')
                state['local_tx'] += 1
            else:
                raise ValueError('Invalid ESP packet direction')
            payload = row.get('payload')
            if not isinstance(payload, str) or re.fullmatch(r'(?:[0-9a-fA-F]{2}){0,23}', payload) is None:
                raise ValueError('Invalid ESP packet payload')
            data = bytes.fromhex(payload)
            original = integer(row.get('original_length'), len(data), 65535, 'ESP original payload length')
            if type(row.get('truncated')) is not bool or row['truncated'] != (original != len(data)):
                raise ValueError('Inconsistent ESP payload truncation')
            state['indexes'].append(index)
            state['sources'][str(source)] += 1
            state['truncated'] += int(row['truncated'])
            # A prefix of a longer packet must never match a complete known shape.
            observation = None if row['truncated'] else describe_scene_packet(
                Packet(sequence, source, address, opcode, data))
            if row['truncated'] and len(data) >= 2 and data[1] == SCENE_NOUN:
                observation = {'kind': 'truncated_scene_message', 'source': source,
                               'destination': address, 'payload_hex': data.hex(), 'semantics_verified': False}
            if observation is None:
                ignored += 1
            else:
                observation.update(session=state['session'], request_id=state['request_id'],
                    capture_index=index, sequence=sequence, sequence_known=direction == 'rx',
                    received_ms=received, original_length=original, truncated=row['truncated'],
                    origin='local_tx_attempt' if direction == 'local_tx' else 'recorder_asserted_authenticated_rx')
                if direction == 'local_tx':
                    observation['tx_result_raw'] = row['tx_result']
                observations.append(observation)
        elif kind == 'association':
            if state['end'] is not None:
                raise ValueError('Association record after capture completion')
            state['association_rows'] += 1
        elif kind == 'status':
            if state['end'] is not None or row.get('action') not in ('capture_status', 'capture_stop', 'capture_complete'):
                raise ValueError('Invalid ESP capture status phase')
            counters = _esp_status(row)
            if counters['transport_queue_dropped_start'] != state['start']['transport_queue_dropped_start']:
                raise ValueError('ESP transport baseline changed within a capture')
            if any(counters[k] < state['last_status'][k] for k in
                   ('received', 'dropped', 'truncated', 'transport_queue_dropped_total')):
                raise ValueError('ESP capture counters decreased')
            state['last_status'] = counters
            if row['action'] == 'capture_complete':
                if row['active'] or counters['queued']:
                    raise ValueError('ESP completion is not inactive and drained')
                trace = row.get('association_trace')
                if trace is not None and (not isinstance(trace, dict) or trace.get('active') is not False
                                          or type(trace.get('queued')) is not int or trace['queued'] != 0
                                          or type(trace.get('session')) is not int
                                          or trace['session'] != state['session']):
                    raise ValueError('ESP association completion is not drained')
                if counters['truncated'] < state['truncated']:
                    raise ValueError('ESP truncation count contradicts observed packets')
                state['end'] = counters
        elif kind == 'collector_summary':
            if state['end'] is None or row.get('completion_received') is not True or row.get('rf_complete') != 'unknown':
                raise ValueError('ESP collector summary has no matching drained completion')
            if type(row.get('externally_stopped')) is not bool:
                raise ValueError('Invalid ESP collector stop flag')
            expected = _esp_indices(state['indexes'], state['end']['received'])
            expected.update(final_received=state['end']['received'], delivery_gap_assessment='indexed',
                capture_queue_dropped=state['end']['dropped'],
                transport_queue_dropped_start=state['end']['transport_queue_dropped_start'],
                transport_queue_dropped_total=state['end']['transport_queue_dropped_total'],
                transport_queue_dropped_delta=state['end']['transport_queue_dropped_total'] -
                                              state['end']['transport_queue_dropped_start'],
                association_rows=state['association_rows'], sources=dict(state['sources']))
            # Strict JSON equality also rejects bools masquerading as integer counters.
            if any(json.dumps(row.get(k), sort_keys=True) != json.dumps(v, sort_keys=True) for k, v in expected.items()):
                raise ValueError('ESP collector summary contradicts observed records')
            state['collector_dropped'] = _esp_counter(row, 'dropped')
            state['summary'] = expected
            state['closed'] = True
        else:
            raise ValueError('Unsupported ESP collector record kind')
    reports, warnings = [], set()
    for state in segments:
        end = state['end']
        counters = state['last_status']
        complete = state['summary'] is not None and not state['incomplete']
        loss = _esp_indices(state['indexes'], end['received'] if end else None)
        segment_warnings = set(state['warnings'])
        if not complete: segment_warnings.add('capture_incomplete')
        if state['start'] is None: segment_warnings.add('capture_not_started')
        if counters and counters['dropped']: segment_warnings.add('capture_dropped_records')
        if state.get('collector_dropped'): segment_warnings.add('collector_dropped_records')
        if counters and counters['transport_queue_dropped_total'] != counters['transport_queue_dropped_start']:
            segment_warnings.add('transport_dropped_records')
        if state['truncated'] or counters and counters['truncated']:
            segment_warnings.add('capture_truncated_records')
        if loss['prefix_index_gap'] or loss['interior_index_gaps'] or loss['tail_index_gap']:
            segment_warnings.add('capture_index_gaps')
        if loss['nonincreasing_indices']: segment_warnings.add('capture_nonincreasing_indices')
        if state['association_rows']: segment_warnings.add('association_records_not_decoded')
        warnings.update(segment_warnings)
        reports.append({'session': state['session'], 'request_id': state['request_id'],
            'complete': complete, 'recorded_local_tx': state['local_tx'], **loss,
            'capture_dropped': counters['dropped'] if counters else None,
            'collector_dropped': state.get('collector_dropped'),
            'transport_dropped': counters['transport_queue_dropped_total'] - counters['transport_queue_dropped_start'] if counters else None,
            'capture_truncated': counters['truncated'] if counters else None,
            'warnings': sorted(segment_warnings)})
    if not observations: warnings.add('no_scene_observations_does_not_mean_no_scenes')
    return {'format': 2, 'mode': 'offline_scene_inspection', 'input_format': 'esp-jsonl',
            'authentication_revalidated': False, 'rf_complete': 'unknown',
            'scene_backup_complete': False, 'programming_supported': False, 'recall_supported': False,
            'capture_complete': bool(reports) and all(r['complete'] for r in reports),
            'segments': reports, 'warnings': sorted(warnings), 'ignored_records': ignored,
            'counts': dict(Counter(o['kind'] for o in observations)), 'observations': observations}
