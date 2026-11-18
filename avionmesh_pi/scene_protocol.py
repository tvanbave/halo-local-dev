"""Pure scene framing; no transport or sequence allocation happens here.

The observed HALO save writes colour and brightness separately. ``scene_engine``
sends these frames on the live mesh. It never trusts a mutation reply code: a
write counts only after independent reads of both properties. Model 134 (A19)
at references 1 and 2 is physically observed; other colour lights and other
references remain experimental. See ``docs/SCENE-PROTOCOL.md``.
"""
from .protocol import integer

SCENE_NOUN = 0x22
MAX_RESEARCH_REFERENCE = 20  # Study bound from an old table report, not hardware capacity.
# Research flag for the offline planner in ``scenes.py`` only. The live
# ``scene_engine`` does not consult it.
PROGRAMMING_SUPPORTED = False


def _reference(value):
    return integer(value, 1, MAX_RESEARCH_REFERENCE, 'scene research reference')


def _mutation(target, token, verb, values):
    integer(target, 32896, 65407, 'scene light target')
    integer(token, 1, 255, 'scene correlation token')
    payload = bytes([verb, SCENE_NOUN, 0, 0, token]) + values
    assert len(payload) == 10
    return target.to_bytes(2, 'little') + b'\x73' + payload


def candidate_colour_insert(target, reference, kelvin, token):
    """Candidate full CCT row: unlike generic commands, requires all 5 value bytes."""
    reference = _reference(reference)
    integer(kelvin, 2700, 5000, 'scene colour temperature')
    return _mutation(target, token, 2,
                     bytes([reference, 2]) + kelvin.to_bytes(2, 'big') + b'\0')


def candidate_brightness_insert(target, reference, brightness, token):
    """Candidate brightness bank uses reference | 0x20; bank meaning is inferred."""
    reference = _reference(reference)
    integer(brightness, 0, 255, 'scene brightness')
    return _mutation(target, token, 2, bytes([reference | 0x20, brightness, 0, 0, 0]))


def candidate_delete(target, reference, token):
    """Observed app deletion shape; does NOT establish deletion of both properties."""
    return _mutation(target, token, 5, bytes([_reference(reference), 0, 0, 0, 0]))


def candidate_recall(reference, *, profile='halo_app'):
    """Recorded recall forms; the differing flag's semantics remain unknown.

    Normal activation retains the app's flag 1. Flag 3 is only for explicit
    comparison with captured keypad recalls, never an inferred speed setting.
    """
    if not isinstance(profile, str) or profile not in ('halo_app', 'keypad_capture'):
        raise ValueError('Unknown captured scene recall profile')
    flag = 1 if profile == 'halo_app' else 3
    reference = _reference(reference)
    payload = (bytes([0, SCENE_NOUN]) + reference.to_bytes(2, 'big')
               + bytes([0, reference, flag, 0, 0, 0]))
    return b'\0\0\x73' + payload


def describe_scene_message(packet):
    """Classify already-decoded bytes, preserving uncertain fields and addresses.

    Does not authenticate a packet, correlate a transaction, accept a result code,
    or prove table contents. Sequence may be None for a local send attempt.
    """
    p = packet.payload
    if not isinstance(p, bytes):
        raise ValueError('Invalid scene payload bytes')
    if packet.opcode != 0x73 or len(p) < 2:
        return None
    binding = len(p) == 8 and p[:3] == b'\x01\x30\0' and 1 <= p[3] <= 4 and p[6] == 0x20
    if p[1] != SCENE_NOUN and not binding:
        return None
    if len(p) > 23:
        raise ValueError('Invalid scene observation size')
    integer(packet.crypto_source, 1, 65535, 'observation source')
    integer(packet.mcp_address, 0, 65535, 'observation destination')
    result = {'kind': 'unclassified_scene_message', 'source': packet.crypto_source,
              'destination': packet.mcp_address, 'payload_hex': p.hex(),
              'verb_raw': p[0], 'noun_raw': p[1], 'semantics_verified': False}
    if binding:
        result.update(kind='keypad_scene_binding_candidate', slot=p[3],
                      target_reference_raw=int.from_bytes(p[4:6], 'big'),
                      flags_raw=p[6], value_raw=p[7])
    elif p[0] == 4 and len(p) == 5:
        result.update(kind='scene_count_candidate', header_byte_raw=p[2],
                      reported_count_raw=p[3], reported_capacity_raw=p[4])
    elif p[0] == 1 and len(p) == 8:
        result.update(kind='scene_row_candidate', header_byte_raw=p[2],
                      selector_raw=p[3], row_bytes_raw=p[4:].hex())
    elif p[0] in (2, 5) and len(p) == 4:
        result.update(kind='scene_mutation_reply_candidate', correlation_token_raw=p[2],
                      result_raw=p[3], result_meaning='unknown')
    elif (p[0] in (2, 5) and len(p) == 10 and p[2:4] == b'\0\0'
          and 32896 <= packet.mcp_address <= 65407):
        selector = p[5]
        if p[0] == 5 and 1 <= selector <= MAX_RESEARCH_REFERENCE and not any(p[6:]):
            result.update(kind='scene_delete_candidate', correlation_token_raw=p[4],
                          selector_raw=selector, scene_reference_candidate=selector,
                          deletion_extent='unknown')
        elif (p[0] == 2 and 1 <= selector <= MAX_RESEARCH_REFERENCE
              and p[6] == 2 and p[9] == 0 and 2700 <= int.from_bytes(p[7:9], 'big') <= 5000):
            result.update(kind='scene_colour_insert_candidate', correlation_token_raw=p[4],
                          selector_raw=selector, scene_reference_candidate=selector,
                          property_tag_raw=p[6], kelvin_candidate=int.from_bytes(p[7:9], 'big'))
        elif (p[0] == 2 and 0x21 <= selector <= 0x20 + MAX_RESEARCH_REFERENCE
              and not any(p[7:])):
            result.update(kind='scene_brightness_insert_candidate', correlation_token_raw=p[4],
                          selector_raw=selector, scene_reference_candidate=selector & 0x1f,
                          brightness_candidate=p[6], selector_bank_verified=False)
    elif (p[0] == 0 and len(p) == 10 and p[4] == 0 and p[6] in (1, 3)
          and not any(p[7:])):
        result.update(kind='scene_trigger_candidate',
                      header_reference_raw=int.from_bytes(p[2:4], 'big'),
                      value_bytes_raw=p[5:8].hex(), flags_raw=p[6])
    return result
