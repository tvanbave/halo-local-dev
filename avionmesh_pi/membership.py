"""Guarded RL4/A19 membership operations using the ESP COUNT/READ contract.

Both models use the same requests and complete two-round table verification.
Model-specific receipt names keep recovery tied to the original device type.
"""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from .protocol import integer, model_command
from .bridge import LIGHTS
from .errors import OperationNotStarted

MEMBERSHIP_SCHEMAS = {93: 'rl4-groups-v1', 134: 'a19-groups-v1'}

_READ_ONLY = object()
# The ESP membership state machine waits at least 150 ms after a reply before
# dispatching its next query. A successful GATT write is not device readiness.
_MEMBERSHIP_GAP_SECONDS = 0.150
_ADMISSION_WAIT_SECONDS = 10


class Membership:
    def __init__(self, runtime):
        self.runtime = runtime

    async def change(self, target, group, member):
        if self.runtime.capabilities.get('group_membership_readback'):
            self._validate(target, group, member)
            async with self._change_admission([target], group, member):
                return await self._change_verified(target, group, member)
        return await self._change_legacy(target, group, member)

    async def _change_legacy(self, target, group, member):
        r = self.runtime
        integer(target,32896,65407,'device')
        integer(group,256,24575,'group')
        if group == 24566 or type(member) is not bool or not r.capabilities.get('group_membership'):
            raise ValueError('Membership programming disabled or invalid request')
        destination = next((g for g in r.store.inventory()['groups'] if g['group_id']==group),None)
        device = r.core.devices.get(target)
        if not destination or destination['protected'] or not device or device.product_id not in LIGHTS:
            raise ValueError('Expected known load and ordinary group')
        self._available()
        async with r.operation:
            identifier = r.store.begin('group_membership',target,{'group':group,'desired_member':member,
                'prior_metadata_member':target in destination['members'],'physical_readback_supported':False})
            r.store.transition(identifier,'prepared','attempted')
            r.session()
            r.emit(r.discovery())
            try:
                await r.send(model_command(target,2 if member else 5,3,group.to_bytes(2,'big')+b'\0'))
            finally:
                r.store.transition(identifier,'attempted','uncertain')
            return {'transaction':identifier,'transport_accepted':True,'membership_verified':False,
                    'metadata_changed':False,'requires_review':True,'retry_allowed':False}

    def _validate(self, target, group=_READ_ONLY, member=None, *, write=True):
        r = self.runtime
        integer(target, 32896, 65407, 'device')
        device = r.core.devices.get(target)
        if not r.capabilities.get('group_membership_readback') or not device or device.product_id not in MEMBERSHIP_SCHEMAS:
            raise ValueError('Verified membership is disabled or model is not qualified for this schema')
        if group is not _READ_ONLY:
            integer(group, 256, 24575, 'group')
            destination = next((g for g in r.store.inventory()['groups'] if g['group_id'] == group), None)
            if (group == 24566 or type(member) is not bool or write and not r.capabilities.get('group_membership')
                    or destination is None or destination['protected']):
                raise ValueError('Expected enabled membership change and ordinary group')
        return device

    def _available(self, *, transaction=None, check_busy=True):
        r = self.runtime
        if any(j['id'] != transaction for j in r.store.journals(pending=True)):
            raise OperationNotStarted('pending_operation')
        if not r.mesh_ready or not r.transport.ready:
            raise OperationNotStarted('mesh_offline')
        if check_busy and r.operation.locked():
            raise OperationNotStarted('mesh_busy')

    @asynccontextmanager
    async def _change_admission(self, targets, group, member):
        """Wait for current radio work, then revalidate before any membership I/O."""
        r = self.runtime
        self._available(check_busy=False)
        inventory, generation = r.store.inventory(), r.generation
        try:
            await asyncio.wait_for(r.operation.acquire(), _ADMISSION_WAIT_SECONDS)
        except asyncio.TimeoutError:
            raise OperationNotStarted('mesh_busy') from None
        try:
            self._available(check_busy=False)
            if generation != r.generation:
                raise OperationNotStarted('session_changed')
            if inventory != r.store.inventory():
                raise OperationNotStarted('inventory_changed')
            for target in targets:
                self._validate(target, group, member)
            yield
        finally:
            r.operation.release()

    async def _verified_table(self, target, generation, cursor=None):
        r = self.runtime
        cursor = cursor if cursor is not None else {"sequence": -1}
        last_sequence = cursor["sequence"]
        async def query(slot):
            nonlocal last_sequence
            def match(packet):
                p = packet.payload
                return (packet.crypto_source == target and packet.opcode == 115
                        and packet.sequence > last_sequence
                        and len(p) == (6 if slot else 5)
                        and p[:3] == bytes([1 if slot else 4, 3, 0])
                        and (p[3] == slot if slot else 0 <= p[3] <= p[4] <= 64 and p[4] > 0))
            reply = await r.exchange(model_command(target, 1 if slot else 4, 3, bytes([slot, 0, 0])),
                                     match, timeout=3, generation=generation)
            last_sequence = reply.sequence
            cursor["sequence"] = last_sequence
            # Preserve ESP query pacing, including the boundary between rounds.
            # No read or setter is retried if a response is missing.
            await asyncio.sleep(_MEMBERSHIP_GAP_SECONDS)
            return reply.payload
        async def round_():
            count = await query(0)
            used, capacity = count[3:5]
            members = set()
            for slot in range(1, capacity + 1):
                if len(members) == used:
                    break
                raw = await query(slot)
                group = int.from_bytes(raw[4:6], 'big')
                if group and (not 256 <= group <= 24575 or group in members):
                    raise ValueError('Invalid or duplicate physical group')
                if group:
                    members.add(group)
            if len(members) != used:
                raise ValueError('Incomplete group table')
            return {'used': used, 'capacity': capacity, 'groups': sorted(members)}
        first, second = await round_(), await round_()
        if generation != r.generation or first != second:
            raise RuntimeError('Membership table changed across reads or sessions')
        return second

    async def read(self, target):
        self._validate(target)
        self._available()
        async with self.runtime.operation:
            table = await self._verified_table(target, self.runtime.generation)
            return {'target': target, 'table': table, 'metadata_changed': False}

    def _save_observation(self, target, group, desired, table, baseline, identifier=None):
        r = self.runtime
        observed = group in table['groups']
        if identifier:
            receipt = next(j for j in r.store.journals() if j['id'] == identifier)
            before = receipt['detail']['baseline_table']
            unchanged = set(before['groups'])
            expected = (unchanged - {group}) | ({group} if desired else set())
            if table['capacity'] != before['capacity'] or set(table['groups']) not in (unchanged, expected):
                raise RuntimeError('Unrelated membership or capacity changed; keep transaction quarantined')
        with r.store.transaction():
            if r.store.inventory() != baseline:
                raise RuntimeError('Inventory changed during verification')
            data = copy.deepcopy(baseline)
            destination = next(g for g in data['groups'] if g['group_id'] == group)
            destination['members'] = sorted((set(destination['members']) - {target}) | ({target} if observed else set()))
            r.store.reserve_targets(table['groups'])
            r.store.set('inventory', data)
            if identifier:
                journal = next(j for j in r.store.journals() if j['id'] == identifier)
                detail = dict(journal['detail'], observed_table=table, desired_matched=observed == desired)
                if journal['status'] not in ('attempted', 'uncertain'):
                    raise RuntimeError('Transaction changed during verification')
                changed = r.store.db.execute(
                    'UPDATE journals SET detail=?,status=? WHERE id=? AND status=?',
                    (json.dumps(detail), 'verified', identifier, journal['status'])).rowcount
                if changed != 1:
                    raise RuntimeError('Transaction changed during verification')
        result = {'transaction': identifier, 'target': target, 'group': group,
                  'membership_verified': True, 'desired_matched': observed == desired,
                  'metadata_changed': data != baseline, 'retry_allowed': False}
        try:
            r.emit(r.discovery())
            r.publish_groups()
        except Exception:
            # Publication failure cannot undo a verified, durably committed row.
            result.update(publication_pending=True, requires_review=True)
        return result

    async def _change_verified(self, target, group, member):
        r = self.runtime
        baseline = r.store.inventory()
        generation = r.generation
        cursor = {"sequence": -1}
        before = await self._verified_table(target, generation, cursor)
        if (group in before['groups']) == member:
            return self._save_observation(target, group, member, before, baseline)
        if member and before['used'] == before['capacity']:
            raise ValueError('Group table is full')
        if r.store.inventory() != baseline:
            raise RuntimeError('Inventory changed before dispatch')
        identifier = r.store.begin('group_membership', target, {
            'group': group, 'desired_member': member, 'readback_schema': MEMBERSHIP_SCHEMAS[r.core.devices[target].product_id],
            'product_id': r.core.devices[target].product_id,
            'physical_readback_supported': True, 'baseline_table': before})
        r.store.transition(identifier, 'prepared', 'attempted')
        phase = 'session_invalidation'
        try:
            # Revoke stale state and mappings before a possible physical mutation.
            r.session()
            generation = r.generation
            r.emit(r.discovery())
            phase = 'membership_submission'
            await r.send(model_command(target, 2 if member else 5, 3, group.to_bytes(2, 'big') + b'\0'), generation=generation)
            # Let the device process/persist the one setter before requesting its
            # table. This is a delay, never permission to resend the command.
            phase = 'postwrite_settle'
            await asyncio.sleep(_MEMBERSHIP_GAP_SECONDS)
            phase = 'postwrite_readback'
            after = await self._verified_table(target, generation, cursor)
            phase = 'save_observation'
            return self._save_observation(target, group, member, after, baseline, identifier)
        except BaseException as error:
            if next(j for j in r.store.journals() if j['id'] == identifier)['status'] == 'attempted':
                r.store.transition(identifier, 'attempted', 'uncertain')
                # Bounded diagnostics only: no exception strings, packets or keys.
                # Preserve the uncertain receipt even if diagnostic persistence fails.
                try:
                    receipt = next(j for j in r.store.journals() if j['id'] == identifier)
                    failure = {'phase': phase,
                               'reason': 'timeout' if isinstance(error, asyncio.TimeoutError) else
                                         'cancelled' if isinstance(error, asyncio.CancelledError) else
                                         'validation_error' if isinstance(error, ValueError) else
                                         'runtime_error' if isinstance(error, RuntimeError) else 'internal_error',
                               'session_changed': generation != r.generation,
                               'mesh_ready': bool(r.mesh_ready and r.transport.ready)}
                    detail = dict(receipt['detail'], verification_failure=failure)
                    r.store.db.execute('UPDATE journals SET detail=? WHERE id=? AND status=?',
                                       (json.dumps(detail), identifier, 'uncertain'))
                except Exception:
                    pass
            raise

    async def reconcile(self, identifier):
        r = self.runtime
        journal = next((j for j in r.store.journals(pending=True) if j['id'] == identifier), None)
        if (not journal or journal['kind'] != 'group_membership' or journal['status'] != 'uncertain'
                or journal['detail'].get('readback_schema') not in MEMBERSHIP_SCHEMAS.values()):
            raise ValueError('Expected an uncertain supported membership transaction')
        target, detail = journal['target'], journal['detail']
        device = self._validate(target, detail['group'], detail['desired_member'], write=False)
        if (detail['readback_schema'] != MEMBERSHIP_SCHEMAS[device.product_id]
                or detail.get('product_id', device.product_id) != device.product_id):
            raise ValueError('Membership receipt model does not match this light')
        self._available(transaction=identifier)
        async with r.operation:
            baseline = r.store.inventory()
            table = await self._verified_table(target, r.generation)
            return self._save_observation(target, detail['group'], detail['desired_member'], table, baseline, identifier)

    async def change_many(self, targets, group, member):
        if not isinstance(targets, list) or not 1 <= len(targets) <= 32:
            raise ValueError('Expected one to 32 distinct devices')
        for target in targets:
            self._validate(target, group, member)
        if len(set(targets)) != len(targets):
            raise ValueError('Duplicate batch targets')
        results = []
        async with self._change_admission(targets, group, member):
            for target in targets:
                try:
                    result = await self._change_verified(target, group, member)
                except (ValueError, RuntimeError, asyncio.TimeoutError):
                    results.append({'target': target, 'membership_verified': False,
                                    'requires_review': True, 'retry_allowed': False})
                    break
                results.append(result)
                if not result['desired_matched'] or result.get('requires_review'):
                    break
        return {'results': results, 'remaining': targets[len(results):],
                'complete': len(results) == len(targets) and all(row.get('desired_matched') and not row.get('requires_review') for row in results),
                'automatic_resume': False}
