"""ESP device_identity.h evidence mapping; a catalog hint is not verified identity."""
from .protocol import ping_command, ping_match


def advertised_identity(identity):
    raw = bytes.fromhex(identity)
    if len(raw) != 16:
        raise ValueError('Expected full UUID')
    vendor, product = raw[6], int.from_bytes(raw[8:10],'big')
    model = {4:93,5:90,6:97,7:91,8:94,10:134,13:137,18:162,19:167}.get(product) if vendor==20 else None
    if (vendor, product) == (26, 4):
        model = 127
    return {'vendor':vendor,'product':product,'candidate_model':model,
            'source':'advertised_uuid','model_verified':False}


def paired_identity(payload, halo_context=False):
    if len(payload) not in (7,10) or payload[:2] != b'\x06\xff':
        raise ValueError('Invalid paired identity reply')
    result = {'source':'paired_ping','model_verified':False,'candidate_model':None}
    if len(payload)==7:
        result['signature'] = payload[3:].hex()
        if halo_context and payload[3:6]==b'\x01\x0a\x07' and payload[6] in (0,3):
            result['candidate_model'] = 91
    else:
        if payload[2:4]==b'\0\0' and payload[5:]==b'\x01\0\0\0\0':
            raise ValueError('Ping request echo is not identity')
        result.update(vendor=int.from_bytes(payload[7:9],'big'),product=payload[9],firmware=list(payload[3:6]))
        if (result['vendor'],result['product'])==(26,4):
            result['candidate_model'] = 127
    return result


def ping_token(store):
    with store.transaction():
        token = store.get('identity_token',1)+1  # Legacy claims used token1.
        if token>255:
            raise RuntimeError('Identity token space exhausted; no wrap')
        store.set('identity_token',token)
    return token


async def examine(runtime, target):
    r = runtime
    if type(target) is not int or target not in r.core.devices:
        raise ValueError('Unknown device')
    if r.operation.locked() or not r.mesh_ready or r.store.journals(pending=True):
        raise RuntimeError('Mesh busy or reconciliation pending')
    async with r.operation:
        token = ping_token(r.store)
        packet = await r.exchange(ping_command(target,token),lambda p:ping_match(p,target,token),timeout=5)
        # Context comes from a retained claim UUID, not the selected product label.
        claims = [j for j in r.store.journals() if j['kind']=='claim' and j['target']==target and j['status']=='verified']
        halo = any(advertised_identity(j['detail']['uuid'])['vendor']==20 for j in claims)
        result = paired_identity(packet.payload,halo)
        result.update(device_id=target,sequence=packet.sequence)
        r.store.set('identity/'+str(target),result)
        return result


def wired_reset_profile(identity, selected_product):
    """Narrow retained UUID catalog consistency, not manufacturer verification."""
    try:
        hint = advertised_identity(identity)
    except (TypeError, ValueError):
        return False
    return type(selected_product) is int and (
        selected_product == 127 and (hint['vendor'], hint['product']) == (26, 4)
        or selected_product == 91 and (hint['vendor'], hint['product']) == (20, 7))


def bootstrap_reset_profile(detail):
    """Retained first-RL4 bootstrap identity only; not a generic light reset policy."""
    if ((detail.get('direct_bootstrap') is not True and detail.get('fresh_mesh_first_claim') is not True) or type(detail.get('product_id')) is not int
            or detail['product_id'] != 93 or not isinstance(detail.get('bootstrap_address'), str)):
        return False
    try:
        identity = bytes.fromhex(detail['uuid'])
        address = bytes.fromhex(detail['bootstrap_address'].replace(':', ''))
        return (len(identity) == 16 and len(address) == 6 and identity[:6] == address[::-1]
                and advertised_identity(detail['uuid']).get('candidate_model') == 93)
    except (ValueError, TypeError, KeyError):
        return False


def pre_key_claim_profile(journal):
    """Only the initial first-RL4 request could have been submitted; no key transfer."""
    d = journal.get('detail', {})
    return (journal.get('kind') == 'claim' and journal.get('status') == 'uncertain'
            and bootstrap_reset_profile(d)
            and d.get('inventory_complete') is True
            and d.get('association_complete') is False
            and d.get('association_phase') == 'association'
            and type(d.get('submissions_attempted')) is int and d['submissions_attempted'] == 1
            and type(d.get('submissions_accepted')) is int and d['submissions_accepted'] == 1)


def _light_claim_receipt(journal, allowed, product=93):
    """Shared integrity checks for a retained supported light claim; ``allowed`` maps phase -> counts.

    Dispatch durably records state/count before each send, so a phase/count pair
    names exactly which association requests could have been submitted. Missing or
    legacy counters and partial transport acceptance are rejected.
    """
    if not isinstance(journal, dict) or not isinstance(journal.get('detail'), dict):
        return False
    d = journal['detail']
    target = journal.get('target')
    identity = d.get('uuid')
    attempted = d.get('submissions_attempted')
    if (journal.get('kind') != 'claim' or journal.get('status') != 'uncertain'
            or type(target) is not int or not 32896 <= target <= 65407 or target == 32898
            or type(d.get('product_id')) is not int or d['product_id'] != product
            or d.get('inventory_complete') is not True or d.get('association_complete') is not False
            or d.get('setup_requested') is not False
            or not isinstance(d.get('association_phase'), str) or d['association_phase'] not in allowed
            or type(attempted) is not int or attempted not in allowed[d['association_phase']]
            or type(d.get('submissions_accepted')) is not int
            or d['submissions_accepted'] != attempted
            or not isinstance(d.get('name'), str) or not 1 <= len(d['name']) <= 128
            or not isinstance(identity, str) or len(identity) != 32):
        return False
    try:
        from .association import uuid_hash
        raw = bytes.fromhex(identity)
        return (len(raw) == 16 and any(raw) and raw.hex() == identity
                and advertised_identity(identity).get('candidate_model') == product
                and type(d.get('uuid_hash')) is int and d['uuid_hash'] == uuid_hash(raw))
    except ValueError:
        return False


def rl4_public_key_claim_profile(journal):
    """A coherent RL4 receipt whose only possible writes precede address/key transfer.

    The association request is submission 1; public-key chunks are 2..7. This
    intentionally excludes every later phase. It is not physical identity, reset
    or successful-pairing evidence.
    """
    return _light_claim_receipt(journal, {'public_key': range(2, 8)})


def rl4_pre_network_key_claim_profile(journal):
    """A coherent RL4 receipt that went past the public key but never sent the network key.

    Submission 8 is the confirmation, 9 the random value and 10 the encrypted
    DeviceID (the proposed address). The network key is only ever submissions 11
    and 12, so a light in this state cannot hold this mesh's key. It may hold the
    proposed address, which recovery therefore retires permanently. Every other
    phase or count, including any network-key submission, is excluded.
    """
    return _light_claim_receipt(journal, {'confirm': (8,), 'random': (9,), 'device_id': (10,)})


def a19_public_key_claim_profile(journal):
    """A19 only: public-key submissions 2..7, never address or network-key TX.

    This is a dispatch receipt profile, not physical reset proof. Recovery also
    requires isolated ownership, verified prior retirement and a fresh exact UUID.
    """
    return _light_claim_receipt(journal, {'public_key': range(2, 8)}, product=134)


def a19_pre_address_claim_profile(journal):
    """Recognize a first-request timeout or an existing public-key-only receipt."""
    if a19_public_key_claim_profile(journal):
        return True
    if not _light_claim_receipt(journal, {'association': (1,)}, product=134):
        return False
    detail = journal['detail']
    timing = detail.get('association_timing')
    observed = detail.get('failure_observation')
    if not isinstance(timing, dict) or not isinstance(observed, dict):
        return False
    steps = timing.get('steps')
    if not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict):
        return False
    step = steps[0]
    return (detail.get('failure_code') == 'association_response_timeout'
            and step.get('state') == observed.get('state') == 'association'
            and type(step.get('chunk')) is int and step['chunk'] == 0
            and type(observed.get('expected_chunk')) is int and observed['expected_chunk'] == 0
            and type(step.get('write_finished_ms')) is int and step['write_finished_ms'] >= 0
            and observed.get('same_session') is True and observed.get('transport_ready') is True
            and observed.get('queue_overflow') is False)
