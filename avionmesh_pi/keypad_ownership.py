"""Owned HA button destinations, derived from retained, verified receipts.

A protected group name is never proof of ownership. Targets remain burned and
protected after retirement; only an exact retained reservation can be managed.
"""
import json


PARTIAL_SETUP_SCHEMA = 'keypad-setup-partial-resolution-v1'


def _complete_table(value):
    # Lazy import avoids the keypad -> ownership helper import cycle.
    from .keypad import snapshot
    try:
        rebuilt = snapshot(bytes.fromhex(value['count']), [bytes.fromhex(row['raw']) for row in value['slots']])
        if rebuilt != value:
            raise ValueError('Invalid table revision')
        return rebuilt
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Expected a complete internally consistent keypad table') from error


def classify_setup_table(detail, actual):
    """Validate retained setup intent and classify a twice-read mixed outcome.

    This pure helper does not itself establish freshness. Only the explicit
    recovery service may attach its result to a cancelled setup receipt.
    """
    if type(detail.get('product_id')) is not int or detail['product_id'] not in (126, 127):
        raise ValueError('Expected retained scene keypad model')
    before, desired, actual = (_complete_table(value) for value in (detail.get('before'), detail.get('desired'), actual))
    sets = {}
    for name in ('planned', 'attempted', 'verified'):
        slots = detail.get(name)
        if (not isinstance(slots, list) or any(type(slot) is not int or not 1 <= slot <= 4 for slot in slots)
                or len(set(slots)) != len(slots)):
            raise ValueError('Invalid retained setup slot history')
        sets[name] = set(slots)
    planned, attempted, verified = (sets[name] for name in ('planned', 'attempted', 'verified'))
    if (not planned or not attempted or not verified <= attempted <= planned
            or detail['planned'] != sorted(planned)
            or detail['attempted'] != detail['planned'][:len(attempted)]):
        raise ValueError('Inconsistent setup attempt history')
    stages = detail.get('stages')
    allowed = [str(slot) + '/' + kind for slot in detail['attempted'] for kind in ('primary', 'secondary')]
    if (not isinstance(stages, list) or any(stage not in allowed for stage in stages)
            or len(set(stages)) != len(stages) or stages != [stage for stage in allowed if stage in stages]
            or any(str(slot) + '/secondary' in stages and str(slot) + '/primary' not in stages for slot in attempted)):
        raise ValueError('Inconsistent setup write stage history')
    reservations = detail.get('reservations')
    if not isinstance(reservations, list) or len(reservations) != len(planned):
        raise ValueError('Incomplete owned setup reservations')
    destinations = []
    for slot in range(1, 5):
        old, new = before['slots'][slot - 1], desired['slots'][slot - 1]
        if slot in planned:
            if (old['target'] != 65535 or old['flags'] or old['level'] or new['flags'] != 8
                    or new['level'] != 255 or not 256 <= new['target'] <= 24575
                    or any(row['target'] == new['target'] for row in before['slots'])):
                raise ValueError('Setup must preserve occupied slots and own new HA destinations')
            destinations.append(new['target'])
        elif new != old:
            raise ValueError('Unplanned setup proposal changed an assignment')
    if (len(set(destinations)) != len(destinations)
            or any(not isinstance(group, dict) or group.get('protected') is not True
                   or group.get('exposed') is not False or group.get('members') != [] for group in reservations)
            or {group.get('group_id') for group in reservations} != set(destinations)):
        raise ValueError('Setup reservations do not match the intended HA slots')
    applied, unchanged, unattempted = [], [], []
    for slot in range(1, 5):
        old, new, observed = (value['slots'][slot - 1] for value in (before, desired, actual))
        if slot not in planned:
            if observed != old:
                raise ValueError('Unplanned assignment changed')
        elif slot not in attempted:
            if observed != old:
                raise ValueError('Unattempted assignment changed externally')
            unattempted.append(slot)
        elif observed == new:
            if str(slot) + '/primary' not in stages:
                raise ValueError('No recorded write stage for observed assignment')
            applied.append(slot)
        elif observed == old and slot not in verified:
            unchanged.append(slot)
        else:
            raise ValueError('Verified assignment regressed or unknown assignment observed')
    return {'applied_slots': applied, 'unchanged_attempted_slots': unchanged, 'unattempted_slots': unattempted}


def partial_setup_slots(journal):
    """Only a strict, resolved readback receipt can retain partial HA ownership."""
    if journal.get('kind') != 'keypad_setup' or journal.get('status') != 'cancelled':
        return []
    detail = journal.get('detail', {})
    proof = detail.get('partial_resolution')
    if (not isinstance(proof, dict) or set(proof) != {'schema', 'table', 'readback_rounds',
            'assignment_read_back', 'write_retried', 'applied_slots', 'unchanged_attempted_slots', 'unattempted_slots'}
            or proof.get('schema') != PARTIAL_SETUP_SCHEMA or type(proof.get('readback_rounds')) is not int
            or proof['readback_rounds'] != 2 or proof.get('assignment_read_back') is not True
            or proof.get('write_retried') is not False):
        return []
    try:
        classified = classify_setup_table(detail, proof['table'])
    except (KeyError, TypeError, ValueError):
        return []
    return classified['applied_slots'] if all(proof.get(key) == value for key, value in classified.items()) else []


def owned_assignments(owner, journals, groups, *, retired=(), sources=None, bindings=None):
    if owner in retired:
        return {}
    owned = {}
    for journal in journals:
        if journal['target'] != owner:
            continue
        resolved_slots = partial_setup_slots(journal)
        if journal['status'] != 'verified' and not resolved_slots:
            continue
        detail = journal.get('detail', {})
        if journal['kind'] == 'keypad_setup':
            for slot in resolved_slots if journal['status'] == 'cancelled' else detail.get('planned', []):
                row = detail['desired']['slots'][slot - 1]
                reservation = next((g for g in detail.get('reservations', [])
                                    if g['group_id'] == row['target']), None)
                if reservation and row['flags'] == 8 and row['level'] == 255:
                    owned[row['target']] = {'slot': slot, 'reservation': reservation}
        elif journal['kind'] == 'keypad_edit' and detail.get('managed'):
            managed = detail['managed']
            old, new = managed.get('old'), managed.get('new')
            if old:
                owned.pop(old['group_id'], None)
            if new:
                owned[new['group_id']] = {'slot': detail['slot'], 'reservation': new}
    for target, entry in list(owned.items()):
        reservation = entry['reservation']
        intact = (reservation in groups and reservation.get('protected') is True
                  and reservation.get('members') == [] and reservation.get('exposed') is False)
        safe = all(source == owner for source in (sources or {}).get(str(target), []))
        exclusive = all(source == owner and row['slot'] == entry['slot']
                        for source, table in (bindings or {}).items() for row in table['slots']
                        if row['target'] == target)
        if not intact or not safe or not exclusive:
            del owned[target]
    return owned


def stored_tables(store):
    """Include stale tables: losing freshness is not permission to reuse a target."""
    return {target: json.loads(raw) for target, raw in store.db.execute('SELECT target,snapshot FROM bindings')}


def owned_targets(store, owner):
    journals, groups = store.journals(), store.inventory().get('groups', [])
    options = dict(retired=store.get('retired_addresses', []), sources=store.get('target_sources', {}))
    if not owned_assignments(owner, journals, groups, **options):
        return {}
    return owned_assignments(owner, journals, groups, bindings=stored_tables(store), **options)


def used_targets(store, table):
    used = set(store.get('known_targets', [])) | {24566}
    used.update(g['group_id'] for g in store.inventory()['groups'])
    used.update(int(target) for target in store.get('target_sources', {}))
    used.update(row['target'] for value in list(stored_tables(store).values()) + [table]
                for row in value['slots'])
    return used


def reservation(owner, slot, target):
    return {'group_id': target, 'name': f'HA keypad target: {owner}/{slot}',
            'members': [], 'exposed': False, 'protected': True}


def validate_managed(store, owner, detail):
    """Recheck durable intent before every setter and before accepting readback."""
    managed = detail.get('managed')
    if not managed:
        return
    slot = detail['slot']
    owned = owned_targets(store, owner)
    for key in ('old', 'new'):
        group = managed.get(key)
        if not group:
            continue
        target = group['group_id']
        if (group not in store.inventory()['groups'] or group.get('protected') is not True
                or group.get('members') != [] or group.get('exposed') is not False
                or not store.owned_target_safe(target, owner)
                or any(source != owner or row['slot'] != slot
                       for source, table in stored_tables(store).items() for row in table['slots']
                       if row['target'] == target)):
            raise ValueError('Owned HA reservation changed or has a competing reference')
        if key == 'old' and owned.get(target) != {'slot': slot, 'reservation': group}:
            raise ValueError('Protected assignment is not owned by this button')
        row = detail['before' if key == 'old' else 'desired']['slots'][slot - 1]
        if row['target'] != target or row['flags'] != 8 or row['level'] != 255:
            raise ValueError('Owned HA intent does not match the retained table')
