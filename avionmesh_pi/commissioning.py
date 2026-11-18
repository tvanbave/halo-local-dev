"""Durable ordinary claim engine. No automatic controller assignments are implied."""
import asyncio
import copy
import json
import uuid

from .identity import (advertised_identity, ping_token, wired_reset_profile,
                       bootstrap_reset_profile, pre_key_claim_profile, rl4_public_key_claim_profile, a19_public_key_claim_profile,
                       rl4_pre_network_key_claim_profile)
from .association import (AssociationError, Association, MASP_RESPONSE_OPCODES, discovery_request,
                          open_envelope, uuid_hash)
from .bridge import CONTROLLERS, LIGHTS, Bridge, load_devices
from .protocol import ping_command, ping_match

REPLY_WAIT = 5        # seconds per association reply
SLOW_REPLY_WAIT = 15  # DeviceID and network-key replies
ASSOCIATION_DEADLINE = 60  # whole 12-request exchange
ANYPLACE_WAKE_WAIT = 120  # one owner-coordinated wake, no automatic retry
BATTERY_CLAIM_WAKE_WAIT = 120  # explicit saved-claim proof only


class ClaimValidationError(ValueError):
    """User-safe admission error raised before starting association."""


class AnyplaceWakeTimeout(asyncio.TimeoutError):
    """No fresh wake was received; the final Ping was not sent."""


CLAIM_HISTORY_CAPACITY = 128


class Commissioning:
    def __init__(self, runtime):
        self.runtime = runtime
        self.discovered = {}
        self.candidates = {}
        self.scan_truncated = False
        self.discovery_generation = runtime.generation
        self.batch_owner = None
        self.progress = {}
        self.wake_sequences = {}
        runtime.session_observers.append(self._invalidate_discovery)

    def remaining_claims(self):
        count = self.runtime.store.db.execute("SELECT COUNT(*) FROM journals WHERE kind='claim'").fetchone()[0]
        return max(0, CLAIM_HISTORY_CAPACITY - count)

    def _invalidate_discovery(self):
        self.discovered.clear()
        self.discovery_generation = -1

    def _batch_guard(self):
        if getattr(self.runtime, 'maintenance', False):
            raise RuntimeError('Bridge is restarting for restore')
        if self.batch_owner is not None and self.batch_owner is not asyncio.current_task():
            raise RuntimeError('A commissioning batch is active')

    @staticmethod
    def _identity(identity):
        if not isinstance(identity, str) or len(identity) != 32:
            raise ValueError('Expected exact full UUID')
        value = bytes.fromhex(identity)
        if len(value) != 16 or not any(value) or value.hex() != identity:
            raise ValueError('Expected canonical nonzero full UUID')
        return value

    def candidate_snapshot(self):
        now = self.runtime.clock()
        return [{'uuid': identity, 'uuid_hash': uuid_hash(bytes.fromhex(identity)),
                 'model_verified': False, 'identity': advertised_identity(identity),
                 'ambiguous_hash': uuid_hash(bytes.fromhex(identity)) in self.runtime.store.get('unpaired_hash_conflicts', []),
                 'age_ms': max(0, now - when),
                 'fresh': self.discovery_generation == self.runtime.generation
                          and identity in self.discovered and 0 <= now - self.discovered[identity] <= 30000}
                for identity, when in self.candidates.items()]

    def _sleeping_claim_admission(self, journal, generation, product=81):
        """A wake is not claim proof; retain the exact completed association authority."""
        r, detail = self.runtime, journal['detail']
        identity = self._identity(detail.get('uuid'))
        if (journal['kind'] != 'claim' or journal['status'] not in ('attempted', 'uncertain')
                or product not in (81, 126) or detail.get('product_id') != product
                or detail.get('association_complete') is not True
                or detail.get('inventory_complete') is not True
                or detail.get('uuid_hash') != uuid_hash(identity)):
            raise ValueError('Exact completed sleeping-controller association checkpoint required')
        if generation != r.generation or not r.mesh_ready or not r.transport.ready:
            raise RuntimeError('Sleeping-controller claim proof belongs to an offline or earlier session')
        current = next((j for j in r.store.journals() if j['id'] == journal['id']), None)
        if current != journal or journal['id'] in r.store.get('retired_claims', []):
            raise ValueError('Claim receipt changed during proof')
        if (journal['target'] in r.core.devices
                or journal['target'] in r.store.get('retired_addresses', [])
                or any(d['device_id'] == journal['target'] for d in r.store.inventory()['devices'])
                or len(r.store.inventory()['devices']) >= 512):
            raise ValueError('Reserved claim address no longer exclusive')
        if any(j['id'] != journal['id'] for j in r.store.journals(pending=True)):
            raise RuntimeError('Resolve competing operations first')

    async def _prove_anyplace(self, journal, generation):
        return await self._prove_sleeping_claim(journal, generation, 81)

    async def _prove_sleeping_claim(self, journal, generation, product):
        """Wait once for a fresh wake, then send one nonce-correlated read-only Ping."""
        r, target = self.runtime, journal['target']
        self._sleeping_claim_admission(journal, generation, product)
        battery = product == 126
        max_receive_age = 1000 if battery else 250
        fingerprint = journal['detail']['uuid_hash']
        def phase(value):
            self.progress[target] = {'phase': value, 'uuid_hash': fingerprint}
        phase('waiting_for_wake')
        future = asyncio.get_running_loop().create_future()
        since, received = r.clock(), {}
        def observe(packet, when):
            if (future.done() or received or generation != r.generation
                    or not since <= when <= r.clock() or r.clock() - when > max_receive_age
                    or packet.crypto_source != target or packet.opcode != 115
                    or len(packet.payload) != 7 or packet.payload[:2] != b'\x0b\x1c'):
                return
            previous = self.wake_sequences.get(target)
            if (previous is not None and previous[0] == generation
                    and not 0 < ((packet.sequence - previous[1]) & 0xffffff) < 0x800000):
                return
            received[id(packet)] = when
            # Consume at receipt, even if cancellation wins before the task resumes.
            self.wake_sequences[target] = (generation, packet.sequence)
        waiter = (lambda packet: id(packet) in received, future, since)
        r.packet_observers.append(observe)
        r.waiters.append(waiter)
        try:
            # wait() propagates cancellation even when a wake completed in the
            # same loop turn; wait_for() on older test runtimes can swallow it.
            done, _ = await asyncio.wait((future,), timeout=BATTERY_CLAIM_WAKE_WAIT if battery else ANYPLACE_WAKE_WAIT)
            if not done:
                if battery:
                    raise asyncio.TimeoutError('Battery claim wake was not observed')
                raise AnyplaceWakeTimeout('Anyplace claim wake was not observed')
            wake = future.result()
            wake_at = received[id(wake)]
            if battery:
                phase('settling')
                await asyncio.sleep(max(0, 150 - (r.clock() - wake_at)) / 1000)
            def admit():
                self._sleeping_claim_admission(journal, generation, product)
                if not (150 if battery else 0) <= r.clock() - wake_at <= (1500 if battery else 250):
                    raise RuntimeError('Sleeping-controller claim wake dispatch admission expired')
            admit()
            token = ping_token(r.store)
            admit()
            phase('verifying')
            packet = await r.exchange(ping_command(target, token),
                lambda p: ping_match(p, target, token)
                and 0 < ((p.sequence - wake.sequence) & 0xffffff) < 0x800000,
                timeout=5, generation=generation, admission=admit)
            self._sleeping_claim_admission(journal, generation, product)
            return packet
        except BaseException:
            phase('needs_review')
            raise
        finally:
            r.waiters.remove(waiter)
            r.packet_observers.remove(observe)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()


    async def discover(self, seconds=5):
        r = self.runtime
        self._batch_guard()
        if type(seconds) not in (int, float) or not 0 <= seconds <= 30:
            raise ValueError('Discovery duration must be between zero and 30 seconds')
        r.store.require_tx_ready()
        if r.operation.locked() or not r.mesh_ready or not r.transport.ready:
            raise RuntimeError("Mesh not available for discovery")
        async with r.operation:
            self._batch_guard()
            self.discovered.clear()
            self.scan_truncated = False
            generation, started = r.generation, r.clock()
            complete = False
            known = r.store.get('unpaired_identity_hashes', {})
            conflicts = set(r.store.get('unpaired_hash_conflicts', []))
            history_full = r.store.get('unpaired_history_full', False)
            def observe(frame, when):
                nonlocal history_full
                if generation != r.generation or not started <= when <= r.clock():
                    return
                try:
                    decoded = open_envelope(frame)
                except ValueError:
                    return
                if len(decoded) >= 17 and decoded[0] == 1 and any(decoded[1:17]):
                    identity = decoded[1:17].hex()
                    fingerprint = uuid_hash(decoded[1:17])
                    prior = known.get(str(fingerprint))
                    if prior is not None and prior != identity:
                        conflicts.add(fingerprint)
                    elif prior is None:
                        if len(known) >= 512:
                            history_full = True
                        else:
                            known[str(fingerprint)] = identity
                    if identity not in self.discovered and len(self.discovered) >= 128:
                        self.scan_truncated = True
                        return
                    self.discovered[identity] = when
            r.raw_observers.append(observe)
            try:
                await r.transport.send(discovery_request())
                await asyncio.sleep(seconds)
                if generation != r.generation or not r.mesh_ready or not r.transport.ready:
                    raise RuntimeError('Discovery crossed a disconnected session')
                for identity, when in self.discovered.items():
                    if identity not in self.candidates and len(self.candidates) >= 128:
                        oldest = min(self.candidates, key=self.candidates.get)
                        self.candidates.pop(oldest)
                    self.candidates[identity] = when
                self.discovery_generation = generation
                complete = True
            finally:
                r.raw_observers.remove(observe)
                try:
                    # A known collision stays unsafe even if later scans omit a
                    # device or the bounded display cache evicts it.
                    with r.store.transaction():
                        r.store.set('unpaired_identity_hashes', known)
                        r.store.set('unpaired_hash_conflicts', sorted(conflicts))
                        r.store.set('unpaired_history_full', history_full)
                except BaseException:
                    self._invalidate_discovery()
                    raise
                if not complete:
                    self._invalidate_discovery()
        return [{"uuid": identity, "uuid_hash": uuid_hash(bytes.fromhex(identity)),
                 "model_verified": False, "identity": advertised_identity(identity)} for identity in self.discovered]

    async def claim(self, identity, name, product, *, inventory_complete=False, setup_requested=False, refresh_identity=False):
        r = self.runtime
        self._batch_guard()
        r.store.require_tx_ready()
        if len(r.core.devices)>=512:
            raise ClaimValidationError('Device capacity exhausted before association')
        if type(inventory_complete) is not bool or type(setup_requested) is not bool or type(refresh_identity) is not bool:
            raise ClaimValidationError('Invalid claim flags')
        if not inventory_complete:
            raise ClaimValidationError("Complete inventory/reservations are required before address allocation")
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or type(product) is not int or product not in {**LIGHTS, **CONTROLLERS}:
            raise ClaimValidationError("Invalid device metadata")
        uuid_bytes = self._identity(identity)
        bootstrap = getattr(r, 'bootstrap', None)
        first_bootstrap = bootstrap is not None and bootstrap.needs_first_claim
        automatic_bootstrap = first_bootstrap and getattr(bootstrap, 'automatic', False) is True
        direct_bootstrap = first_bootstrap and not automatic_bootstrap
        if direct_bootstrap:
            selected = bootstrap.selected_address
            if (not selected or uuid_bytes[:6] != bytes.fromhex(selected.replace(':', ''))[::-1]
                    or product != 93 or advertised_identity(identity).get('candidate_model') != 93):
                raise ClaimValidationError('First claim must be the selected factory-reset RL4 GATT device')
        fingerprint = uuid_hash(uuid_bytes)
        if r.operation.locked() or not r.mesh_ready or not r.transport.ready:
            raise RuntimeError('Mesh unavailable')
        if r.store.journals(pending=True):
            raise ClaimValidationError('Resolve pending transactions before commissioning')
        if refresh_identity:
            # Broadcast replies may collide when many lamps reset together.
            # Retry discovery only, never association; require a fresh exact target.
            for _ in range(3):
                await self.discover()
                if identity in self.discovered:
                    break
        if (self.scan_truncated or self.discovery_generation != r.generation or identity not in self.discovered
                or not 0 <= r.clock() - self.discovered[identity] <= 30000):
            raise ClaimValidationError("A fresh, complete observation of the selected light is required. No pairing was started. Leave it powered and scan again.")
        if (r.store.get('unpaired_history_full', False) or fingerprint in r.store.get('unpaired_hash_conflicts', [])
                or r.store.get('unpaired_identity_hashes', {}).get(str(fingerprint)) != identity
                or sum(uuid_hash(bytes.fromhex(item)) == fingerprint for item in self.discovered) != 1):
            raise ClaimValidationError("Ambiguous discovery hash")
        if r.operation.locked() or not r.mesh_ready:
            raise RuntimeError("Mesh unavailable")
        if r.store.journals(pending=True):
            raise ClaimValidationError("Resolve pending transactions before commissioning")
        for old in r.store.journals():
            cancelled_unattempted = (old['status']=='cancelled' and old['detail'].get('cancelled_before_dispatch') is True
                and old['detail'].get('uuid')==identity and not old['detail'].get('association_complete'))
            if (old["kind"] == "claim" and old["detail"].get("uuid_hash") == fingerprint
                    and not cancelled_unattempted
                    and (old['id'] not in r.store.get('retired_claims', []) or old['detail']['uuid'] != identity)):
                raise ClaimValidationError("Identity already has a retained claim; reconciliation required")
        if self.remaining_claims() == 0:
            raise ClaimValidationError("Ordinary claim lifetime capacity exhausted")
        used = set(r.core.devices) | {j["target"] for j in r.store.journals()}
        target = max([32895, *used]) + 1
        if target == 32898:
            target += 1  # never allocate the retained legacy Accessory test address
        if target > 65407:
            raise ClaimValidationError("Address space exhausted")
        async with r.operation:
            self._batch_guard()
            generation = r.generation
            protocol = Association(uuid_bytes, target, r.key)
            failure_reason = None
            started_at = r.clock()
            steps = []
            counter_names = ('low', 'high', 'paired_high', 'standalone_high', 'standalone_low',
                'expired_low', 'replaced_low', 'invalid_notification', 'queue_drop',
                'tx_low_attempted', 'tx_low_submitted', 'tx_low_failed',
                'tx_high_attempted', 'tx_high_submitted', 'tx_high_failed')
            counters_before = {k: getattr(r.transport, 'counts', {}).get(k, 0) for k in counter_names}
            def timing_evidence():
                counters = getattr(r.transport, 'counts', {})
                return {'elapsed_ms': max(0, r.clock() - started_at),
                    'steps': [dict(step) for step in steps],
                    'transport_delta': {k: max(0, counters.get(k, 0) - counters_before[k]) for k in counter_names}}
            receive_counts = {'ignored_frames': 0, 'out_of_step_frames': 0,
                              'duplicate_frames': 0, 'queued_target_frames': 0}
            record = r.store.begin("claim", target, {"uuid": identity, "uuid_hash": fingerprint,
                "name": name, "product_id": product, "model_verified": False, 'setup_requested': setup_requested,'inventory_complete':True,'association_complete':False,'association_phase':'prepared','submissions_attempted':0,'submissions_accepted':0,
                'direct_bootstrap':direct_bootstrap,
                'fresh_mesh_first_claim':automatic_bootstrap,
                # Which GATT relay carried this attempt (diagnostic, not identity proof).
                'relay_address':getattr(r.transport, 'address', None),
                'relay_preferred':getattr(r.transport, 'relay_preferred', None),
                'bootstrap_address':':'.join(f'{b:02X}' for b in uuid_bytes[:6][::-1]) if automatic_bootstrap else (bootstrap.selected_address if direct_bootstrap else None)})
            async def dispatch(frame):
                nonlocal failure_reason
                self._batch_guard()
                r.store.require_tx_ready()
                if generation != r.generation or not r.transport.ready:
                    failure_reason = ('association_session_changed' if generation != r.generation
                                      else 'association_transport_unavailable')
                    raise RuntimeError('Association session changed before submission')
                step = {'state': protocol.state, 'chunk': protocol.chunk,
                        'started_ms': max(0, r.clock() - started_at)}
                steps.append(step)
                with r.store.transaction():
                    receipt = next(j for j in r.store.journals() if j['id'] == record)
                    detail = receipt['detail']
                    detail['association_phase'] = protocol.state
                    detail['submissions_attempted'] += 1
                    r.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(detail), record))
                step['write_started_ms'] = max(0, r.clock() - started_at)
                try:
                    await r.transport.send(frame)
                    step['write_finished_ms'] = max(0, r.clock() - started_at)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failure_reason = 'association_transport_write_failed'
                    raise
                if generation != r.generation:
                    failure_reason = 'association_session_changed'
                    raise RuntimeError('Association session changed during submission')
                with r.store.transaction():
                    receipt = next(j for j in r.store.journals() if j['id'] == record)
                    receipt['detail']['submissions_accepted'] += 1
                    r.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(receipt['detail']), record))
            def checkpoint_complete():
                # Durable proof is the observed final ACK, not a successful send.
                with r.store.transaction():
                    receipt = next(j for j in r.store.journals() if j['id'] == record)
                    receipt['detail']['association_complete'] = True
                    receipt['detail']['completion_generation'] = generation
                    receipt['detail']['device_id_ack_hash_matched'] = protocol.device_id_ack_hash_matched
                    r.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(receipt['detail']), record))
            queue = asyncio.Queue(maxsize=64)
            overflow = False
            step_seen = {'step': None, 'payloads': set()}
            def count(field):
                receive_counts[field] = min(65535, receive_counts[field] + 1)
            def observe(frame, when):
                nonlocal overflow
                try:
                    if generation == r.generation:
                        # The shared relay also carries ordinary mesh traffic,
                        # discovery and other devices' MASP. Authenticate and
                        # correlate BEFORE enqueueing so unrelated bursts cannot
                        # consume the bounded queue and hide a valid reply.
                        try:
                            decoded = open_envelope(frame)
                        except ValueError:
                            count('ignored_frames')
                            return
                        if (len(decoded) < 5 or decoded[1:5] != protocol.hash_bytes
                                or decoded[0] not in (3, 5, 7, 9, 11, 13)):
                            count('ignored_frames')
                            return
                        # The protocol only moves forward, and the next request
                        # is sent only after its predecessor's reply is processed.
                        # A reply that does not match the current step (earlier
                        # step or chunk, or a step not yet requested) can never
                        # be accepted by protocol.process, so it must not consume
                        # queue capacity. Exact repeats of an already queued reply
                        # for this step are equally redundant. Distinct replies
                        # for the current step still fill the queue and overflow
                        # fails closed.
                        expected = MASP_RESPONSE_OPCODES.get(protocol.state)
                        if (expected is None or decoded[0] != expected
                                or (protocol.state in ('public_key', 'network_key')
                                    and (len(decoded) < 6 or decoded[5] != protocol.chunk))):
                            count('out_of_step_frames')
                            return
                        step = (protocol.state, protocol.chunk)
                        if step_seen['step'] != step:
                            step_seen['step'], step_seen['payloads'] = step, set()
                        if decoded in step_seen['payloads']:
                            count('duplicate_frames')
                            return
                        # Target may itself be the GATT relay and reboot as soon
                        # as it accepts the key. Preserve a valid final ACK in
                        # this synchronous receive turn, before session invalidation
                        # can outrun the asynchronous queue consumer.
                        if not overflow and not queue.full() and protocol.state == 'network_key' and protocol.chunk == 1:
                            if (len(decoded) >= 6 and decoded[0] == 13
                                    and decoded[1:5] == protocol.hash_bytes and decoded[5] == 1):
                                protocol.process(frame)
                                if protocol.state == 'complete':
                                    checkpoint_complete()
                        queue.put_nowait(frame)
                        if steps and 'reply_observed_ms' not in steps[-1]:
                            steps[-1]['reply_observed_ms'] = max(0, when - started_at)
                            steps[-1]['reply_processed_ms'] = max(0, r.clock() - started_at)
                        count('queued_target_frames')
                        if len(step_seen['payloads']) < 64:
                            step_seen['payloads'].add(decoded)
                except asyncio.QueueFull:
                    overflow = True
            r.raw_observers.append(observe)
            try:
                r.store.transition(record, "prepared", "attempted")
                await dispatch(protocol.start())
                async def exchange():
                    nonlocal failure_reason
                    while protocol.state != "complete":
                        if overflow or not r.transport.ready or generation != r.generation:
                            failure_reason = ('association_receive_overflow' if overflow
                                              else 'association_session_changed' if generation != r.generation
                                              else 'association_transport_unavailable')
                            raise RuntimeError("Association stream lost")
                        try:
                            # Lights may take longer to answer the address and key steps
                            # (ESP allows 30 s overall). Waiting longer sends nothing extra.
                            frame = await asyncio.wait_for(queue.get(), SLOW_REPLY_WAIT
                                if protocol.state in ('device_id', 'network_key') else REPLY_WAIT)
                        except asyncio.TimeoutError:
                            failure_reason = ('association_session_changed' if generation != r.generation
                                              else 'association_transport_unavailable' if not r.transport.ready
                                              else 'association_response_timeout')
                            raise
                        try:
                            open_envelope(frame)
                        except ValueError:
                            continue
                        following = protocol.process(frame)
                        if following is not None:
                            await dispatch(following)
                try:
                    await asyncio.wait_for(exchange(), ASSOCIATION_DEADLINE)
                except asyncio.TimeoutError:
                    if failure_reason is None:
                        failure_reason = 'association_deadline_exceeded'
                    raise
                if generation != r.generation:
                    failure_reason = 'association_session_changed'
                    raise RuntimeError('Association crossed a session boundary')
                # Persist the completed cryptographic exchange before attempting proof.
                checkpoint_complete()
                with r.store.transaction():
                    receipt = next(j for j in r.store.journals() if j['id'] == record)
                    receipt['detail']['association_timing'] = timing_evidence()
                    r.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(receipt['detail']), record))
                # Protocol ACKs alone are insufficient: authenticate a fresh MCP reply.
                try:
                    if product == 81:
                        proof = next(j for j in r.store.journals() if j['id'] == record)
                        await self._prove_anyplace(proof, generation)
                    else:
                        token=ping_token(r.store)
                        await r.exchange(ping_command(target, token), lambda p: ping_match(p, target, token), timeout=5, generation=generation)
                except asyncio.TimeoutError as error:
                    failure_reason = ('association_wake_timeout' if isinstance(error, AnyplaceWakeTimeout)
                                      else 'association_ping_timeout')
                    raise
                data = copy.deepcopy(r.store.inventory())
                data["devices"].append({"device_id": target, "product_id": product, "name": name, "exposed": False})
                # Atomic acceptance and journal completion: no ghost discovery on failure.
                with r.store.transaction():
                    if product == 81:
                        self._sleeping_claim_admission(proof, generation)
                    r.store.set("inventory", data)
                    r.store.db.execute("UPDATE journals SET status='verified' WHERE id=? AND status='attempted'", (record,))
                if product == 81:
                    self.progress[target] = {'phase': 'verified', 'uuid_hash': fingerprint}
                from .lifecycle import reload_inventory
                reload_inventory(r)
                self.discovered.pop(identity, None)
                self.candidates.pop(identity, None)
                return {"device_id": target, "associated": True, "exposed": False,
                        "model_verified": False, "controller_setup_complete": False}
            except BaseException as error:
                current = None
                try:
                    try:
                        current = next(j for j in r.store.journals() if j['id'] == record)
                    except Exception:
                        r.last_error = 'Claim receipt needs storage recovery; do not retry'
                    if current is not None and current['status'] == 'attempted':
                        # Diagnostic failure must not erase the original failure
                        # or cancellation, nor prevent durable quarantine. Each
                        # persistence attempt is independent. If storage itself
                        # fails, the already durable attempted receipt remains
                        # pending and blocks retry until storage recovery.
                        try:
                            with r.store.transaction():
                                current['detail']['failure_code'] = (
                                    error.code if isinstance(error, AssociationError)
                                    else 'association_cancelled' if isinstance(error, asyncio.CancelledError)
                                    else failure_reason or 'association_interrupted')
                                current['detail']['association_timing'] = timing_evidence()
                                current['detail']['failure_observation'] = {
                                    'state': protocol.state, 'expected_chunk': protocol.chunk,
                                    'same_session': generation == r.generation,
                                    'transport_ready': r.transport.ready,
                                    'queue_overflow': overflow, **receive_counts}
                                if isinstance(error, AssociationError):
                                    current['detail']['confirm_type'] = protocol.confirm_type
                                if protocol.device_id_ack_hash_matched is not None:
                                    current['detail']['device_id_ack_hash_matched'] = protocol.device_id_ack_hash_matched
                                r.store.db.execute('UPDATE journals SET detail=? WHERE id=?', (json.dumps(current['detail']), record))
                        except Exception:
                            r.last_error = 'Claim failure diagnostics could not be saved; retained attempt needs review'
                        if current['status'] == 'attempted':
                            try:
                                r.store.transition(record, "attempted", "uncertain")
                            except Exception:
                                r.last_error = 'Claim receipt needs storage recovery; do not retry'
                finally:
                    try:
                        await r.transport.disconnect()  # isolate delayed association fragments
                        client = getattr(r.transport, 'client', None)
                        if not r.transport.ready and not (client and client.is_connected):
                            r.claim_isolated.add(record)
                    except Exception:
                        r.last_error = 'Claim transport cleanup needs review'
                    finally:
                        r.mesh_ready = False
                        try:
                            r.session()
                        except Exception:
                            r.last_error = 'Claim cleanup needs publication reconciliation'
                if current is not None and current['status'] == 'verified' and isinstance(error, Exception):
                    # The durable association already succeeded. Never report it
                    # as a failed claim that could invite a second association.
                    return {"device_id": target, "associated": True, "exposed": False,
                            "model_verified": False, "controller_setup_complete": False,
                            "publication_pending": True, "requires_review": True}
                raise
            finally:
                r.raw_observers.remove(observe)

    async def reconcile(self, identifier):
        """Finish only a retained complete ECDH exchange; never retry association."""
        r=self.runtime
        r.store.require_tx_ready()
        journal=next((j for j in r.store.journals() if j['id']==identifier),None)
        if (not journal or journal['kind']!='claim' or journal['status']!='uncertain'
                or journal['detail'].get('association_complete') is not True
                or journal['detail'].get('inventory_complete') is not True):
            raise ValueError('A durable completed association checkpoint is required')
        if r.operation.locked() or not r.mesh_ready or any(j['id']!=identifier for j in r.store.journals(pending=True)):
            raise RuntimeError('Resolve competing operations first')
        target=journal['target']
        if len(r.core.devices)>=512:raise ValueError('Device capacity exhausted before recovery proof')
        if target in r.core.devices or target in r.store.get('retired_addresses',[]):
            raise ValueError('Reserved claim address no longer exclusive')
        async with r.operation:
            proof_generation = r.generation
            try:
                if journal['detail'].get('product_id') in (81, 126):
                    await self._prove_sleeping_claim(journal, proof_generation, journal['detail']['product_id'])
                else:
                    token=ping_token(r.store)
                    await r.exchange(ping_command(target,token),lambda p:ping_match(p,target,token),timeout=5)
            except asyncio.TimeoutError:
                return {'verified':False,'requires_review':True,'association_retried':False}
            with r.store.transaction():
                current=next(j for j in r.store.journals() if j['id']==identifier)
                if current!=journal:
                    raise ValueError('Claim receipt changed during proof')
                if journal['detail'].get('product_id') in (81, 126):
                    self._sleeping_claim_admission(journal, proof_generation, journal['detail']['product_id'])
                data=copy.deepcopy(r.store.inventory())
                data['devices'].append({'device_id':target,'product_id':journal['detail']['product_id'],
                                       'name':journal['detail']['name'],'exposed':False})
                r.store.set('inventory',data)
                r.store.db.execute("UPDATE journals SET status='verified' WHERE id=? AND status='uncertain'",(identifier,))
            if journal['detail'].get('product_id') in (81, 126):
                self.progress[target] = {'phase': 'verified', 'uuid_hash': journal['detail']['uuid_hash']}
            from .lifecycle import reload_inventory
            result = {'verified':True,'associated':True,'device_id':target,'model_verified':False,
                      'association_retried':False,'setup_automatically_resumed':False}
            try:
                reload_inventory(r)
            except Exception:
                r.mesh_ready = False
                result.update(publication_pending=True, requires_review=True)
            return result

    async def recover_safe_pending(self, identifier):
        """Bounded observation-only recovery; never associate, reset, or reclaim.

        The caller must invoke this after the claim worker has released its lock.
        Transport reconnection remains the service's responsibility.
        """
        r = self.runtime
        attempts = 0
        def outcome(reason, **extra):
            return dict(outcome=reason, transaction=identifier, ping_attempts=attempts,
                        association_retried=False, reset_dispatched=False,
                        automatic_reclaim=False, **extra)
        if getattr(self, '_safe_recovery_active', False):
            return outcome('recovery_busy', requires_review=True)
        self._safe_recovery_active = True
        try:
            self._batch_guard()
            r.store.require_tx_ready()
            original = next((j for j in r.store.journals(pending=True) if j['id'] == identifier), None)
            if not original or original['kind'] != 'claim' or original['status'] != 'uncertain':
                return outcome('not_pending_claim', requires_review=True)
            complete = (original['detail'].get('association_complete') is True
                        and original['detail'].get('inventory_complete') is True)
            if complete and original['detail'].get('product_id') in (81, 126):
                return outcome('owner_wake_required', requires_review=True)
            if not complete and not (pre_key_claim_profile(original) or rl4_public_key_claim_profile(original)
                                     or a19_public_key_claim_profile(original) or rl4_pre_network_key_claim_profile(original)):
                return outcome('unsafe_checkpoint', requires_review=True)
            waits = 0
            for _ in range(3 if complete else 1):
                while True:
                    self._batch_guard()
                    current = next((j for j in r.store.journals() if j['id'] == identifier), None)
                    if current != original:
                        return outcome('receipt_changed', requires_review=True)
                    if r.operation.locked() or any(j['id'] != identifier for j in r.store.journals(pending=True)):
                        return outcome('competing_operation', requires_review=True)
                    if r.mesh_ready and r.transport.ready:
                        break
                    if waits >= 20:
                        return outcome('transport_unavailable', requires_review=True)
                    waits += 1
                    await asyncio.sleep(0.5)
                if complete:
                    attempts += 1
                    result = await self.reconcile(identifier)
                    if result.get('verified') is True:
                        return outcome('verified', result=result,
                                       requires_review=result.get('requires_review', False))
                else:
                    result = await self.reconcile_pre_key(identifier)
                    retired = result.get('pre_key_retired') is True
                    return outcome('pre_key_retired' if retired else 'unpaired_not_observed',
                                   result=result, requires_review=not retired or result.get('requires_review', False))
            return outcome('proof_unavailable', requires_review=True)
        except (ValueError, RuntimeError, asyncio.TimeoutError):
            # Do not turn uncertain transport errors into repeated writes or
            # expose arbitrary exception text. Cancellation deliberately propagates.
            return outcome('recovery_blocked', requires_review=True)
        finally:
            self._safe_recovery_active = False

    async def claim_many(self, rows, *, inventory_complete=False):
        """Serial RL4 claims with a new exact-identity scan before every row."""
        r = self.runtime
        self._batch_guard()
        if not r.capabilities.get('bulk_claim') or inventory_complete is not True:
            raise ValueError('Bulk claim requires explicit capability and complete inventory')
        if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
            raise ValueError('Expected one to 32 named RL4 devices')
        identities, hashes = set(), set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'uuid', 'name', 'product_id'}:
                raise ValueError('Expected exact claim fields')
            value = self._identity(row['uuid'])
            fingerprint = uuid_hash(value)
            if (row['uuid'] in identities or fingerprint in hashes or type(row['product_id']) is not int
                    or row['product_id'] != 93 or advertised_identity(row['uuid'])['candidate_model'] != 93
                    or not isinstance(row['name'], str) or not 1 <= len(row['name']) <= 128):
                raise ValueError('Invalid or conflicting batch identity or metadata')
            identities.add(row['uuid'])
            hashes.add(fingerprint)
        if len(r.core.devices) + len(rows) > 512 or len(rows) > self.remaining_claims():
            raise ValueError('Insufficient device or claim capacity for batch')
        rows = copy.deepcopy(rows)
        results = []
        self.batch_owner = asyncio.current_task()
        try:
            for row in rows:
                try:
                    result = await self.claim(row['uuid'], row['name'], row['product_id'],
                                              inventory_complete=True, refresh_identity=True)
                except (ValueError, RuntimeError, asyncio.TimeoutError):
                    results.append({'uuid': row['uuid'], 'associated': False, 'requires_review': True,
                                    'retry_allowed': False})
                    break
                results.append(dict(result, uuid=row['uuid']))
                if result.get('requires_review'):
                    break
        finally:
            self.batch_owner = None
        return {'results': results, 'remaining': [row['uuid'] for row in rows[len(results):]],
                'complete': len(results) == len(rows) and all(row['associated'] and not row.get('requires_review') for row in results),
                'automatic_resume': False, 'group_setup_complete': False}

    async def reconcile_pre_key(self, identifier):
        return await self._reconcile_claim_retirement(identifier, False, pre_key=True)

    async def reconcile_owner_reset(self, identifier, owner_reset):
        return await self._reconcile_claim_retirement(identifier, owner_reset)

    def _public_key_retirement_scope(self, journal, inventory):
        """Recheck durable ownership/history; no new proof is inferred from a scan."""
        r = self.runtime
        if (not (rl4_public_key_claim_profile(journal) or a19_public_key_claim_profile(journal)
                 or rl4_pre_network_key_claim_profile(journal))
                or r.capabilities.get('ordinary_claim') is not True
                or journal['id'] not in r.claim_isolated
                or r.store.inventory() != inventory
                or journal['target'] in r.core.devices
                or journal['target'] in r.store.get('retired_addresses', [])
                or any(d['device_id'] == journal['target'] for d in inventory['devices'])):
            return False
        identity, fingerprint = journal['detail']['uuid'], journal['detail']['uuid_hash']
        receipts = r.store.journals()
        for other in receipts:
            if other['id'] == journal['id']:
                continue
            if other['target'] == journal['target']:
                return False
            if other['kind'] != 'claim':
                continue
            detail = other['detail']
            if detail.get('uuid') != identity and detail.get('uuid_hash') != fingerprint:
                continue
            if (a19_public_key_claim_profile(journal)
                    and self._verified_a19_removal(other, identity, fingerprint, receipts, inventory)):
                continue
            if (detail.get('uuid') != identity or detail.get('uuid_hash') != fingerprint
                    or other['status'] != 'retired'
                    or other['id'] not in r.store.get('retired_claims', [])
                    or other['target'] not in r.store.get('retired_addresses', [])):
                return False
            # A prior same-identity attempt is safe only if its own immutable
            # receipt has an actual verified no-key retirement proof. A reset,
            # removal, status field or tombstone alone is insufficient.
            proven = False
            for proof in receipts:
                p = proof['detail']
                original = p.get('original_receipt')
                if (proof['kind'] != 'claim_pre_key' or proof['status'] != 'verified'
                        or proof['target'] != other['target'] or not isinstance(original, dict)
                        or dict(original, status='retired') != other
                        or not (pre_key_claim_profile(original) or rl4_public_key_claim_profile(original)
                                or a19_public_key_claim_profile(original) or rl4_pre_network_key_claim_profile(original))
                        or p.get('uuid') != identity or p.get('uuid_hash') != fingerprint
                        or p.get('pre_key_only') is not True or p.get('owner_reset_declared') is not False
                        or p.get('reset_dispatched') is not False or p.get('association_retried') is not False
                        or p.get('fresh_unpaired_observation') is not True
                        or p.get('recovery_schema') not in ('claim-retirement-v1', 'claim-public-key-retirement-v1',
                                                            'claim-pre-network-key-retirement-v1')):
                    continue
                proven = True
                break
            if not proven:
                return False
        return True

    def _verified_a19_removal(self, claim, identity, fingerprint, receipts, inventory):
        """A completed prior A19 claim needs its exact-UUID removal proof.

        Cloud/owner-confirmed removal has no prior UUID proof and never qualifies.
        A retirement flag, disconnected target, name, MAC or hash alone cannot
        authorize reuse of an identity from a completed earlier association.
        """
        r=self.runtime;d=claim.get('detail',{});target=claim.get('target')
        if (claim.get('kind')!='claim' or claim.get('status')!='verified'
                or type(target) is not int or not 32896<=target<=65407 or target==32898
                or claim.get('id') not in r.store.get('retired_claims',[])
                or target not in r.store.get('retired_addresses',[])
                or target in r.core.devices or any(row['device_id']==target for row in inventory['devices'])
                or d.get('uuid')!=identity or type(d.get('uuid_hash')) is not int or d['uuid_hash']!=fingerprint
                or type(d.get('product_id')) is not int or d['product_id']!=134
                or d.get('association_complete') is not True or d.get('inventory_complete') is not True
                or d.get('association_phase')!='network_key'
                or type(d.get('submissions_attempted')) is not int or d['submissions_attempted']!=12
                or type(d.get('submissions_accepted')) is not int or d['submissions_accepted']!=12):
            return False
        for proof in receipts:
            p=proof.get('detail',{})
            if (proof.get('kind')!='remove' or proof.get('status')!='verified' or proof.get('target')!=target
                    or p.get('claim')!=claim['id'] or p.get('uuid')!=identity
                    or type(p.get('uuid_hash')) is not int or p['uuid_hash']!=fingerprint
                    or type(p.get('product_id')) is not int or p['product_id']!=134
                    or p.get('identity_source') not in (None,'local_claim')
                    or p.get('verification_proof') is not None):
                continue
            backup=p.get('backup')
            if not isinstance(backup,dict) or not isinstance(backup.get('devices'),list):
                continue
            matching=[row for row in backup['devices'] if isinstance(row,dict) and row.get('device_id')==target]
            if (len(matching)==1 and type(matching[0].get('product_id')) is int
                    and matching[0]['product_id']==134):
                return True
        return False

    async def _reconcile_claim_retirement(self, identifier, owner_reset, *, pre_key=False):
        """Retire a scoped claim after owner reset, or proven pre-address/key dispatch.

        No reset, disassociation or association command is sent here. Ordinary
        light reset claims remain excluded, except the retained direct first RL4.
        An ordinary RL4 or A19 public-key-only attempt may retire without reset only
        under its stricter receipt/isolation/history/exact-observation profile.
        """
        r = self.runtime
        self._batch_guard()
        r.store.require_tx_ready()
        if not pre_key and owner_reset is not True:
            raise ValueError('Explicit owner reset declaration required')
        journal = next((j for j in r.store.journals(pending=True) if j['id'] == identifier), None)
        if (journal is None or journal['kind'] != 'claim' or journal['status'] != 'uncertain'
                or journal['target'] == 32898 or journal['detail'].get('inventory_complete') is not True):
            raise ValueError('Uncertain managed claim required')
        bootstrap = getattr(r, 'bootstrap', None)
        def bootstrap_scope():
            inventory = r.store.inventory()
            return (bootstrap is not None and getattr(r, 'bootstrap', None) is bootstrap
                    and r.store.get('tx_admission', {}).get('document', {}).get('mode') == 'fresh_mesh'
                    and bootstrap.needs_first_claim
                    and bootstrap_reset_profile(journal['detail'])
                    and ((getattr(bootstrap, 'automatic', False) is True and journal['detail'].get('fresh_mesh_first_claim') is True)
                         or bootstrap.selected_address == journal['detail']['bootstrap_address'])
                    and not inventory['devices'] and not inventory['groups']
                    and not any(j['kind'] == 'claim' and j['status'] == 'verified' for j in r.store.journals()))
        direct_reset = bootstrap_scope()
        inventory_before = copy.deepcopy(r.store.inventory())
        pre_network_key = pre_key and rl4_pre_network_key_claim_profile(journal)
        # These explicit profiles prove the network key was never dispatched.
        a19_public_key = pre_key and a19_public_key_claim_profile(journal)
        public_key_only = pre_key and (rl4_public_key_claim_profile(journal) or a19_public_key or pre_network_key)
        if pre_key:
            if public_key_only:
                if not self._public_key_retirement_scope(journal, inventory_before):
                    raise ValueError('No-key light retirement requires isolated, exclusive, unchanged receipt evidence')
            elif not direct_reset or not pre_key_claim_profile(journal) or identifier not in r.claim_isolated:
                raise ValueError('Only a proven first-RL4 or supported pre-key light attempt can retire without reset')
        identity = journal['detail'].get('uuid')
        fingerprint = uuid_hash(self._identity(identity))
        if not direct_reset and not public_key_only and (not r.capabilities.get('claim_reset_recovery')
                or not wired_reset_profile(identity, journal['detail']['product_id'])):
            raise ValueError('Reset recovery is restricted to qualified wired or empty first-device bootstrap scope')
        if journal['detail'].get('uuid_hash') != fingerprint or journal['target'] in r.core.devices:
            raise ValueError('Claim identity or inventory conflicts with recovery')
        if r.operation.locked() or any(j['id'] != identifier for j in r.store.journals(pending=True)):
            raise RuntimeError('Competing operation prevents reset recovery')
        if pre_key:
            # Prior worker has ended under the operation lock; invalidate any
            # prior observations before collecting this explicit recovery proof.
            r.session()
        generation = r.generation
        observations = await self.discover()
        matches = [row for row in observations if row['uuid_hash'] == fingerprint]
        if (self.scan_truncated or r.store.get('unpaired_history_full', False)
                or fingerprint in r.store.get('unpaired_hash_conflicts', [])
                or r.store.get('unpaired_identity_hashes', {}).get(str(fingerprint)) != identity
                or len(matches) != 1 or matches[0]['uuid'] != identity):
            return {'reset_verified': False, 'retired': False, 'requires_review': True, 'association_retried': False}
        if generation != r.generation or self.discovery_generation != generation or r.operation.locked():
            raise RuntimeError('Reset proof crossed a session boundary')
        async with r.operation:
            with r.store.transaction():
                r.store.require_tx_ready()
                current = next(j for j in r.store.journals() if j['id'] == identifier)
                if public_key_only and not self._public_key_retirement_scope(current, inventory_before):
                    raise ValueError('Public-key retirement evidence changed during observation')
                if pre_key and not public_key_only and not pre_key_claim_profile(current):
                    raise ValueError('Pre-key receipt changed during observation')
                if pre_key and identifier not in r.claim_isolated:
                    raise ValueError('Claim isolation changed during observation')
                if (fingerprint in r.store.get('unpaired_hash_conflicts', [])
                        or r.store.get('unpaired_history_full', False)
                        or r.store.get('unpaired_identity_hashes', {}).get(str(fingerprint)) != identity):
                    raise ValueError('Unpaired identity evidence changed during observation')
                if direct_reset and not bootstrap_scope():
                    raise ValueError('First-device reset scope changed during observation')
                if current != journal or any(j['id'] != identifier for j in r.store.journals(pending=True)):
                    raise ValueError('Claim receipt changed during reset proof')
                target = journal['target']
                if target in r.core.devices or target in r.store.get('retired_addresses', []):
                    raise ValueError('Address ownership changed during reset proof')
                proof_id = uuid.uuid4().hex
                proof = {'original_receipt': journal, 'owner_reset_declared': not pre_key, 'pre_key_only': pre_key,
                         'uuid': identity, 'uuid_hash': fingerprint, 'fresh_unpaired_observation': True,
                         'reset_dispatched': False, 'association_retried': False,
                         'recovery_schema': 'claim-retirement-v1', 'observation_generation': generation,
                         'observation_ms': r.clock()}
                if pre_network_key:
                    proof.update(recovery_schema='claim-pre-network-key-retirement-v1',
                                 dispatch_profile='rl4-pre-network-key-8-through-10',
                                 device_id_dispatched=journal['detail']['association_phase'] == 'device_id',
                                 network_key_dispatched=False, isolated_previous_worker=True)
                elif public_key_only:
                    proof.update(recovery_schema='claim-public-key-retirement-v1',
                                 dispatch_profile='a19-public-key-2-through-7' if a19_public_key else 'rl4-public-key-2-through-7',
                                 device_id_dispatched=False, network_key_dispatched=False,
                                 isolated_previous_worker=True)
                r.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
                                   (proof_id, 'claim_pre_key' if pre_key else 'claim_reset', target, 'verified', json.dumps(proof)))
                r.store.set('retired_claims', sorted(set(r.store.get('retired_claims', [])) | {identifier}))
                r.store.set('retired_addresses', sorted(set(r.store.get('retired_addresses', [])) | {target}))
                r.store.db.execute("UPDATE journals SET status='retired' WHERE id=? AND status='uncertain'", (identifier,))
            result = {'reset_verified': not pre_key, 'pre_key_retired': pre_key, 'retired': True, 'transaction': proof_id,
                      'address_retired': True, 'association_retried': False, 'automatic_reclaim': False}
            try:
                r.session()
            except Exception:
                r.mesh_ready = False
                result.update(publication_pending=True, requires_review=True)
            return result
