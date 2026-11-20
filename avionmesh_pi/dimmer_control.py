"""Reviewed dimmer mode changes, with permanent private-target ownership.

Only an authenticated current native command supplies the original destination.
No stored assignment read, clear, setter retry, or automatic rollback is implied.
"""
import asyncio
import copy
import hashlib
import json
import math

from .association import uuid_hash
from .bridge import LIGHTS
from .cloud_provenance import device_source
from .errors import OperationNotStarted
from .keypad_ownership import stored_tables
from .lifecycle import reload_inventory
from .protocol import Packet, decode_controller, integer

SCHEMA = 'dimmer-control-v1'
KINDS = ('accessory_setup', 'accessory_native_setup', 'anyplace_setup', 'dimmer_edit')


def _binding_snapshot(store):
    # SQLite addresses are integers; retained JSON object keys are strings.
    return {str(target): table for target, table in stored_tables(store).items()}


def _history_snapshot(store, target, *, exclude=None):
    # Fingerprint retained receipts rather than embedding previous snapshots.
    # Each new journal stays linear in history instead of recursively copying it.
    return [{'id': j['id'], 'kind': j['kind'], 'status': j['status'],
             'detail_sha256': hashlib.sha256(json.dumps(j['detail'], sort_keys=True,
                 separators=(',', ':')).encode()).hexdigest()}
            for j in store.journals() if j['target'] == target and j['kind'] in KINDS and j['id'] != exclude]


def retained_dimmer_claim(target, product_id, journals, *, retired_claims=(), retired_addresses=()):
    integer(target, 32896, 65407, 'controller')
    if product_id not in (81, 91) or target == 32898 or target in retired_addresses:
        raise ValueError('Expected retained supported dimmer identity')
    claims = [j for j in journals if j['kind'] == 'claim' and j['target'] == target
              and j['status'] == 'verified' and j['id'] not in retired_claims]
    if len(claims) != 1:
        raise ValueError('One verified retained dimmer claim required')
    claim = claims[0]
    d = claim.get('detail', {})
    identity = d.get('uuid')
    try:
        raw = bytes.fromhex(identity) if isinstance(identity, str) and len(identity) == 32 else b''
    except ValueError:
        raw = b''
    if (d.get('product_id') != product_id or d.get('association_complete') is not True
            or len(raw) != 16 or not any(raw) or d.get('uuid_hash') != uuid_hash(raw)):
        raise ValueError('Complete exact dimmer association identity required')
    if any(j['kind'] == 'claim' and j['id'] != claim['id'] and j['id'] not in retired_claims
           and (j.get('detail', {}).get('uuid') == identity
                or j.get('detail', {}).get('uuid_hash') == d['uuid_hash']) for j in journals):
        raise ValueError('Dimmer identity has another retained claim')
    return claim


def imported_dimmer_source(store, target, product, journals):
    """Only an actual reviewed import may authorize an in-place assignment."""
    if (product != 91 or str(target) not in store.get('cloud_device_sources', {})
            or any(j['kind'] == 'claim' and j['target'] == target for j in journals)):
        return None
    try:
        source = device_source(store, target)
    except ValueError:
        return None
    if (source.get('authority_type') == 'reviewed_cloud_inventory' and source.get('product_id') == product
            and source.get('location_id') and target not in store.get('retired_addresses', [])):
        return source
    return None


def dimmer_identity(target, product, journals, retired_claims, retired_addresses, cloud_source):
    if cloud_source is not None:
        integer(target, 32896, 65407, 'controller')
        if (product != 91 or target in retired_addresses or cloud_source.get('device_id') != target
                or cloud_source.get('product_id') != product
                or any(j['kind'] == 'claim' and j['target'] == target for j in journals)):
            raise ValueError('Imported dimmer identity changed')
        return {'cloud_source': cloud_source}
    claim = retained_dimmer_claim(target, product, journals, retired_claims=retired_claims,
                                 retired_addresses=retired_addresses)
    return {'claim': claim['id'], 'claim_receipt': claim}


def _matches_identity(detail, identity):
    return {k: detail[k] for k in ('claim', 'claim_receipt', 'cloud_source') if k in detail} == identity


def _forward(sequence, previous):
    return (type(sequence) is int and 0 <= sequence <= 0xffffff and
            (previous is None or type(previous) is int and 0 <= previous <= 0xffffff
             and 0 < ((sequence-previous) & 0xffffff) < 0x800000))


def _observed(detail, target, product_id, group):
    evidence = detail.get('observed', {})
    sequence = evidence.get('sequence')
    try:
        payload = bytes.fromhex(evidence.get('payload', ''))
    except (ValueError, TypeError):
        return False
    if not _forward(sequence, None):
        return False
    event = decode_controller(Packet(sequence, target, 0x8000, 115, payload), product_id)
    return bool(event and 'brightness' in event and event.get('target') == group)


def _referenced_groups(journal):
    d = journal.get('detail', {})
    groups = [d.get('reservation')] + d.get('reservations', [])
    managed = d.get('managed', {})
    if isinstance(managed, dict):
        groups += [managed.get('old'), managed.get('new')]
    return [g['group_id'] for g in groups if isinstance(g, dict) and type(g.get('group_id')) is int]


def _private_safe(group, owner, journals, groups, sources=None, bindings=None):
    destination = group.get('group_id')
    return (type(destination) is int and 256 <= destination <= 24575
        and group.get('protected') is True and group.get('members') == []
        and group.get('exposed') is False
        and [g for g in groups if g.get('group_id') == destination] == [group]
        and all(source == owner for source in (sources or {}).get(str(destination), []))
        and not any(row.get('target') == destination
                    for table in (bindings or {}).values() for row in table.get('slots', []))
        and not any(j['target'] != owner and destination in _referenced_groups(j) for j in journals))


def owned_dimmer_targets(target, product_id, journals, groups, *, retired_claims=(),
                         retired_addresses=(), target_sources=None, bindings=None, cloud_source=None):
    """All safely retained private targets, including those left by a native switch."""
    try:
        identity = dimmer_identity(target, product_id, journals, retired_claims, retired_addresses, cloud_source)
    except ValueError:
        return {}
    result = {}
    for rank, journal in enumerate(journals):
        if journal['target'] != target or journal['status'] != 'verified':
            continue
        d = journal.get('detail', {})
        initial = ((journal['kind'] == 'accessory_setup' and product_id == 91)
                   or (journal['kind'] == 'anyplace_setup' and product_id == 81
                       and d.get('product_id') == 81))
        initial = (initial and 'claim' in identity and 'cloud_source' not in d and d.get('claim') == identity['claim']
                   and d.get('assignment_attempted') is True
                   and ('claim_receipt' not in d or d['claim_receipt'] == identity['claim_receipt'])
                   and _forward(d.get('observed', {}).get('sequence'),
                       d.get('wake' if product_id == 81 else 'baseline', {}).get('sequence')))
        if initial and type(d.get('wake' if product_id == 81 else 'baseline', {}).get('sequence')) is not int:
            initial = False
        managed = (journal['kind'] == 'dimmer_edit' and d.get('control_schema') == SCHEMA
                   and d.get('mode') == 'ha' and d.get('product_id') == product_id
                   and _matches_identity(d, identity) and d.get('assignment_attempted') is True
                   and d.get('assignment_read_back') is False and not d.get('dispatch_not_started')
                   and type(d.get('wake', d.get('before', {})).get('sequence')) is int
                   and _forward(d.get('observed', {}).get('sequence'),
                                d.get('wake', d.get('before', {})).get('sequence')))
        reservation = d.get('reservation', {})
        group = reservation.get('group_id')
        if (not (initial or managed)
                or not _observed(d, target, product_id, group)
                or not _private_safe(reservation, target, journals, groups, target_sources, bindings)):
            continue
        result[group] = {'reservation': copy.deepcopy(reservation), 'transaction': journal['id'], 'rank': rank}
        if 'claim' in identity:
            result[group]['claim'] = identity['claim']
    return result


def dimmer_control_status(target, product_id, journals, groups, *, retired_claims=(),
                          retired_addresses=(), target_sources=None, bindings=None, cloud_source=None):
    """Latest accepted destination; reports neither physical tests nor readback."""
    result = {'state': 'not_configured', 'configured': False, 'mode': None, 'group': None,
              'transaction': None, 'assignment_read_back': False}
    history = [j for j in journals if j['target'] == target and j['kind'] in KINDS
               and j['status'] != 'cancelled']
    if not history:
        return result
    result['state'] = 'needs_review'
    latest = history[-1]
    if latest['status'] != 'verified':
        return result
    try:
        identity = dimmer_identity(target, product_id, journals, retired_claims, retired_addresses, cloud_source)
    except ValueError:
        return result
    d, kind = latest.get('detail', {}), latest['kind']
    if 'cloud_source' in identity and (kind != 'dimmer_edit' or d.get('control_schema') != SCHEMA or d.get('mode') != 'ha'):
        return result
    reservation = d.get('reservation', {})
    group = reservation.get('group_id')
    if (type(group) is not int or not 256 <= group <= 24575
            or not _observed(d, target, product_id, group)):
        return result
    if kind == 'dimmer_edit' and d.get('control_schema') == SCHEMA:
        mode = d.get('mode')
        if (mode not in ('ha', 'native') or d.get('product_id') != product_id
                or not _matches_identity(d, identity)
                or d.get('assignment_attempted') is not True or d.get('assignment_read_back') is not False
                or d.get('dispatch_not_started')
                or type(d.get('wake', d.get('before', {})).get('sequence')) is not int
                or not _forward(d.get('observed', {}).get('sequence'),
                    d.get('wake', d.get('before', {})).get('sequence'))):
            return result
    elif kind in ('accessory_setup', 'anyplace_setup'):
        mode = 'ha'
    elif kind in ('accessory_native_setup', 'dimmer_edit'):
        mode = 'native'
        if (d.get('product_id') != product_id
                or type(d.get('baseline' if kind == 'accessory_native_setup' else 'before', {}).get('sequence')) is not int
                or not _forward(d.get('observed', {}).get('sequence'),
                    d.get('baseline' if kind == 'accessory_native_setup' else 'before', {}).get('sequence'))):
            return result
        if kind == 'accessory_native_setup' and (product_id != 91 or not _matches_identity(d, identity) or d.get('mode') != 'native'
                or d.get('assignment_attempted') is not True or d.get('assignment_read_back') is not False):
            return result
    else:
        return result
    if mode == 'ha':
        owned = owned_dimmer_targets(target, product_id, journals, groups,
            retired_claims=retired_claims, retired_addresses=retired_addresses,
            target_sources=target_sources, bindings=bindings, cloud_source=cloud_source)
        if group not in owned:
            return result
    else:
        matches = [g for g in groups if g.get('group_id') == group]
        if reservation.get('protected') is not False or len(matches) != 1 or matches[0].get('protected') is not False:
            return result
    result.update(state='configured', configured=True, mode=mode, group=group, transaction=latest['id'])
    return result


class ManagedDimmerConfiguration:
    def __init__(self, editor):
        self.editor, self.runtime = editor, editor.runtime

    def _identity(self, target, product):
        r = self.runtime
        journals = r.store.journals()
        return dimmer_identity(target, product, journals, r.store.get('retired_claims', []),
            r.store.get('retired_addresses', []), imported_dimmer_source(r.store, target, product, journals))

    def _owned(self, target, product, identity):
        r = self.runtime
        return owned_dimmer_targets(target, product, r.store.journals(), r.store.inventory()['groups'],
            retired_claims=r.store.get('retired_claims', []),
            retired_addresses=r.store.get('retired_addresses', []),
            target_sources=r.store.get('target_sources', {}), bindings=stored_tables(r.store),
            cloud_source=identity.get('cloud_source'))

    def _members(self, destination):
        r = self.runtime
        if destination.get('protected') is not False or not destination.get('members'):
            raise ValueError('Expected ordinary nonempty lighting group')
        members = []
        devices = r.store.inventory()['devices']
        for target in destination['members']:
            rows = [d for d in devices if d['device_id'] == target]
            device = r.core.devices.get(target)
            if (len(rows) != 1 or rows[0]['product_id'] not in LIGHTS or not device
                    or device.product_id != rows[0]['product_id']
                    or target in r.store.get('retired_addresses', [])):
                raise ValueError('Native group contains an unknown or non-light member')
            members.append({'device_id': target, 'product_id': device.product_id})
        if len({m['device_id'] for m in members}) != len(members):
            raise ValueError('Duplicate group member')
        return members

    def _source_group(self, address, imported):
        r = self.runtime
        if 256 <= address <= 24575:
            groups = [g for g in r.store.inventory()['groups'] if g['group_id'] == address]
            if len(groups) == 1:
                return groups[0]
            if not groups and imported and address != 24566 and not any(
                    address in _referenced_groups(j) for j in r.store.journals()):
                return None  # HALO may omit this native target from its group list.
        elif imported and address not in r.store.get('retired_addresses', []):
            device = r.core.devices.get(address)
            if device and device.product_id in LIGHTS:
                return None
        raise ValueError('Observed source must be a supported native target')

    def _plan(self, target, mode, group):
        r = self.runtime
        integer(target, 32896, 65407, 'controller')
        if mode not in ('ha', 'native') or mode == 'ha' and group is not None:
            raise ValueError('Expected native group or automatic owned HA destination')
        if mode == 'native':
            integer(group, 256, 24575, 'group')
        device = r.core.devices.get(target)
        product = device.product_id if device else None
        flag = 'anyplace_editor' if product == 81 else 'dimmer_editor'
        if product not in (81, 91) or r.capabilities.get(flag) is not True or r.capabilities.get('inventory_complete') is not True:
            raise ValueError('Managed dimmer configuration disabled or inventory incomplete')
        if r.operation.locked() or not r.mesh_ready or not r.transport.ready or r.store.journals(pending=True):
            raise RuntimeError('Mesh busy, offline or reconciliation pending')
        identity = self._identity(target, product)
        imported = 'cloud_source' in identity
        if imported and (mode != 'ha' or not (r.mqtt_enabled and r.mqtt_ready)):
            raise ValueError('Imported dimmer conversion requires Home Assistant mode and connected MQTT')
        inventory = r.store.inventory()
        devices = [d for d in inventory['devices'] if d['device_id'] == target]
        if len(devices) != 1 or devices[0]['product_id'] != product:
            raise ValueError('Dimmer inventory identity changed')
        baseline = r.native_observations.get(target)
        if (not baseline or not 0 <= r.clock()-baseline[1] <= 15000
                or type(baseline[0].get('target')) is not int
                or (imported and r.native_sequences.get(target) != baseline[0].get('sequence'))
                or not _forward(baseline[0].get('sequence'), None)
                or type(baseline[0].get('target_generation')) is not int):
            raise ValueError('Fresh current native target observation required')
        source = self._source_group(baseline[0]['target'], imported)
        owned = self._owned(target, product, identity)
        if source is not None and source.get('protected') is not False and source['group_id'] not in owned:
            raise ValueError('Protected source is not safely owned by this dimmer')
        newly_reserved, members = False, []
        if mode == 'native':
            destinations = [g for g in inventory['groups'] if g['group_id'] == group]
            if len(destinations) != 1:
                raise ValueError('Expected known native lighting group')
            destination = destinations[0]
            members = self._members(destination)
        elif owned:
            destination = max(owned.values(), key=lambda item: item['rank'])['reservation']
        else:
            used = (set(r.store.get('known_targets', [])) | {g['group_id'] for g in inventory['groups']}
                    | {int(t) for t in r.store.get('target_sources', {})} | {24566})
            used.update(row['target'] for table in stored_tables(r.store).values() for row in table['slots'])
            group = next((i for i in range(24575, 255, -1) if i not in used), None)
            if group is None or len(inventory['groups']) >= 128:
                raise ValueError('Private target capacity exhausted')
            destination = {'group_id': group, 'name': f'HA dimmer target: {target}',
                           'members': [], 'exposed': False, 'protected': True}
            newly_reserved = True
        if destination['group_id'] == baseline[0]['target']:
            raise ValueError('Dimmer already uses this destination')
        plan = {'target': target, 'product_id': product, 'mode': mode, **identity,
                'source_group': copy.deepcopy(source), 'reservation': copy.deepcopy(destination),
                'destination_members': members, 'new_reservation': newly_reserved,
                'baseline': {'target': baseline[0]['target'], 'observed_ms': baseline[1],
                             'target_generation': baseline[0]['target_generation']},
                'before': copy.deepcopy(baseline[0]), 'generation': r.generation,
                'prior': _history_snapshot(r.store, target),
                'bindings': _binding_snapshot(r.store)}
        revision = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return plan, revision

    def preview(self, target, mode, group=None):
        plan, revision = self._plan(target, mode, group)
        return {'target': target, 'mode': mode, 'group': plan['reservation']['group_id'],
                'source_group': plan['baseline']['target'],
                'source_mode': 'ha' if plan['source_group'] and plan['source_group']['protected'] else 'native',
                'baseline': plan['baseline'], 'revision': revision,
                'valid_for_ms': 15000-(self.runtime.clock()-plan['baseline']['observed_ms']),
                'reuses_ha_target': mode == 'ha' and not plan['new_reservation'],
                'assignment_read_back': False}

    def _authority(self, identifier, detail, *, statuses=('prepared', 'attempted', 'uncertain')):
        r = self.runtime
        current = self.editor.receipt(identifier)
        target, product = current['target'], detail['product_id']
        identity = self._identity(target, product)
        if (current['kind'] != 'dimmer_edit' or current['status'] not in statuses
                or current['detail'] != detail or detail.get('control_schema') != SCHEMA
                or not _matches_identity(detail, identity) or ('cloud_source' in identity and detail['mode'] != 'ha')
                or any(j['id'] != identifier for j in r.store.journals(pending=True))):
            raise ValueError('Dimmer configuration receipt or claim changed')
        rows = [d for d in r.store.inventory()['devices'] if d['device_id'] == target]
        device = r.core.devices.get(target)
        if len(rows) != 1 or rows[0]['product_id'] != product or not device or device.product_id != product:
            raise ValueError('Dimmer inventory identity changed')
        prior = _history_snapshot(r.store, target, exclude=identifier)
        if prior != detail['prior'] or _binding_snapshot(r.store) != detail['bindings']:
            raise ValueError('Dimmer history or retained keypad references changed')
        groups = r.store.inventory()['groups']
        if self._source_group(detail['baseline']['target'], 'cloud_source' in identity) != detail['source_group']:
            raise ValueError('Reviewed source changed')
        for name in ('source_group', 'reservation'):
            group = detail[name]
            if group is None and name == 'source_group':
                continue
            if [g for g in groups if g['group_id'] == group['group_id']] != [group]:
                raise ValueError('Reviewed dimmer destination changed')
            if group['protected'] and not _private_safe(group, target, r.store.journals(), groups,
                    r.store.get('target_sources', {}), stored_tables(r.store)):
                raise ValueError('Private target ownership changed')
        owned = self._owned(target, product, identity)
        if detail['source_group'] and detail['source_group']['protected'] and detail['source_group']['group_id'] not in owned:
            raise ValueError('Protected source is no longer owned')
        if detail['mode'] == 'ha':
            if not detail['new_reservation'] and detail['reservation']['group_id'] not in owned:
                raise ValueError('Reused HA target is no longer owned')
        elif self._members(detail['reservation']) != detail['destination_members']:
            raise ValueError('Native destination membership changed')
        return current

    @staticmethod
    def _timeout(timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError('Expected timeout in (0, 120] seconds')

    async def _observe(self, target, product, group, floor, generation, deadline, *, command=None, admission=None):
        r = self.runtime
        future = asyncio.get_running_loop().create_future()
        since = r.clock()
        admitted_at = since if command is None else None
        contradicted = False
        def observer(packet, when):
            nonlocal contradicted
            if (admitted_at is None or generation != r.generation or packet.crypto_source != target
                    or not admitted_at <= when <= r.clock() or r.clock() >= deadline
                    or not _forward(packet.sequence, floor)):
                return
            event = decode_controller(packet, product)
            if not event or 'brightness' not in event:
                return
            if event.get('target') != group:
                contradicted = True
                if not future.done():
                    future.set_exception(RuntimeError('Conflicting native destination observed'))
            elif not future.done():
                future.set_result({'sequence': packet.sequence, 'payload': packet.payload.hex(), 'received_ms': when})
        def admitted():
            nonlocal admitted_at
            admission()
            admitted_at = r.clock()
        waiter = (lambda packet: False, future, since)
        r.packet_observers.append(observer)
        r.waiters.append(waiter)
        try:
            if command is not None:
                await r.send(command, generation=generation, admission=admitted)
            remaining = (deadline-r.clock())/1000
            if remaining <= 0:
                raise asyncio.TimeoutError('Dimmer observation deadline expired')
            observed = await asyncio.wait_for(future, remaining)
            if generation != r.generation or not r.mesh_ready or not r.transport.ready:
                raise RuntimeError('Dimmer observation crossed a session boundary')
            if r.clock() >= deadline:
                raise asyncio.TimeoutError('Dimmer observation deadline expired')
            if contradicted:
                raise RuntimeError('Conflicting native destination observed')
            return observed
        finally:
            r.waiters.remove(waiter)
            r.packet_observers.remove(observer)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def configure(self, target, mode, group=None, *, expected_baseline, expected_revision, timeout=120):
        self._timeout(timeout)
        r = self.runtime
        try:
            plan, revision = self._plan(target, mode, group)
            if (not isinstance(expected_revision, str) or expected_revision != revision
                    or not isinstance(expected_baseline, dict) or expected_baseline != plan['baseline']):
                raise ValueError('Dimmer review changed or expired; review again')
        except (ValueError, RuntimeError) as error:
            code = ('mesh_offline' if not r.mesh_ready or not r.transport.ready else
                    'mesh_busy' if r.operation.locked() else
                    'pending_operation' if r.store.journals(pending=True) else 'inventory_changed')
            raise OperationNotStarted(code) from error
        async with r.operation:
            with r.store.transaction():
                token = r.store.get('dimmer_token', 0)+1
                if type(token) is not int or token > 255:
                    raise ValueError('Dimmer token capacity exhausted')
                r.store.set('dimmer_token', token)
            detail = copy.deepcopy(plan)
            detail.update(control_schema=SCHEMA, token=token,
                          assignment_read_back=False, assignment_attempted=False)
            data = copy.deepcopy(r.store.inventory())
            if plan['new_reservation']:
                data['groups'].append(copy.deepcopy(plan['reservation']))
            identifier = r.store.begin('dimmer_edit', target, detail, inventory=data,
                                       reserved_targets=[plan['reservation']['group_id']])
            generation, deadline = r.generation, r.clock()+timeout*1000
            dispatched = False
            try:
                r.emit(r.discovery())
                wake_at, floor = None, plan['before']['sequence']
                if plan['product_id'] == 81:
                    baseline = (plan['before'], plan['baseline']['observed_ms'])
                    wake_at = await self.editor.wait_wake(target, baseline, generation, timeout)
                    floor = self.editor.wake_sequences[target][1]
                    detail['wake'] = {'sequence': floor, 'received_ms': wake_at}
                    self.editor.save(identifier, detail, 'prepared')

                def validate():
                    if (generation != r.generation or not r.mesh_ready or not r.transport.ready
                            or r.clock() >= deadline):
                        raise RuntimeError('Dimmer configuration session changed or expired')
                    self._authority(identifier, detail)
                    flag = 'anyplace_editor' if plan['product_id'] == 81 else 'dimmer_editor'
                    if r.capabilities.get(flag) is not True or r.capabilities.get('inventory_complete') is not True:
                        raise ValueError('Dimmer configuration capability changed')
                    if 'cloud_source' in detail and not (r.mqtt_enabled and r.mqtt_ready):
                        raise ValueError('MQTT disconnected before assignment')
                    latest = r.native_observations.get(target)
                    if (not latest or ('cloud_source' in detail and latest[0]['sequence'] != r.native_sequences.get(target))
                            or latest[0]['target'] != plan['baseline']['target']
                            or latest[0].get('target_generation') != plan['baseline']['target_generation']
                            or not 0 <= r.clock()-plan['baseline']['observed_ms'] <= 15000):
                        raise ValueError('Reviewed native target changed or expired')
                    if plan['product_id'] == 81 and (not 0 <= r.clock()-wake_at <= 250
                            or _forward(r.native_sequences.get(target), floor)):
                        raise ValueError('Wake dispatch window changed or expired')

                validate()
                detail['assignment_attempted'] = True
                self.editor.save(identifier, detail, 'attempted')
                def admit():
                    nonlocal dispatched
                    validate()
                    dispatched = True
                command = (target.to_bytes(2, 'little') + b'\x73\0\x1b\0\0' + bytes([token])
                           + plan['reservation']['group_id'].to_bytes(2, 'big') + b'\x01\0\0')
                observed = await self._observe(target, plan['product_id'], plan['reservation']['group_id'],
                    floor, generation, deadline, command=command, admission=admit)
                self._authority(identifier, detail)
                detail['observed'] = observed
                self.editor.save(identifier, detail, 'uncertain')
                self.editor.observed[identifier] = r.clock()
                r.emit(r.discovery())
                return {'transaction': identifier, 'mode': mode, 'group': plan['reservation']['group_id'],
                        'desired_behavior_observed': True, 'acceptance_required': True,
                        'assignment_read_back': False}
            except BaseException as error:
                operation_result = {'transaction': identifier, 'mode': mode,
                                    'group': plan['reservation']['group_id']}
                error.operation_result = operation_result
                try:
                    try:
                        if not dispatched:
                            detail['dispatch_not_started'] = True
                        self.editor.save(identifier, detail, 'uncertain' if dispatched else 'cancelled')
                    finally:
                        if dispatched:
                            try:
                                await r.transport.disconnect()
                            finally:
                                r.mesh_ready = False
                                r.session()
                except BaseException as cleanup_error:
                    cleanup_error.operation_result = operation_result
                    raise
                if not dispatched and isinstance(error, Exception):
                    code = 'anyplace_wake_timeout' if isinstance(error, asyncio.TimeoutError) and wake_at is None and plan['product_id'] == 81 else 'inventory_changed'
                    raise OperationNotStarted(code) from error
                raise

    async def reconcile(self, identifier, *, timeout=120):
        self._timeout(timeout)
        r = self.runtime
        j = self.editor.receipt(identifier)
        detail = copy.deepcopy(j['detail'])
        if (j['kind'] != 'dimmer_edit' or j['status'] != 'uncertain'
                or detail.get('control_schema') != SCHEMA or detail.get('assignment_attempted') is not True
                or detail.get('dispatch_not_started')):
            raise ValueError('Expected attempted managed dimmer configuration')
        if r.operation.locked() or not r.mesh_ready or not r.transport.ready:
            raise RuntimeError('Mesh busy or offline')
        self._authority(identifier, detail, statuses=('uncertain',))
        self.editor.observed.pop(identifier, None)
        async with r.operation:
            target, generation, deadline = j['target'], r.generation, r.clock()+timeout*1000
            floor = detail.get('observed', detail.get('wake', detail['before']))['sequence']
            latest = r.native_sequences.get(target)
            if _forward(latest, floor):
                floor = latest
            if detail['product_id'] == 81:
                baseline = ({'sequence': floor}, r.clock())
                await self.editor.wait_wake(target, baseline, generation, timeout)
                floor = self.editor.wake_sequences[target][1]
            observed = await self._observe(target, detail['product_id'], detail['reservation']['group_id'],
                                           floor, generation, deadline)
            self._authority(identifier, detail, statuses=('uncertain',))
            detail['observed'] = observed
            self.editor.save(identifier, detail, 'uncertain')
            self.editor.observed[identifier] = r.clock()
            return {'transaction': identifier, 'mode': detail['mode'], 'group': detail['reservation']['group_id'],
                    'desired_behavior_observed': True, 'acceptance_required': True,
                    'assignment_read_back': False, 'write_retried': False}

    def accept(self, identifier):
        r = self.runtime
        journal = self.editor.receipt(identifier)
        detail = journal['detail']
        if (r.operation.locked() or not r.mesh_ready or not r.transport.ready
                or identifier not in self.editor.observed
                or not 0 <= r.clock()-self.editor.observed[identifier] <= 15000):
            raise ValueError('Fresh current-process behavior observation required')
        self._authority(identifier, detail, statuses=('uncertain',))
        target, group = journal['target'], detail['reservation']['group_id']
        latest = r.native_observations.get(target)
        evidence = detail.get('observed', {})
        if (not latest or not 0 <= r.clock()-latest[1] <= 15000
                or latest[0]['target'] != group or latest[0]['sequence'] != evidence.get('sequence')
                or latest[0]['payload'] != evidence.get('payload')
                or r.native_sequences.get(target) != evidence.get('sequence')
                or detail.get('dispatch_not_started')):
            raise ValueError('Latest native behavior no longer matches the retained observation')
        with r.store.transaction():
            inventory = copy.deepcopy(r.store.inventory())
            if detail['mode'] == 'ha':
                for device in inventory['devices']:
                    if device['device_id'] == target:
                        device['exposed'] = True
            r.store.set('inventory', inventory)
            r.store.set('local_modes', [i for i in r.store.get('local_modes', []) if i != target])
            self.editor.save(identifier, detail, 'verified')
        result = {'transaction': identifier, 'accepted_observed_behavior': True,
                  'mode': detail['mode'], 'group': group, 'assignment_read_back': False}
        try:
            reload_inventory(r)
        except Exception:
            result['publication_refresh_pending'] = True
        return result
