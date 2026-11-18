"""Scoped native dimmer target setter; no guessed READ or clear operation."""
import asyncio
import copy

from .accessory import AccessoryOnboarding
from .protocol import integer
from .dimmer_control import ManagedDimmerConfiguration, SCHEMA


class DimmerEditor(AccessoryOnboarding):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.observed = {}
        self.wake_sequences = {}
        runtime.session_observers.append(self.observed.clear)
        self.configuration = ManagedDimmerConfiguration(self)

    def preview_configuration(self, target, mode, group=None):
        return self.configuration.preview(target, mode, group)

    async def configure(self, target, mode, group=None, *, expected_baseline, expected_revision, timeout=120):
        return await self.configuration.configure(target, mode, group,
            expected_baseline=expected_baseline, expected_revision=expected_revision, timeout=timeout)

    async def wait_wake(self, target, baseline, generation, timeout):
        """Consume one fresh model-81 wake; a delayed task cannot renew its age."""
        r = self.runtime
        future = asyncio.get_running_loop().create_future()
        since, received = r.clock(), {}

        def observe(packet, when):
            if (future.done() or received or generation != r.generation
                    or not since <= when <= r.clock() or r.clock()-when > 250
                    or packet.crypto_source != target or packet.opcode != 115
                    or len(packet.payload) != 7 or packet.payload[:2] != b'\x0b\x1c'):
                return
            sequences = [baseline[0]['sequence'], r.native_sequences.get(target)]
            previous = self.wake_sequences.get(target)
            if previous is not None and previous[0] == generation:
                sequences.append(previous[1])
            if any(sequence is not None and not 0 < ((packet.sequence-sequence) & 0xffffff) < 0x800000
                   for sequence in sequences):
                return
            received[id(packet)] = when
            # Consumed at receipt, including if the waiting task is cancelled or
            # resumes late. Runtime's short duplicate cache is not this boundary.
            self.wake_sequences[target] = (generation, packet.sequence)

        waiter = (lambda packet: id(packet) in received, future, since)
        r.packet_observers.append(observe)
        r.waiters.append(waiter)
        try:
            packet = await asyncio.wait_for(future, min(timeout,15))
            return received[id(packet)]
        finally:
            r.waiters.remove(waiter)
            r.packet_observers.remove(observe)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def assign(self, target, group, *, timeout=120, expected_baseline=None):
        r = self.runtime
        integer(target, 32896, 65407, 'controller')
        integer(group, 256, 24575, 'group')
        if expected_baseline is not None:
            if not isinstance(expected_baseline, dict) or set(expected_baseline) != {'target', 'observed_ms', 'target_generation'}:
                raise ValueError('Expected exact reviewed baseline')
            integer(expected_baseline['target'], 256, 24575, 'baseline target')
            integer(expected_baseline['observed_ms'], 0, 2**63-1, 'baseline timestamp')
            integer(expected_baseline['target_generation'], 0, 2**63-1, 'baseline generation')
        device = r.core.devices.get(target)
        destination = next((g for g in r.store.inventory()['groups'] if g['group_id']==group),None)
        flag = 'anyplace_editor' if device and device.product_id == 81 else 'dimmer_editor'
        if not r.capabilities.get(flag) or not device or device.product_id not in (81,91):
            raise ValueError('Scoped dimmer programming disabled')
        if not destination or destination['protected']:
            raise ValueError('Expected ordinary known destination')
        if r.operation.locked() or not r.mesh_ready or r.store.journals(pending=True):
            raise RuntimeError('Mesh busy or reconciliation pending')
        baseline = r.native_observations.get(target)
        if (not baseline or not 0 <= r.clock()-baseline[1] <= 15000 or baseline[0]['target']==group
                or not 256 <= baseline[0]['target'] <= 24575):
            raise ValueError('Fresh original native target evidence required')
        if expected_baseline is not None and expected_baseline != {
                'target': baseline[0]['target'], 'observed_ms': baseline[1],
                'target_generation': baseline[0].get('target_generation')} :
            raise ValueError('Reviewed native baseline changed; review again before programming')
        if any(g['protected'] and g['group_id']==baseline[0]['target'] for g in r.store.inventory()['groups']):
            raise ValueError('Protected native assignment requires its managed lifecycle')
        generation = r.generation
        async with r.operation:
            detail = {'before':copy.deepcopy(baseline[0]),'product_id':device.product_id,
                      'reservation':copy.deepcopy(destination),'assignment_read_back':False}
            identifier = r.store.begin('dimmer_edit',target,detail)
            attempted = False
            try:
                wake_at = None
                if device.product_id==81:
                    wake_at = await self.wait_wake(target, baseline, generation, timeout)

                def admit():
                    if generation != r.generation or not r.mesh_ready or not r.transport.ready:
                        raise RuntimeError('Dimmer assignment belongs to an offline or earlier session')
                    latest = r.native_observations.get(target)
                    if (not latest or latest[0]['target']!=baseline[0]['target']
                            or not 0 <= r.clock()-baseline[1] <= 15000
                            or latest[0].get('target_generation')!=baseline[0].get('target_generation')):
                        raise ValueError('Original target evidence changed or expired')
                    if device.product_id==81 and not 0 <= r.clock()-wake_at <= 250:
                        raise RuntimeError('Wake dispatch admission expired')

                admit()
                with r.store.transaction():
                    token = r.store.get('dimmer_token',0)+1
                    if token>255:
                        raise RuntimeError('Dimmer token space exhausted')
                    r.store.set('dimmer_token',token)
                    detail['token'] = token
                    self.save(identifier,detail,'attempted')
                attempted = True
                admit()
                command = target.to_bytes(2,'little')+b'\x73\0\x1b\0\0'+bytes([token])+group.to_bytes(2,'big')+b'\x01\0\0'
                evidence = await self.observe_native(identifier,dispatch=command,timeout=timeout,admission=admit)
                detail['observed'] = evidence
                self.save(identifier,detail,'uncertain')
                self.observed[identifier] = r.clock()
                r.emit(r.discovery())
                return {'transaction':identifier,'desired_behavior_observed':True,'acceptance_required':True,
                        'assignment_read_back':False}
            except BaseException:
                try:
                    self.save(identifier,detail,'uncertain' if attempted else 'cancelled')
                finally:
                    if attempted:
                        await r.transport.disconnect()
                        r.mesh_ready = False
                        r.session()
                raise

    async def reconcile(self, identifier, *, timeout=120):
        r = self.runtime
        journal = self.receipt(identifier)
        if journal['detail'].get('control_schema') == SCHEMA:
            return await self.configuration.reconcile(identifier, timeout=timeout)
        if journal['kind']!='dimmer_edit' or journal['status']!='uncertain':
            raise ValueError('Expected uncertain dimmer assignment')
        if r.operation.locked() or not r.mesh_ready:
            raise RuntimeError('Mesh busy or offline')
        async with r.operation:
            detail = journal['detail']
            detail['observed'] = await self.observe_native(identifier,timeout=timeout)
            self.save(identifier,detail,'uncertain')
            self.observed[identifier] = r.clock()
            return {'desired_behavior_observed':True,'acceptance_required':True,'write_retried':False}

    def accept_observed(self, identifier):
        r = self.runtime
        journal = self.receipt(identifier)
        if journal['detail'].get('control_schema') == SCHEMA:
            return self.configuration.accept(identifier)
        if (r.operation.locked() or not r.mesh_ready or not r.transport.ready
                or journal['kind']!='dimmer_edit' or journal['status']!='uncertain'
                or identifier not in self.observed or not 0 <= r.clock()-self.observed[identifier] <= 15000):
            raise ValueError('Fresh current-process behavior observation required')
        latest = r.native_observations.get(journal['target'])
        if not latest or latest[0]['target']!=journal['detail']['reservation']['group_id']:
            raise ValueError('Latest native behavior no longer matches')
        self.save(identifier,journal['detail'],'verified')
        r.emit(r.discovery())
        return {'accepted_observed_behavior':True,'assignment_read_back':False}
