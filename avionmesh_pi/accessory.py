"""Fresh-claim Accessory enrollment; accepted behavior is not assignment readback."""
import asyncio
import copy
import json
import math

from .association import uuid_hash
from .bridge import LIGHTS
from .protocol import decode_controller, integer, ping_command, ping_match
from .lifecycle import reload_inventory


ACCESSORY_PROGRAMMING = ('accessory_setup', 'accessory_native_setup', 'dimmer_edit')


def fresh_native_claim(target, journals, retired_claims=(), retired_addresses=(), *, receipt=None):
    """Return the unique retained association identity, or reject programming.

    ``receipt`` permits observation-only validation of this one native operation.
    A cancelled attempt is retained too; it never silently becomes a fresh claim.
    """
    integer(target, 32896, 65407, 'controller')
    claims = [j for j in journals if j['kind'] == 'claim' and j['target'] == target
              and j['status'] == 'verified' and j['id'] not in retired_claims]
    if target == 32898 or target in retired_addresses or len(claims) != 1:
        raise ValueError('Unique fresh retained Accessory claim required')
    claim = claims[0]
    detail = claim.get('detail', {})
    identity = detail.get('uuid')
    try:
        raw = bytes.fromhex(identity) if isinstance(identity, str) and len(identity) == 32 else b''
    except ValueError:
        raw = b''
    if (detail.get('product_id') != 91 or detail.get('association_complete') is not True
            or len(raw) != 16 or not any(raw) or detail.get('uuid_hash') != uuid_hash(raw)):
        raise ValueError('Complete exact Accessory association identity required')
    if any(j['kind'] == 'claim' and j['id'] != claim['id']
           and j['id'] not in retired_claims and
           (j.get('detail', {}).get('uuid') == identity
            or j.get('detail', {}).get('uuid_hash') == detail['uuid_hash']) for j in journals):
        raise ValueError('Accessory identity has another retained claim')
    prior = [j for j in journals if j['target'] == target and j['kind'] in ACCESSORY_PROGRAMMING]
    if prior != ([receipt] if receipt is not None else []):
        raise ValueError('Fresh retained claim required; enrollment cannot repeat')
    return claim


class AccessoryOnboarding:
    def __init__(self, runtime):
        self.runtime = runtime
        self.progress = {}

    def receipt(self, identifier):
        return next(j for j in self.runtime.store.journals() if j['id'] == identifier)

    def save(self, identifier, detail, status):
        self.runtime.store.db.execute('UPDATE journals SET detail=?,status=? WHERE id=?',
                                     (json.dumps(detail),status,identifier))

    def native_match(self, packet, target, group, product=91):
        event = decode_controller(packet,product)
        return (packet.crypto_source == target and event is not None
                and 'brightness' in event and event.get('target') == group)

    async def observe_native(self, identifier, *, dispatch=None, timeout=120, admission=None):
        r = self.runtime
        generation=r.generation
        j = self.receipt(identifier)
        target, group = j['target'], j['detail']['reservation']['group_id']
        product = j['detail'].get('product_id',91)
        future = asyncio.get_running_loop().create_future()
        minimum = (j['detail'].get('baseline', {}).get('sequence')
                   if j['kind'] == 'accessory_native_setup' else None)
        def fresh(packet):
            return minimum is None or 0 < ((packet.sequence-minimum) & 0xffffff) < 0x800000
        waiter = (lambda p:fresh(p) and self.native_match(p,target,group,product),future,r.clock())
        contradicted = False
        def contradict(packet, when):
            nonlocal contradicted
            event = decode_controller(packet,product)
            if (fresh(packet) and packet.crypto_source == target and event and 'brightness' in event
                    and when >= waiter[2]
                    and (j['kind'] == 'accessory_native_setup' or 256 <= event.get('target',0) <= 24575)
                    and event.get('target') != group):
                contradicted = True
                if not future.done():
                    future.set_exception(RuntimeError('Conflicting native target observed'))
        r.waiters.append(waiter)
        r.packet_observers.append(contradict)
        try:
            if dispatch:
                self.progress[target] = 'writing'
                await r.send(dispatch, generation=generation, admission=admission)
            # The listener is installed before dispatch. Ask for a press only
            # after transport acceptance, and never after an early input/error.
            if not future.done():
                self.progress[target] = 'awaiting_input'
            packet = await asyncio.wait_for(future,timeout)
            if generation!=r.generation:
                raise RuntimeError('Native observation belongs to an earlier session')
            if contradicted:
                raise RuntimeError('Conflicting native target observed')
            self.progress[target] = 'observed'
            return {'sequence':packet.sequence,'payload':packet.payload.hex()}
        finally:
            self.progress.pop(target, None)
            r.waiters.remove(waiter)
            r.packet_observers.remove(contradict)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def setup(self, target, *, timeout=120):
        r = self.runtime
        if not r.capabilities.get('accessory_onboarding') or not r.capabilities.get('inventory_complete'):
            raise ValueError('Accessory enrollment disabled or inventory incomplete')
        device = r.core.devices.get(target)
        if not device or device.product_id != 91 or target == 32898:
            raise ValueError('Expected a nonlegacy freshly claimed Accessory')
        if r.operation.locked() or not r.mesh_ready or r.store.journals(pending=True):
            raise RuntimeError('Mesh busy or an operation needs reconciliation')
        claim = next((j for j in r.store.journals() if j['kind']=='claim' and j['target']==target
                      and j['status']=='verified' and j['id'] not in r.store.get('retired_claims',[])),None)
        if claim is None or claim['detail'].get('product_id') != 91 or any(j['kind'] in ACCESSORY_PROGRAMMING and j['target']==target for j in r.store.journals()):
            raise ValueError('Fresh retained claim required; enrollment cannot repeat')
        async with r.operation:
            data = copy.deepcopy(r.store.inventory())
            used = set(r.store.get('known_targets',[])) | {g['group_id'] for g in data['groups']}
            used.add(24566)  # reserved legacy test target remains out of allocation
            group = next((i for i in range(24575,255,-1) if i not in used),None)
            generation = sum(j['kind'] in ('accessory_setup','accessory_native_setup') for j in r.store.journals()) + 1
            if group is None or generation > 127:
                raise ValueError('Enrollment reservation capacity exhausted')
            reservation = {'group_id':group,'name':f'HA dimmer target: {target}',
                           'members':[],'exposed':False,'protected':True}
            data['groups'].append(reservation)
            detail = {'claim':claim['id'],'reservation':reservation,'token':generation,
                      'original_assignment':'unknown','assignment_attempted':False,'ping_attempted':False}
            identifier = r.store.begin('accessory_setup',target,detail,inventory=data)
            r.session()
            self.progress[target] = 'preparing'
            r.emit(r.discovery())
            try:
                detail['ping_attempted'] = True
                self.save(identifier,detail,'attempted')
                token = 0x80 + generation
                ping = await r.exchange(ping_command(target,token),lambda p:ping_match(p,target,token)
                    and len(p.payload)==7 and p.payload[3]==1,timeout=5)
                baseline_at = r.clock()
                detail['baseline'] = {'sequence':ping.sequence,'payload':ping.payload.hex()}
                detail['assignment_attempted'] = True
                self.save(identifier,detail,'attempted')
                if (r.clock()-baseline_at > 15000 or reservation not in r.store.inventory()['groups']
                        or not r.store.owned_target_safe(group,target)):
                    raise RuntimeError('Enrollment admission expired or ownership changed')
                command = target.to_bytes(2,'little') + b'\x73\x00\x1b\0\0' + bytes([generation]) + group.to_bytes(2,'big') + b'\x01\0\0'
                detail['observed'] = await self.observe_native(identifier,dispatch=command,timeout=timeout)
                return self.accept(identifier,detail)
            except BaseException:
                try:
                    self.save(identifier,detail,'uncertain')
                finally:
                    await r.transport.disconnect()
                    r.mesh_ready = False
                    r.session()
                raise
            finally:
                self.progress.pop(target, None)

    def _native_inputs(self, target, group, *, receipt=None):
        r = self.runtime
        claim = fresh_native_claim(target, r.store.journals(), r.store.get('retired_claims', []),
                                   r.store.get('retired_addresses', []), receipt=receipt)
        integer(group, 256, 24575, 'group')
        inventory = r.store.inventory()
        devices = [d for d in inventory['devices'] if d['device_id'] == target]
        device = r.core.devices.get(target)
        groups = [g for g in inventory['groups'] if g['group_id'] == group]
        if (len(devices) != 1 or devices[0]['product_id'] != 91
                or not device or device.product_id != 91 or len(groups) != 1
                or groups[0].get('protected') is not False or not groups[0].get('members')):
            raise ValueError('Expected retained Accessory and ordinary nonempty lighting group')
        members = []
        for member in groups[0]['members']:
            rows = [d for d in inventory['devices'] if d['device_id'] == member]
            light = r.core.devices.get(member)
            if (len(rows) != 1 or rows[0]['product_id'] not in LIGHTS
                    or not light or light.product_id != rows[0]['product_id']
                    or member in r.store.get('retired_addresses', [])):
                raise ValueError('Native destination contains an unknown or non-light member')
            members.append({'device_id': member, 'product_id': rows[0]['product_id']})
        if len({m['device_id'] for m in members}) != len(members):
            raise ValueError('Native destination contains duplicate members')
        if receipt is not None:
            detail = receipt['detail']
            if (detail.get('mode') != 'native' or detail.get('product_id') != 91
                    or detail.get('claim_receipt') != claim or detail.get('claim') != claim['id']
                    or detail.get('reservation') != groups[0] or detail.get('members') != members):
                raise ValueError('Reviewed native setup identity or destination changed')
        return claim, groups[0], members

    async def setup_native(self, target, group, *, timeout=120):
        """Program one fresh model-91 claim directly to an ordinary lighting group."""
        r = self.runtime
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 120):
            raise ValueError('Expected an observation timeout in (0, 120] seconds')
        if not r.capabilities.get('accessory_onboarding') or not r.capabilities.get('inventory_complete'):
            raise ValueError('Accessory enrollment disabled or inventory incomplete')
        if r.operation.locked() or not r.mesh_ready or not r.transport.ready or r.store.journals(pending=True):
            raise RuntimeError('Mesh busy or an operation needs reconciliation')
        async with r.operation:
            claim, destination, members = self._native_inputs(target, group)
            token = sum(j['kind'] in ('accessory_setup', 'accessory_native_setup')
                        for j in r.store.journals()) + 1
            if token > 127:
                raise ValueError('Enrollment token capacity exhausted')
            detail = {'mode': 'native', 'product_id': 91, 'claim': claim['id'],
                      'claim_receipt': copy.deepcopy(claim), 'reservation': copy.deepcopy(destination),
                      'members': members, 'token': token, 'original_assignment': 'unknown',
                      'ping_attempted': False, 'assignment_attempted': False,
                      'assignment_read_back': False}
            identifier = r.store.begin('accessory_native_setup', target, detail)
            r.session()
            generation = r.generation
            self.progress[target] = 'preparing'
            r.emit(r.discovery())

            def admit():
                if (generation != r.generation or not r.mesh_ready or not r.transport.ready
                        or r.capabilities.get('accessory_onboarding') is not True
                        or r.capabilities.get('inventory_complete') is not True):
                    raise RuntimeError('Native setup belongs to an offline or earlier session')
                current = self.receipt(identifier)
                if current['status'] != 'attempted' or current['detail'] != detail:
                    raise ValueError('Native setup receipt changed')
                if any(j['id'] != identifier for j in r.store.journals(pending=True)):
                    raise RuntimeError('Another operation owns reconciliation')
                self._native_inputs(target, group, receipt=current)

            try:
                detail['ping_attempted'] = True
                self.save(identifier, detail, 'attempted')
                ping_token = 0x80 + token
                ping = await r.exchange(ping_command(target, ping_token),
                    lambda p: ping_match(p, target, ping_token) and len(p.payload) == 7 and p.payload[3] == 1,
                    timeout=5, generation=generation, admission=admit)
                baseline_at = r.clock()
                detail['baseline'] = {'sequence': ping.sequence, 'payload': ping.payload.hex()}
                detail['assignment_attempted'] = True
                self.save(identifier, detail, 'attempted')

                def dispatch_admission():
                    admit()
                    if not 0 <= r.clock()-baseline_at <= 15000:
                        raise RuntimeError('Native setup ping evidence expired')

                command = (target.to_bytes(2, 'little') + b'\x73\x00\x1b\0\0' + bytes([token])
                           + group.to_bytes(2, 'big') + b'\x01\0\0')
                observed = await self.observe_native(identifier, dispatch=command, timeout=timeout,
                                                     admission=dispatch_admission)
                admit()
                detail['observed'] = observed
                return self._accept_native(identifier, detail)
            except BaseException:
                # Once acceptance commits, publication failures cannot turn a
                # verified setter into another uncertain programming attempt.
                try:
                    accepted = self.receipt(identifier)['status'] == 'verified'
                except Exception:
                    accepted = False
                if accepted:
                    raise
                try:
                    self.save(identifier, detail, 'uncertain' if detail['assignment_attempted'] else 'cancelled')
                finally:
                    try:
                        await r.transport.disconnect()
                    finally:
                        r.mesh_ready = False
                        r.session()
                raise
            finally:
                self.progress.pop(target, None)

    def _accept_native(self, identifier, detail):
        r = self.runtime
        journal = self.receipt(identifier)
        latest = r.native_observations.get(journal['target'])
        observed = detail.get('observed', {})
        if (journal['status'] not in ('attempted', 'uncertain') or not r.mesh_ready or not r.transport.ready
                or not latest or latest[0]['target'] != detail['reservation']['group_id']
                or latest[0].get('sequence') != observed.get('sequence')
                or latest[0].get('payload') != observed.get('payload')
                or not 0 <= r.clock()-latest[1] <= 15000):
            raise ValueError('Fresh current-session native behavior required')
        self._native_inputs(journal['target'], detail['reservation']['group_id'], receipt=journal)
        with r.store.transaction():
            self.save(identifier, detail, 'verified')
            r.store.set('local_modes', [i for i in r.store.get('local_modes', []) if i != journal['target']])
        result = {'transaction': identifier, 'ready': True, 'mode': 'native',
                'group': detail['reservation']['group_id'], 'native_behavior_observed': True,
                'assignment_read_back': False, 'original_assignment': 'unknown'}
        try:
            r.core.controllers.local_modes = set(r.store.get('local_modes', []))
            r.core.controllers.baselines.pop(journal['target'], None)
            r.emit(r.discovery())
        except Exception:
            result['publication_refresh_pending'] = True
        return result

    def accept(self, identifier, detail):
        r = self.runtime
        j = self.receipt(identifier)
        with r.store.transaction():
            data = copy.deepcopy(r.store.inventory())
            if (detail['reservation'] not in data['groups']
                    or not r.store.owned_target_safe(detail['reservation']['group_id'],j['target'])):
                raise ValueError('Enrollment reservation changed')
            for device in data['devices']:
                if device['device_id'] == j['target']:
                    device['exposed'] = True
            r.store.set('inventory',data)
            r.store.set('local_modes',[i for i in r.store.get('local_modes',[]) if i != j['target']])
            self.save(identifier,detail,'verified')
        reload_inventory(r)
        return {'transaction':identifier,'ready':True,'native_behavior_observed':True,
                'assignment_read_back':False,'original_assignment':'unknown'}

    async def reconcile(self, identifier, *, timeout=120):
        r = self.runtime
        j = self.receipt(identifier)
        if j['kind'] not in ('accessory_setup','accessory_native_setup') or j['status']!='uncertain' or not j['detail']['assignment_attempted']:
            raise ValueError('No attempted assignment available for observation-only reconciliation')
        if j['kind'] == 'accessory_native_setup' and (type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 120):
            raise ValueError('Expected an observation timeout in (0, 120] seconds')
        if r.operation.locked() or not r.mesh_ready:
            raise RuntimeError('Mesh busy or offline')
        if any(p['id'] != identifier for p in r.store.journals(pending=True)):
            raise RuntimeError('Another lifecycle owns reconciliation')
        async with r.operation:
            native = j['kind'] == 'accessory_native_setup'
            generation = r.generation
            if native:
                self._native_inputs(j['target'], j['detail']['reservation']['group_id'], receipt=j)
            detail = j['detail']
            observed = await self.observe_native(identifier,timeout=timeout)
            if native:
                if (generation != r.generation or not r.mesh_ready or not r.transport.ready
                        or self.receipt(identifier) != j
                        or any(p['id'] != identifier for p in r.store.journals(pending=True))):
                    raise ValueError('Native setup reconciliation changed')
                self._native_inputs(j['target'], detail['reservation']['group_id'], receipt=j)
                detail['observed'] = observed
                return self._accept_native(identifier, detail)
            detail['observed'] = observed
            return self.accept(identifier,detail)
