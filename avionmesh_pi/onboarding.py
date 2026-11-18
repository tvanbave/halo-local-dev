"""Unused-keypad-slot setup with owned reservations and explicit safe resume.

Derived from staged ESP keypad_setup / keypad_setup_journal. Occupied or unknown
records are preserved. An attempted but unverified insertion is never repeated.
"""
import asyncio
import copy
import json

from .keypad import AdmissionExpired, KeypadService, snapshot, table_write
from .lifecycle import reload_inventory
from .keypad_ownership import PARTIAL_SETUP_SCHEMA, classify_setup_table, stored_tables


def table_from_rows(rows):
    assigned = sum(r[4:] != b'\xff\xff\0\0' for r in rows)
    return snapshot(bytes([4, 48, 0, assigned, 4]), rows)


class KeypadOnboarding:
    def __init__(self, runtime, keypad=None):
        self.runtime = runtime
        self.keypad = keypad or KeypadService(runtime)

    def admit(self, target, *, resume=False):
        r = self.runtime
        device = r.core.devices.get(target)
        flag = {127: 'wired_onboarding', 126: 'battery_onboarding'}.get(device.product_id if device else None)
        if not flag or not r.capabilities.get(flag) or not r.capabilities.get('inventory_complete'):
            raise ValueError('Onboarding disabled or authoritative inventory incomplete')
        if r.operation.locked() or not r.mesh_ready:
            raise RuntimeError('Mesh busy or offline')
        if not resume and r.store.journals(pending=True):
            raise RuntimeError('Resolve pending operations first')
        return device

    async def setup(self, target):
        device = self.admit(target)
        r = self.runtime
        async with r.operation:
            self.keypad.invalidate(target)
            before = await self.keypad.verified_rounds(target, device.product_id == 126)
            data = copy.deepcopy(r.store.inventory())
            used = set(r.store.get('known_targets', [])) | {g['group_id'] for g in data['groups']}
            used.add(24566)
            used.update(s['target'] for s in before['slots'])
            desired = [bytes.fromhex(s['raw']) for s in before['slots']]
            planned, reservations = [], []
            for row in before['slots']:
                slot = row['slot']
                if row['target'] != 65535 or row['flags'] or row['level']:
                    continue
                group = next((i for i in range(24575, 255, -1) if i not in used), None)
                if group is None:
                    raise ValueError('No free event target')
                used.add(group)
                reservation = {'group_id': group, 'name': f'HA keypad target: {target}/{slot}',
                               'members': [], 'exposed': False, 'protected': True}
                data['groups'].append(reservation)
                reservations.append(reservation)
                desired[slot-1] = bytes([1,48,0,slot]) + group.to_bytes(2, 'big') + b'\x08\xff'
                planned.append(slot)
            if not planned:
                ready = self.ready(before)
                with r.store.transaction():
                    for dev in data['devices']:
                        if dev['device_id'] == target:
                            dev['exposed'] = ready
                    r.store.set('inventory', data)
                    r.store.save_binding(target, before)
                reload_inventory(r)
                r.store.save_binding(target, before)
                r.core.controllers.bindings[target] = before
                self.keypad.remember_binding(target, before)
                r.emit(r.discovery())
                return {'changed': False, 'table': before, 'ready': ready}
            detail = {'before': before, 'desired': table_from_rows(desired), 'planned': planned,
                      'attempted': [], 'verified': [], 'stages': [], 'reservations': reservations,
                      'product_id': device.product_id}
            identifier = r.store.begin('keypad_setup', target, detail, inventory=data)
            return await self._run(identifier, before, device.product_id == 126)

    @staticmethod
    def ready(table):
        rows = table['slots']
        return all(s['flags'] == 8 and s['level'] > 0 and 256 <= s['target'] <= 24575
                   and sum(r['target'] == s['target'] for r in rows) == 1 for s in rows)

    def journal(self, identifier):
        return next(j for j in self.runtime.store.journals() if j['id'] == identifier)

    def save(self, identifier, detail, status):
        self.runtime.store.db.execute('UPDATE journals SET detail=?,status=? WHERE id=?',
                                     (json.dumps(detail), status, identifier))

    async def resume(self, identifier):
        j = self.journal(identifier)
        if j['kind'] != 'keypad_setup' or j['status'] not in ('prepared', 'uncertain'):
            raise ValueError('Expected interrupted keypad setup')
        device = self.admit(j['target'], resume=True)
        if any(x['id'] != identifier for x in self.runtime.store.journals(pending=True)):
            raise RuntimeError('Another pending operation exists')
        async with self.runtime.operation:
            self.keypad.invalidate(j['target'])
            actual = await self.keypad.verified_rounds(j['target'], device.product_id == 126)
            return await self._run(identifier, actual, device.product_id == 126)

    async def resolve_partial(self, identifier):
        """Close a reviewed mixed setup outcome using reads only; never continue writes."""
        r = self.runtime
        original = copy.deepcopy(self.journal(identifier))
        if original['kind'] != 'keypad_setup' or original['status'] != 'uncertain':
            raise ValueError('Expected uncertain keypad setup')
        target, detail = original['target'], original['detail']
        device = self.admit(target, resume=True)
        if detail.get('product_id') != device.product_id or 'partial_resolution' in detail:
            raise ValueError('Retained setup identity or resolution changed')
        # Validate the proposal/history before any radio reads. This synthetic
        # mixed table proves only that the retained plan is internally coherent.
        try:
            verified = detail['verified']
            if (not isinstance(verified, list)
                    or any(type(slot) is not int or not 1 <= slot <= 4 for slot in verified)):
                raise ValueError('Invalid retained setup verified slot history')
            validation_rows = [bytes.fromhex(row['raw']) for row in detail['before']['slots']]
            for slot in verified:
                validation_rows[slot - 1] = bytes.fromhex(detail['desired']['slots'][slot - 1]['raw'])
            classify_setup_table(detail, table_from_rows(validation_rows))
        except (KeyError, TypeError, IndexError, ValueError) as error:
            raise ValueError('Invalid retained setup history') from error
        inventory = copy.deepcopy(r.store.inventory())
        retired_claims = copy.deepcopy(r.store.get('retired_claims', []))
        retired_addresses = copy.deepcopy(r.store.get('retired_addresses', []))
        generation = r.generation

        def authority():
            self.keypad.check_session(generation)
            current = next((j for j in r.store.journals() if j['id'] == identifier), None)
            if (current != original or r.store.inventory() != inventory
                    or r.store.get('retired_claims', []) != retired_claims
                    or r.store.get('retired_addresses', []) != retired_addresses
                    or target in retired_addresses
                    or any(j['id'] != identifier for j in r.store.journals(pending=True))):
                raise ValueError('Setup recovery authority changed or another operation is pending')
            retained = [d for d in inventory['devices'] if d['device_id'] == target]
            if len(retained) != 1 or retained[0]['product_id'] != device.product_id:
                raise ValueError('Setup controller identity changed')
            for group in detail['reservations']:
                destination = group['group_id']
                slot = next(row['slot'] for row in detail['desired']['slots'] if row['target'] == destination)
                if (group not in inventory['groups'] or destination not in r.store.get('known_targets', [])
                        or not r.store.owned_target_safe(destination, target)
                        or any(owner != target or row['slot'] != slot
                               for owner, value in stored_tables(r.store).items() for row in value['slots']
                               if row['target'] == destination)):
                    raise ValueError('Setup reservation is missing, changed or shared with another owner')

        authority()
        async with r.operation:
            authority()
            self.keypad.invalidate(target)
            try:
                actual = await self.keypad.verified_rounds(target, device.product_id == 126)
                authority()
                classified = classify_setup_table(detail, actual)
                resolved = copy.deepcopy(detail)
                resolved['partial_resolution'] = dict(classified, schema=PARTIAL_SETUP_SCHEMA,
                    table=copy.deepcopy(actual), readback_rounds=2, assignment_read_back=True, write_retried=False)
                with r.store.transaction():
                    authority()
                    r.store.save_binding(target, actual)
                    changed = r.store.db.execute("UPDATE journals SET status='cancelled',detail=? WHERE id=? AND status='uncertain'",
                        (json.dumps(resolved), identifier)).rowcount
                    if changed != 1:
                        raise ValueError('Setup recovery receipt changed before closure')
            except BaseException:
                self.keypad.progress[target] = 'needs_review'
                raise
            result = dict(classified, transaction=identifier, resolved=True, partial_resolution=True,
                assignment_read_back=True, write_retried=False, setup_automatically_resumed=False,
                exposure_changed=False, reservations_retained=True, requires_review=False)
            try:
                r.core.controllers.bindings[target] = actual
                self.keypad.remember_binding(target, actual)
                self.keypad.progress[target] = 'verified'
                r.emit(r.discovery())
            except Exception:
                result.update(publication_pending=True, requires_review=True)
            return result

    async def _run(self, identifier, actual, battery):
        r = self.runtime
        j = self.journal(identifier)
        target, d = j['target'], j['detail']
        generation = r.generation
        # Persisted reservation ownership must survive intact before any dispatch.
        if any(g not in r.store.inventory()['groups'] for g in d['reservations']):
            raise ValueError('Owned reservation changed')
        for slot, row in enumerate(actual['slots'], 1):
            old, desired = d['before']['slots'][slot-1], d['desired']['slots'][slot-1]
            if row == desired and slot in d['planned']:
                if slot not in d['attempted']:
                    raise ValueError('Unexpected external assignment')
                if slot not in d['verified']:
                    d['verified'].append(slot)
            elif row != old or slot in d['attempted']:
                raise ValueError('Attempted/changed slot requires review; insertion will not repeat')
        self.save(identifier, d, j['status'])
        working = actual
        try:
            for slot in d['planned']:
                if slot in d['verified']:
                    continue
                # Battery support remains separately gated: each table round
                # admits a fresh wake, and the selected slot is reread just before TX.
                if battery:
                    fresh_table = await self.keypad.verified_rounds(target, True)
                    if fresh_table != working:
                        raise ValueError('Table changed before next wake stage')
                fresh, checked = await self.keypad.prepare_write(target, slot, battery, generation)
                if fresh.payload.hex() != working['slots'][slot-1]['raw']:
                    raise ValueError('Selected slot changed')
                for secondary in (False, True):
                    label = f'{slot}/' + ('secondary' if secondary else 'primary')
                    if label in d['stages']:
                        raise RuntimeError('Programming stage already attempted')
                    staged = copy.deepcopy(d)
                    if not secondary:
                        staged['attempted'].append(slot)
                    staged['stages'].append(label)
                    # One FULL-sync commit retains the previous reply, unique
                    # token and next intent before any transmission. This leaves
                    # more of the unchanged slot/wake budget for the radio.
                    token = self.keypad._token(checkpoint=lambda:
                        self.save(identifier, staged, 'attempted'))
                    d = staged
                    if any(not r.store.owned_target_safe(g['group_id'],target) for g in d['reservations']):
                        raise RuntimeError('Reserved target has competing native source evidence')
                    row = d['desired']['slots'][slot-1]
                    command = table_write(target,slot,token,'secondary' if secondary else 'primary',group=row['target'],level=row['level'])
                    # As in the ESP setup: a reply is a hint and its absence is not a
                    # failure. Nothing is resent; the full-table reads below decide.
                    code = None
                    try:
                        reply = await r.exchange(command,
                            lambda p, t=token: p.crypto_source == target and p.opcode == 115
                            and len(p.payload) == 4 and p.payload[:3] == bytes([2, 48, t]),
                            timeout=2, generation=generation,
                            admission=lambda: self.keypad.admit_write(target, battery, checked, generation))
                        code = reply.payload[3]
                    except asyncio.TimeoutError:
                        self.keypad.check_session(generation)
                    except AdmissionExpired:
                        if not secondary:
                            raise
                        self.keypad.check_session(generation)
                    d.setdefault('replies', {})[label] = code
                    if not secondary and code == 5:
                        # Persist this hint with the secondary's intent/token.
                        # An interruption still saves it in the failure handler;
                        # no replay is permitted for the attempted primary.
                        continue
                    self.save(identifier, d, 'attempted')
                    if not secondary:
                        break  # Only the observed `05` status permits the follow-up record.
                rows = [bytes.fromhex(s['raw']) for s in working['slots']]
                rows[slot-1] = bytes.fromhex(d['desired']['slots'][slot-1]['raw'])
                expected_table = table_from_rows(rows)
                working = await self.keypad.verified_rounds(target, battery)
                if working != expected_table:
                    raise ValueError('Programmed table did not match complete proposal')
                d['verified'].append(slot)
                self.save(identifier, d, 'attempted')
            if working != d['desired']:
                raise ValueError('Final table mismatch')
            if any(not r.store.owned_target_safe(g['group_id'],target) for g in d['reservations']):
                raise RuntimeError('Reserved target ownership changed')
            ready = self.ready(working)
            with r.store.transaction():
                data = copy.deepcopy(r.store.inventory())
                for dev in data['devices']:
                    if dev['device_id'] == target:
                        dev['exposed'] = ready
                r.store.set('inventory', data)
                self.save(identifier, d, 'verified')
                r.store.save_binding(target, working)
            reload_inventory(r)
            r.store.save_binding(target, working)
            r.core.controllers.bindings[target] = working
            self.keypad.remember_binding(target, working)
            r.emit(r.discovery())
            return {'transaction': identifier, 'setup_complete': True, 'ready': ready,
                    'existing_assignments_preserved': True}
        except BaseException:
            try:
                self.save(identifier, d, 'uncertain' if d['attempted'] else 'prepared')
            finally:
                await r.transport.disconnect()
                r.mesh_ready = False
                r.session()
            raise
