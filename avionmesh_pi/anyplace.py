"""Experimental fresh Anyplace enrollment using the captured noun-27 setter.

Unknown original assignments stay unknown. No ping, assignment read, clear,
restore or setter retry is inferred from the model-91 enrollment workflow.
"""
import asyncio
import copy
import math

from .accessory import AccessoryOnboarding
from .errors import OperationNotStarted
from .lifecycle import reload_inventory
from .protocol import decode_controller, integer


def cancelled_setup_allows_new(journal, *, target, claim, groups, known_targets=(), target_sources=None):
    """Only intact, definite no-dispatch evidence permits a new reviewed request.

    The old reservation/token remain burned. This predicate never resumes an
    operation, and callers must establish that ``claim`` is uniquely retained.
    """
    if not isinstance(journal, dict) or not isinstance(claim, dict):
        return False
    detail = journal.get('detail', {})
    if not isinstance(detail, dict):
        return False
    reservation = detail.get('reservation', {})
    if not isinstance(reservation, dict):
        return False
    group, token = reservation.get('group_id'), detail.get('token')
    return (journal.get('kind') == 'anyplace_setup' and journal.get('target') == target
            and journal.get('status') == 'cancelled' and detail.get('product_id') == 81
            and claim.get('kind') == 'claim' and claim.get('target') == target
            and claim.get('status') == 'verified' and claim.get('detail', {}).get('product_id') == 81
            and detail.get('claim') == claim.get('id') and detail.get('claim_receipt') == claim
            and detail.get('dispatch_not_started') is True
            and type(detail.get('assignment_attempted')) is bool
            and detail.get('original_assignment') == 'unknown'
            and 'observed' not in detail and 'reconciliation_wake' not in detail
            and type(token) is int and 1 <= token <= 255
            and type(group) is int and 256 <= group <= 24575 and group != 24566
            and group in known_targets and reservation.get('protected') is True
            and reservation.get('members') == [] and reservation.get('exposed') is False
            and [g for g in groups if g.get('group_id') == group] == [reservation]
            and not (target_sources or {}).get(str(group), []))


def cancelled_setups_allow_new(prior, *, target, claim, groups, known_targets=(),
                               target_sources=None, dimmer_token=0):
    """Validate the complete abandoned history, including unique burned tokens/targets."""
    if not all(cancelled_setup_allows_new(j, target=target, claim=claim, groups=groups,
                   known_targets=known_targets, target_sources=target_sources) for j in prior):
        return False
    return (all(type(dimmer_token) is int and j['detail']['token'] <= dimmer_token for j in prior)
            and len({j['detail']['token'] for j in prior}) == len(prior)
            and len({j['detail']['reservation']['group_id'] for j in prior}) == len(prior))


class AnyplaceOnboarding(AccessoryOnboarding):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.progress = {}
        self.wake_sequences = {}

    @staticmethod
    def _forward(sequence, previous):
        return previous is None or 0 < ((sequence - previous) & 0xffffff) < 0x800000

    def _deadline(self, timeout):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 120):
            raise ValueError('Expected an observation timeout in (0, 120] seconds')
        return self.runtime.clock() + timeout * 1000

    def _remaining(self, deadline, generation):
        r = self.runtime
        if generation != r.generation or not r.mesh_ready or not r.transport.ready:
            raise RuntimeError('Anyplace enrollment belongs to an offline or earlier session')
        remaining = deadline - r.clock()
        if remaining <= 0:
            raise asyncio.TimeoutError('Anyplace observation deadline expired')
        return remaining / 1000

    def _claim(self, target):
        r = self.runtime
        claims = [j for j in r.store.journals() if j['kind'] == 'claim'
                  and j['target'] == target and j['status'] == 'verified'
                  and j['id'] not in r.store.get('retired_claims', [])]
        if (len(claims) != 1 or claims[0]['detail'].get('product_id') != 81
                or target in r.store.get('retired_addresses', [])):
            raise ValueError('One verified retained model-81 claim is required')
        return claims[0]

    def receipt(self, identifier):
        matches = [j for j in self.runtime.store.journals() if j['id'] == identifier]
        if len(matches) != 1:
            raise ValueError('Expected a retained Anyplace enrollment receipt')
        return matches[0]

    def _identity(self, target, claim):
        r = self.runtime
        device = r.core.devices.get(target)
        stored = [d for d in r.store.inventory()['devices'] if d['device_id'] == target]
        if (not device or device.product_id != 81 or len(stored) != 1
                or stored[0]['product_id'] != 81 or self._claim(target) != claim):
            raise ValueError('Anyplace retained claim or device identity changed')

    def _prior_setups(self, target, claim, *, current=None):
        store = self.runtime.store
        prior = [j for j in store.journals() if j['target'] == target and j['id'] != current
                 and (j['kind'].endswith('_setup') or j['kind'] in ('dimmer_edit', 'keypad_edit'))]
        groups, known = store.inventory()['groups'], store.get('known_targets', [])
        sources, token = store.get('target_sources', {}), store.get('dimmer_token', 0)
        if not cancelled_setups_allow_new(prior, target=target, claim=claim, groups=groups,
                known_targets=known, target_sources=sources, dimmer_token=token):
            raise ValueError('Prior Anyplace operation needs review; it cannot be retried')
        return prior

    def _ownership(self, identifier, target, claim, detail):
        r = self.runtime
        self._identity(target, claim)
        journal = self.receipt(identifier)
        if (journal['kind'] != 'anyplace_setup' or journal['target'] != target
                or journal['status'] not in ('prepared', 'attempted', 'uncertain')
                or journal['detail'] != detail
                or any(j['id'] != identifier for j in r.store.journals(pending=True))):
            raise ValueError('Anyplace enrollment receipt or operation ownership changed')
        reservation = detail['reservation']
        matches = [g for g in r.store.inventory()['groups']
                   if g['group_id'] == reservation['group_id']]
        if (matches != [reservation] or reservation.get('protected') is not True
                or reservation.get('members') != [] or reservation.get('exposed') is not False
                or not r.store.owned_target_safe(reservation['group_id'], target)):
            raise ValueError('Anyplace event target ownership changed')

    async def _wake(self, target, generation, deadline, *, previous=None):
        r = self.runtime
        self._remaining(deadline, generation)
        future = asyncio.get_running_loop().create_future()
        since = r.clock()

        def observe(packet, when):
            if (future.done() or generation != r.generation or not since <= when <= r.clock()
                    or r.clock() - when > 250 or r.clock() >= deadline
                    or packet.crypto_source != target or packet.opcode != 115
                    or len(packet.payload) != 7 or packet.payload[:2] != b'\x0b\x1c'):
                return
            last = self.wake_sequences.get(target)
            sequences = [previous, r.native_sequences.get(target)]
            if last and last[0] == generation:
                sequences.append(last[1])
            if not all(self._forward(packet.sequence, value) for value in sequences):
                return
            # Consume the first qualifying receipt even if cancellation or a late
            # task resumption prevents dispatch. Later packets cannot renew it.
            self.wake_sequences[target] = (generation, packet.sequence)
            future.set_result({'sequence': packet.sequence, 'received_ms': when})

        waiter = (lambda packet: False, future, since)
        r.packet_observers.append(observe)
        r.waiters.append(waiter)
        try:
            return await asyncio.wait_for(future, self._remaining(deadline, generation))
        finally:
            r.packet_observers.remove(observe)
            r.waiters.remove(waiter)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _observe(self, target, group, wake, generation, deadline, *, dispatch=None,
                       admission=None):
        r = self.runtime
        future = asyncio.get_running_loop().create_future()
        since = r.clock()
        admitted_at = None if dispatch is not None else since
        contradicted = False

        def observe(packet, when):
            nonlocal contradicted
            if (admitted_at is None or generation != r.generation or packet.crypto_source != target
                    or not admitted_at <= when <= r.clock() or r.clock() >= deadline
                    or not self._forward(packet.sequence, wake['sequence'])):
                return
            event = decode_controller(packet, 81)
            if not event or 'brightness' not in event:
                return
            if event['target'] != group:
                contradicted = True
                if not future.done():
                    future.set_exception(RuntimeError('Conflicting Anyplace native target observed'))
            elif not future.done():
                future.set_result({'sequence': packet.sequence, 'payload': packet.payload.hex(),
                                   'received_ms': when})

        def admit():
            nonlocal admitted_at
            admission()
            admitted_at = r.clock()

        waiter = (lambda packet: False, future, since)
        r.packet_observers.append(observe)
        r.waiters.append(waiter)
        try:
            self._remaining(deadline, generation)
            if dispatch is not None:
                await r.send(dispatch, generation=generation, admission=admit)
            result = await asyncio.wait_for(future, self._remaining(deadline, generation))
            self._remaining(deadline, generation)
            if contradicted:
                raise RuntimeError('Conflicting Anyplace native target observed')
            return result
        finally:
            r.packet_observers.remove(observe)
            r.waiters.remove(waiter)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def _accept(self, identifier, target, claim, detail, generation, deadline):
        r = self.runtime
        with r.store.transaction():
            self._remaining(deadline, generation)
            self._ownership(identifier, target, claim, detail)
            latest = r.native_observations.get(target)
            if (not latest or latest[0]['target'] != detail['reservation']['group_id']
                    or latest[0]['sequence'] != detail['observed']['sequence']
                    or r.native_sequences.get(target) != detail['observed']['sequence']
                    or latest[1] != detail['observed']['received_ms']):
                raise ValueError('Latest Anyplace native behavior no longer matches')
            data = copy.deepcopy(r.store.inventory())
            for device in data['devices']:
                if device['device_id'] == target:
                    device['exposed'] = True
            r.store.set('inventory', data)
            self.save(identifier, detail, 'verified')
        result = {'transaction': identifier, 'ready': True, 'native_behavior_observed': True,
                  'assignment_read_back': False, 'original_assignment': 'unknown'}
        try:
            reload_inventory(r)
        except Exception:
            # Acceptance is already durable. A publication/storage refresh
            # failure must never turn it into another assignment opportunity.
            r.last_error = 'Anyplace acceptance is saved; publication refresh needs review'
            self.progress[target] = 'needs_review'
            return dict(result, ready=False, publication_pending=True, requires_review=True)
        self.progress[target] = 'complete'
        return result

    async def setup(self, target, *, timeout=120):
        r = self.runtime
        integer(target, 32896, 65407, 'controller')
        deadline = self._deadline(timeout)
        if not r.capabilities.get('anyplace_onboarding') or not r.capabilities.get('inventory_complete'):
            raise ValueError('Anyplace enrollment disabled or inventory incomplete')
        if r.operation.locked() or r.store.journals(pending=True):
            raise RuntimeError('Mesh busy or an operation needs reconciliation')
        generation = r.generation
        self._remaining(deadline, generation)
        claim = self._claim(target)
        self._identity(target, claim)
        prior = self._prior_setups(target, claim)
        async with r.operation:
            data = copy.deepcopy(r.store.inventory())
            used = (set(r.store.get('known_targets', []))
                    | {g['group_id'] for g in data['groups']}
                    | {int(value) for value in r.store.get('target_sources', {})} | {24566})
            group = next((i for i in range(24575, 255, -1) if i not in used), None)
            if group is None or len(data['groups']) >= 128:
                raise ValueError('Anyplace event target reservation capacity exhausted')
            with r.store.transaction():
                token = r.store.get('dimmer_token', 0) + 1
                if token > 255:
                    raise ValueError('Dimmer token space exhausted')
                r.store.set('dimmer_token', token)
            reservation = {'group_id': group, 'name': f'HA Anyplace target: {target}',
                           'members': [], 'exposed': False, 'protected': True}
            data['groups'].append(reservation)
            detail = {'product_id': 81, 'claim': claim['id'], 'claim_receipt': copy.deepcopy(claim),
                      'reservation': reservation,
                      'token': token, 'original_assignment': 'unknown', 'assignment_attempted': False}
            identifier = r.store.begin('anyplace_setup', target, detail, inventory=data,
                                       reserved_targets=[group])
            dispatch_admitted = False
            try:
                self.progress[target] = 'waiting_for_wake'
                wake = await self._wake(target, generation, deadline)
                detail['wake'] = wake
                self.save(identifier, detail, 'prepared')

                def validate():
                    self._remaining(deadline, generation)
                    self._ownership(identifier, target, claim, detail)
                    if self._prior_setups(target, claim, current=identifier) != prior:
                        raise ValueError('Prior Anyplace cancellation evidence changed')
                    if not r.capabilities.get('anyplace_onboarding') or not r.capabilities.get('inventory_complete'):
                        raise ValueError('Anyplace enrollment capability changed')
                    if (not 0 <= r.clock() - wake['received_ms'] <= 250
                            or self.wake_sequences.get(target) != (generation, wake['sequence'])
                            or not self._forward(wake['sequence'], r.native_sequences.get(target))):
                        raise RuntimeError('Anyplace wake dispatch admission expired or superseded')

                def admit():
                    nonlocal dispatch_admitted
                    validate()
                    detail['assignment_attempted'] = True
                    self.save(identifier, detail, 'attempted')
                    # Attempt persistence consumes the original wake budget too.
                    validate()
                    dispatch_admitted = True
                    self.progress[target] = 'observing'

                validate()
                command = (target.to_bytes(2, 'little') + b'\x73\0\x1b\0\0' + bytes([token])
                           + group.to_bytes(2, 'big') + b'\x01\0\0')
                observed = await self._observe(target, group, wake, generation, deadline,
                                               dispatch=command, admission=admit)
                self._ownership(identifier, target, claim, detail)
                if self._prior_setups(target, claim, current=identifier) != prior:
                    raise ValueError('Prior Anyplace cancellation evidence changed')
                detail['observed'] = observed
                self.save(identifier, detail, 'attempted')
                return self._accept(identifier, target, claim, detail, generation, deadline)
            except BaseException as error:
                self.progress[target] = 'needs_review'
                # Also preserve acceptance for non-Exception interruptions after
                # the commit; only an unfinished attempt becomes uncertain.
                try:
                    accepted = self.receipt(identifier)['status'] == 'verified'
                except Exception:
                    accepted = False  # Storage failure still requires isolation.
                if accepted:
                    raise
                try:
                    if not dispatch_admitted:
                        detail['dispatch_not_started'] = True
                    self.save(identifier, detail, 'uncertain' if dispatch_admitted else 'cancelled')
                finally:
                    if dispatch_admitted:
                        try:
                            await r.transport.disconnect()
                        finally:
                            r.mesh_ready = False
                            r.session()
                if not dispatch_admitted and isinstance(error, Exception):
                    code = ('anyplace_wake_timeout' if isinstance(error, asyncio.TimeoutError)
                            and 'wake' not in detail else 'anyplace_admission_expired')
                    stopped = OperationNotStarted(code)
                    raise stopped from error
                raise

    async def reconcile(self, identifier, *, timeout=120):
        r = self.runtime
        deadline = self._deadline(timeout)
        journal = self.receipt(identifier)
        detail = copy.deepcopy(journal['detail'])
        if (journal['kind'] != 'anyplace_setup' or journal['status'] != 'uncertain'
                or detail.get('product_id') != 81 or detail.get('assignment_attempted') is not True
                or detail.get('dispatch_not_started') or 'wake' not in detail):
            raise ValueError('No attempted Anyplace enrollment available for observation-only reconciliation')
        if r.operation.locked():
            raise RuntimeError('Mesh busy')
        generation, target = r.generation, journal['target']
        claim = self._claim(target)
        if detail['claim'] != claim['id']:
            raise ValueError('Anyplace original retained claim changed')
        self._remaining(deadline, generation)
        self._ownership(identifier, target, claim, detail)
        async with r.operation:
            try:
                self.progress[target] = 'waiting_for_wake'
                wake = await self._wake(target, generation, deadline, previous=detail['wake']['sequence'])
                self.progress[target] = 'observing'
                observed = await self._observe(target, detail['reservation']['group_id'],
                                               wake, generation, deadline)
                self._ownership(identifier, target, claim, detail)
                detail['observed'] = observed
                detail['reconciliation_wake'] = wake
                self.save(identifier, detail, 'uncertain')
                result = self._accept(identifier, target, claim, detail, generation, deadline)
                return dict(result, write_retried=False)
            except BaseException:
                self.progress[target] = 'needs_review'
                raise
