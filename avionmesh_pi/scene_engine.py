"""Verified native scene transactions. Mutations never replay automatically.

A19 references 1/2 are physically observed; other colour lights and references
remain experimental. Native activation is a single broadcast only after proving
this reference is empty on nonmembers of the admitted inventory, except lights
the owner explicitly chose to leave unchecked because they were unpowered.

Reads are safe to repeat and are retried a bounded number of times. Setters are
never repeated. An operation whose outcome cannot be read can be set aside by
the owner without any radio work; its reference then stays reserved.
"""
import asyncio
import copy
import hashlib
import json
import uuid
import time
from collections import OrderedDict
from contextlib import asynccontextmanager

from .bridge import LIGHTS, COLOR_LIGHTS
from .protocol import integer
from .scene_protocol import (candidate_colour_insert, candidate_brightness_insert,
                             candidate_delete, candidate_recall)

KEY = 'native_scenes_v1'
TOKENS = 'native_scene_query_counters_v1'
FLOORS = 'native_scene_reply_floors_v1'
PROOFS = 'native_scene_activation_proofs_v1'
SET_ASIDE = 'native_scene_set_aside_v1'
UNCHECKED = 'native_scene_unchecked_v1'
EMPTY = {'colour': 'ffffffff', 'brightness': 'ffffffff'}


class SceneLightSilent(asyncio.TimeoutError):
    """One light did not answer repeated read-only scene queries."""
    def __init__(self, target):
        super().__init__(f'Light {target} did not answer. Nothing more was sent.')
        self.target = target


def _newer(sequence, previous):
    return 0 < ((sequence - previous) & 0xffffff) < 0x800000


class SceneEngine:
    plan_schema = 2
    def __init__(self, runtime):
        self.r = runtime
        saved = self.r.store.get(PROOFS, {})
        self.leases = saved if isinstance(saved, dict) else {}
        self.sent_mutations = OrderedDict()
        self.mutation_epoch = 0
        self.gap = .15
        self.pair_gap = .35
        self.read_attempts = 3
        self.read_timeout = 3

    def observe(self, packet, *args):
        # Authenticated observation only: never transmit from this callback.
        p = packet.payload
        if packet.opcode != 0x73 or len(p) < 2 or p[1] != 0x22 or p[0] not in (2, 5):
            return
        self.mutation_epoch += 1
        reference = None
        if len(p) == 10:
            reference = p[5] & 0x1f
        elif len(p) == 4:
            # A reply has no reference, but a recent write made by this process
            # identifies it. Other replies conservatively invalidate all proofs.
            sent = self.sent_mutations.get((packet.crypto_source, p[0], p[2]))
            if (sent and sent['generation'] == self.r.generation and
                    0 <= time.monotonic() - sent['when'] <= 30 and
                    packet.sequence > sent['floor']):
                reference = sent['reference']
        if reference is None:
            self._revoke()
        else:
            for row in self._rows():
                if row['reference'] == reference: self._revoke(row['id'])
        self._changed()

    def _rows(self):
        rows = self.r.store.get(KEY, [])
        if not isinstance(rows, list) or len(rows) > 20:
            raise RuntimeError('Invalid saved scene inventory')
        ids, refs = set(), set()
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                    or row['id'] in ids or row.get('reference') in refs):
                raise RuntimeError('Invalid saved scene identity')
            integer(row['reference'], 1, 20, 'scene reference')
            self._validate(row['name'], row['members'])
            if type(row.get('exposed')) is not bool:
                raise RuntimeError('Invalid saved scene exposure')
            ids.add(row['id']); refs.add(row['reference'])
        return rows

    def _pending(self):
        from .scene_legacy import unresolved
        legacy = unresolved(self)
        legacy_ids = {identifier for row in legacy for identifier in row['detail']['legacy_sources']}
        return [j for j in self.r.store.journals(pending=True)
                if j['kind'] == 'scene' and j['id'] not in legacy_ids] + legacy

    def _row(self, scene_id):
        return next((x for x in self._rows() if x['id'] == scene_id), None)

    def _require(self, scene_id):
        row = self._row(scene_id)
        if row is None:
            raise ValueError('Unknown scene')
        if row.get('owner') != self._owner():
            raise RuntimeError('Scene belongs to a different admitted mesh; no commands sent')
        return row

    def status(self):
        pending = self._pending()
        affected = {j['detail']['scene_id'] for j in pending}
        rows = copy.deepcopy(self._rows())
        existing = {r['id'] for r in rows}
        aside = {e['scene_id'] for e in self._aside()}
        unchecked = self._unchecked()
        for j in pending:
            if j['detail']['scene_id'] not in existing and j['detail'].get('desired'):
                rows.append(copy.deepcopy(j['detail']['desired']))
        for row in rows:
            ready = self._leased(row) and row.get('owner') == self._owner()
            row['ownership_valid'] = row.get('owner') == self._owner()
            row['members_available'] = all(self.r.core.devices.get(m['target']) is not None and
                self.r.core.devices[m['target']].product_id in COLOR_LIGHTS for m in row['members'])
            row['set_aside'] = row['id'] in aside
            row['unchecked'] = [t for t in unchecked.get(row['id'], []) if t in self.r.core.devices]
            row['status'] = 'needs_review' if row['id'] in affected or row['set_aside'] or not row['ownership_valid'] or not row['members_available'] else 'verified'
            row['experimental'] = any(self.r.core.devices.get(m['target']) is None or
                self.r.core.devices[m['target']].product_id != 134 for m in row['members'])
            row['activation_ready'] = ready and row['status'] == 'verified'
        return {'scenes': rows, 'operations': pending, 'supported_models': list(COLOR_LIGHTS),
                'native_validated_models': [134], 'experimental_models': [93, 137, 162],
                'reserved_references': sorted({e['reference'] for e in self._aside()}),
                'enabled': self.r.capabilities.get('native_scenes', True)}

    def _changed(self):
        callback = getattr(self.r, 'scene_changed', None)
        if callback:
            try: callback()
            except Exception: pass  # Durable completion does not depend on publication.

    def _validate(self, name, members):
        if (not isinstance(name, str) or not name.strip() or len(name) > 128
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise ValueError('Expected a scene name of 1 to 128 characters')
        if not isinstance(members, list) or not 1 <= len(members) <= 32:
            raise ValueError('Choose between 1 and 32 colour lights')
        seen = set()
        for m in members:
            if not isinstance(m, dict) or set(m) != {'target', 'brightness', 'kelvin'}:
                raise ValueError('Expected target, brightness and kelvin for every light')
            integer(m['target'], 32896, 65407, 'scene member')
            integer(m['brightness'], 0, 255, 'brightness')
            integer(m['kelvin'], 2700, 5000, 'colour temperature')
            if m['target'] in seen: raise ValueError('Duplicate scene member')
            seen.add(m['target'])

    def _models(self, members):
        for m in members:
            d = self.r.core.devices.get(m['target'])
            if not d or d.product_id not in COLOR_LIGHTS:
                raise ValueError('Native scenes currently support colour lights only')

    def _aside_saved(self):
        rows = self.r.store.get(SET_ASIDE, [])
        return [e for e in rows if isinstance(e, dict) and type(e.get('reference')) is int
                and 1 <= e['reference'] <= 20 and isinstance(e.get('scene_id'), str)] \
            if isinstance(rows, list) else []

    def _aside(self):
        owner = self._owner()
        return [e for e in self._aside_saved() if e.get('owner') == owner]

    def _clear_aside(self, scene_id):
        rows = self._aside_saved(); kept = [e for e in rows if e['scene_id'] != scene_id]
        if kept != rows: self.r.store.set(SET_ASIDE, kept)

    def _reserve_aside(self, reference, scene_id, transaction, reason):
        rows = self._aside_saved()
        rows.append({'reference': reference, 'scene_id': scene_id, 'transaction': transaction,
                     'owner': self._owner(), 'reason': reason, 'at': int(time.time())})
        self.r.store.set(SET_ASIDE, rows[-64:])

    def _unchecked(self):
        rows = self.r.store.get(UNCHECKED, {})
        return {k: [t for t in v if type(t) is int] for k, v in rows.items()
                if isinstance(v, list)} if isinstance(rows, dict) else {}

    def _note_unchecked(self, scene_id, targets):
        rows = self._unchecked()
        if targets: rows[scene_id] = sorted(targets)
        else: rows.pop(scene_id, None)
        if rows != self.r.store.get(UNCHECKED, {}): self.r.store.set(UNCHECKED, rows)

    def _skip(self, skip, targets, protected, reason):
        """Owner-named unpowered lights. They are never written, only left unread."""
        if (not isinstance(skip, (list, tuple)) or len(skip) > 64 or len(set(skip)) != len(skip)
                or any(type(t) is not int for t in skip)):
            raise ValueError('Expected a list of lights to leave unchecked')
        if set(skip) - set(targets): raise ValueError('Only lights on this mesh can be left unchecked')
        if set(skip) & set(protected): raise ValueError(reason)
        return sorted(skip)

    def _owner(self):
        return hashlib.sha256(json.dumps({k: self.r.store.get(k) for k in
            ('mesh_fingerprint', 'state_id', 'tx_admission')}, sort_keys=True).encode()).hexdigest()

    def _identity(self):
        return hashlib.sha256(json.dumps({'owner': self._owner(),
            'inventory': self.r.store.inventory()}, sort_keys=True).encode()).hexdigest()

    def _stamp(self, row):
        # These are verified persistent presets, not connection-local state.
        # Names, rooms, group labels and sharing cannot change a radio preset.
        inventory = self.r.store.inventory()['devices']
        identities = sorted((d['device_id'], d['product_id']) for d in inventory)
        runtime_ids = sorted((d.device_id, d.product_id) for d in self.r.core.devices.values())
        return hashlib.sha256(json.dumps({'schema': 1, 'owner': self._owner(),
            'devices': identities, 'runtime_devices': runtime_ids,
            'scene': {k: row[k] for k in ('id', 'owner', 'reference', 'members')}},
            sort_keys=True).encode()).hexdigest()

    def _grant(self, row):
        self.leases[row['id']] = self._stamp(row)
        self.r.store.set(PROOFS, self.leases)

    def _revoke(self, scene_id=None):
        if scene_id is None:
            self.leases.clear()
        else:
            self.leases.pop(scene_id, None)
        self.r.store.set(PROOFS, self.leases)

    def _track_mutation(self, command):
        target, p = int.from_bytes(command[:2], 'little'), command[3:]
        key = (target, p[0], p[4])
        self.sent_mutations.pop(key, None)
        self.sent_mutations[key] = {'reference': p[5] & 0x1f,
            'generation': self.r.generation, 'when': time.monotonic(),
            'floor': self.r.store.get(FLOORS, {}).get(str(target), -1)}
        while len(self.sent_mutations) > 128:
            self.sent_mutations.popitem(last=False)

    def _leased(self, row):
        return self.leases.get(row['id']) == self._stamp(row)

    def _available(self, transaction=None):
        r = self.r
        if any(row.get('owner') != self._owner() for row in self._rows()):
            raise RuntimeError('Saved scenes belong to a different admitted mesh')
        if not r.capabilities.get('native_scenes', True): raise ValueError('Native scenes disabled')
        if r.capabilities.get('inventory_complete') is not True:
            raise RuntimeError('Scene ownership requires a complete mesh inventory')
        if not r.mesh_ready or not r.transport.ready: raise RuntimeError('Mesh is offline')
        from .scene_legacy import related_ids
        allowed = ({transaction} | set(related_ids(self, transaction))) if transaction else set()
        if any(j['id'] not in allowed for j in r.store.journals(pending=True)):
            raise RuntimeError('Review the unfinished operation first')
        if any(j['id'] not in allowed for j in self._pending()):
            raise RuntimeError('Review the unfinished scene operation first')

    @asynccontextmanager
    async def _admit(self, transaction=None):
        self._available(transaction)
        generation, identity = self.r.generation, self._identity()
        async with self.r.operation:
            self._available(transaction)
            self._fence(generation, identity)
            yield generation, identity

    def _fence(self, generation, identity):
        if generation != self.r.generation or identity != self._identity():
            raise RuntimeError('Mesh session or inventory changed; read-only recovery required')
        if not self.r.mesh_ready or not self.r.transport.ready:
            raise RuntimeError('Mesh is offline')

    def _targets(self):
        return sorted(d.device_id for d in self.r.core.devices.values() if d.product_id in LIGHTS)

    def _reserved(self):
        refs = {r['reference'] for r in self._rows()}
        refs.update(j['detail']['reference'] for j in self._pending())
        # A set-aside operation or forgotten scene may have left presets behind.
        refs.update(e['reference'] for e in self._aside())
        for snapshot in self.r.store.bindings().values():
            for slot in snapshot.get('slots', []):
                if slot.get('flags', 0) & 0x20:
                    refs.add(slot.get('target'))
        return refs

    def _token(self, target):
        # Persistent per-target counters plus monotonically increasing response
        # floors reject delayed replies when the one-byte token eventually wraps.
        values = self.r.store.get(TOKENS, {})
        n = values.get(str(target), 0)
        if type(n) is not int or n < 0: raise RuntimeError('Invalid scene query counter')
        values[str(target)] = n + 1
        self.r.store.set(TOKENS, values)
        return n % 255 + 1

    async def _read(self, target, ref, generation, identity):
        async def one(selector):
            # A read changes nothing, so a lost reply may be asked again with a
            # fresh token. Setters are never repeated here or anywhere else.
            restarted = None
            for _ in range(max(1, self.read_attempts)):
                self._fence(generation, identity)
                token = self._token(target)
                floor = self.r.store.get(FLOORS, {}).get(str(target), -1)
                payload = bytes([1, 0x22, 0, 0, token, selector, 0, 0, 0, 0])
                frame = target.to_bytes(2, 'little') + b'\x73' + payload
                below = []
                def match(p):
                    if not (p.crypto_source == target and p.opcode == 0x73 and len(p.payload) == 8
                            and p.payload[:4] == bytes([1, 0x22, token, selector])):
                        return False
                    if p.sequence > floor: return True
                    # A light that lost power may count from the start again. One
                    # such answer is never accepted. It is accepted only when a
                    # second fresh query is answered later in the same new count.
                    if restarted is not None and _newer(p.sequence, restarted): return True
                    below.append(p.sequence)
                    return False
                try:
                    packet = await self.r.exchange(frame, match, timeout=self.read_timeout, generation=generation)
                except asyncio.TimeoutError:
                    if below: restarted = below[-1]
                    continue
                self._fence(generation, identity)
                floors = self.r.store.get(FLOORS, {}); floors[str(target)] = packet.sequence
                self.r.store.set(FLOORS, floors)
                await asyncio.sleep(self.gap)
                return packet.payload[4:].hex()
            raise SceneLightSilent(target)
        result = []
        for _ in range(2):
            result.append({'colour': await one(ref), 'brightness': await one(ref | 0x20)})
        if result[0] != result[1]: raise RuntimeError('Scene properties changed between reads')
        return result[0]

    async def _survey(self, ref, targets, generation, identity, skip=(), assumed=None):
        epoch = self.mutation_epoch
        result = {}
        for t in targets:
            # A light left unchecked keeps the value this plan last relied on.
            result[str(t)] = (copy.deepcopy(assumed[str(t)]) if t in skip
                              else await self._read(t, ref, generation, identity))
        if epoch != self.mutation_epoch:
            raise RuntimeError('Scene mutation observed during verification; repeat read-only verification')
        return result

    @staticmethod
    def _values(member):
        return {'colour': (b'\x02' + member['kelvin'].to_bytes(2, 'big') + b'\0').hex(),
                'brightness': bytes([member['brightness'], 0, 0, 0]).hex()}

    def _expected(self, row, targets):
        members = {m['target']: m for m in row['members']} if row else {}
        return {str(t): self._values(members[t]) if t in members else dict(EMPTY) for t in targets}

    @staticmethod
    def _plan_digest(d):
        frozen = {k: d[k] for k in ('schema', 'scene_id', 'reference', 'action', 'prior',
                  'desired', 'targets', 'inventory', 'baseline', 'final')}
        frozen['steps'] = [{k: step[k] for k in ('target', 'property', 'value')} for step in d['steps']]
        return hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()

    def _validate_journal(self, d, *, completed_history=False):
        # Closed receipts explain what already happened on their saved inventory.
        # Only active work must still match today's inventory; this historical
        # mode is never used to admit, resume or dispatch an operation.
        try:
            if completed_history and (d['schema'] != 2 or d.get('completed') is not True
                    or d.get('recovery_ready') is not False or d['observed'] != d['final']
                    or any(step['status'] != 'verified' for step in d['steps'])):
                raise ValueError('Historical receipt is not a completed paired save')
            targets = d['targets']
            if (not isinstance(targets, list)
                    or any(type(t) is not int or not 32896 <= t <= 65407 for t in targets)
                    or targets != sorted(set(targets))):
                raise ValueError('Invalid saved scene targets')
            if type(d['schema']) is not int or d['schema'] not in (1, 2) or d['action'] not in ('create', 'edit', 'remove'):
                raise ValueError('Invalid scene journal schema')
            integer(d['reference'], 1, 20, 'scene reference')
            if (not isinstance(d['scene_id'], str) or (not completed_history and targets != self._targets())
                    or d['plan_sha256'] != self._plan_digest(d)):
                raise ValueError('Scene plan identity or digest changed')
            if ((d['action'] == 'create') != (d['prior'] is None)
                    or (d['action'] == 'remove') != (d['desired'] is None)):
                raise ValueError('Invalid scene operation shape')
            for row in (d['prior'], d['desired']):
                if row is None: continue
                self._validate(row['name'], row['members'])
                # A light that has since left the mesh may remain in the prior
                # record; it is outside the targets and is never addressed.
                if row is d['desired']:
                    if any(m['target'] not in targets for m in row['members']):
                        raise ValueError('Desired member is outside the saved inventory')
                    if not completed_history: self._models(row['members'])
                if (row['id'] != d['scene_id'] or row['reference'] != d['reference']
                        or row.get('owner') != self._owner() or type(row['exposed']) is not bool):
                    raise ValueError('Scene plan ownership changed')
            baseline = self._expected(d['prior'], d['targets'])
            final = self._expected(d['desired'], d['targets'])
            if d['baseline'] != baseline or d['final'] != final:
                raise ValueError('Scene plan values disagree with declared presets')
            unchecked = d.get('assumed', [])
            if (not isinstance(unchecked, list) or any(type(t) is not int for t in unchecked)
                    or unchecked != sorted(set(unchecked)) or set(unchecked) - set(d['targets'])):
                raise ValueError('Invalid unchecked light list')
            expected_steps = self._make_steps(d['schema'], baseline, final, d['targets'])
            if set(unchecked) & {step['target'] for step in expected_steps}:
                raise ValueError('An unchecked light cannot have a pending change')
            if ([{k: s[k] for k in ('target', 'property', 'value')} for s in d['steps']] != expected_steps
                    or len(d['steps']) > 64):
                raise ValueError('Scene step list changed')
            observed = copy.deepcopy(baseline); unfinished = False; attempted = False
            for step in d['steps']:
                status = step['status']
                if status not in ('verified', 'not_started', 'attempted', 'not_applied', 'retry_required'):
                    raise ValueError('Invalid scene step status')
                if status == 'verified':
                    if unfinished: raise ValueError('Noncontiguous scene progress')
                    observed = self._after_step(observed, step)
                else:
                    if status in ('attempted', 'not_applied', 'retry_required'):
                        if unfinished or attempted: raise ValueError('Multiple attempted scene steps')
                        attempted = True
                    unfinished = True
                attempts = step.get('attempts', [])
                if not isinstance(attempts, list) or len(attempts) > 8:
                    raise ValueError('Invalid scene attempt history')
                if status in ('attempted', 'not_applied', 'retry_required', 'verified') and not attempts:
                    raise ValueError('Missing scene attempt receipt')
                for attempt in attempts:
                    if d['schema'] == 1:
                        integer(attempt['request_token'], 1, 255, 'scene attempt token')
                    else:
                        expected_commands = ['colour', 'brightness'] if step['property'] == 'preset' else ['remove']
                        commands = attempt['commands']
                        if (not isinstance(commands, list) or [c['property'] for c in commands] != expected_commands):
                            raise ValueError('Invalid paired scene command list')
                        started = False
                        for command in commands:
                            if command['status'] not in ('not_started', 'attempted', 'transport_accepted'):
                                raise ValueError('Invalid paired scene command state')
                            if command['status'] != 'not_started':
                                if started: raise ValueError('Paired commands started out of order')
                                integer(command['request_token'], 1, 255, 'scene attempt token')
                            else:
                                started = True
                                if command.get('request_token') is not None:
                                    raise ValueError('Unsent command has a token')
                        if status == 'verified' and attempt is attempts[-1] and any(
                                c['status'] == 'not_started' for c in commands):
                            raise ValueError('Verified preset has an unsent command')
            if d['observed'] != observed: raise ValueError('Scene progress and observed values disagree')
            if type(d.get('recovery_ready')) is not bool: raise ValueError('Invalid recovery state')
            if d['recovery_ready']:
                proof = d.get('recovery_proof')
                recovered = d.get('recovery_observed', observed) if d['schema'] == 2 else observed
                current = next((step for step in d['steps'] if step['status'] in ('attempted', 'not_applied', 'retry_required')), None)
                if d['schema'] == 2 and recovered != observed and (current is None or
                        not self._valid_intermediate(d, current, recovered)):
                    raise ValueError('Recovery snapshot is outside the pending preset')
                expected = hashlib.sha256(json.dumps(recovered, sort_keys=True).encode()).hexdigest()
                if (not isinstance(proof, dict) or proof.get('observed_sha256') != expected
                        or proof.get('plan_sha256') != d['plan_sha256']):
                    raise ValueError('Missing verified recovery receipt')
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise RuntimeError('Scene journal integrity check failed; no commands sent') from error

    def _journal(self, transaction):
        j = next((j for j in self.r.store.journals() if j['id'] == transaction and j['kind'] == 'scene'), None)
        if not j or j['status'] not in ('prepared', 'attempted', 'uncertain'):
            raise ValueError('Unknown unfinished scene operation')
        self._validate_journal(j['detail'])
        return j

    def _save_detail(self, j, status=None):
        self.r.store.db.execute('UPDATE journals SET detail=?,status=? WHERE id=?',
            (json.dumps(j['detail']), status or j['status'], j['id']))
        if status: j['status'] = status
        self._changed()

    def _known(self, row):
        """Members still on this mesh; a departed light can no longer be read."""
        return [m for m in row['members'] if self.r.core.devices.get(m['target']) is not None
                and self.r.core.devices[m['target']].product_id in COLOR_LIGHTS]

    def _present(self, row):
        if len(self._known(row)) != len(row['members']):
            raise RuntimeError('A light in this scene is no longer on the mesh. Edit the scene and remove that light first.')

    async def create(self, name, members, exposed=False, skip=()):
        self._validate(name, members); self._models(members)
        if type(exposed) is not bool: raise ValueError('Expected boolean exposure')
        async with self._admit() as (generation, identity):
            targets = self._targets(); ref = None; baseline = None
            skip = self._skip(skip, targets, [m['target'] for m in members],
                              'A light in this scene must answer before it can be saved')
            empty = {str(t): dict(EMPTY) for t in targets}
            for candidate in range(1, 21):
                if candidate in self._reserved(): continue
                observed = await self._survey(candidate, targets, generation, identity, skip, empty)
                if all(v == EMPTY for v in observed.values()):
                    ref, baseline = candidate, observed; break
            if ref is None: raise RuntimeError('No empty scene reference found in the supported range')
            row = {'id': uuid.uuid4().hex, 'reference': ref, 'name': name.strip(),
                   'members': copy.deepcopy(members), 'exposed': exposed, 'owner': self._owner()}
            return await self._start('create', None, row, baseline, targets, generation, identity, skip)

    async def edit(self, scene_id, name, members, skip=()):
        self._validate(name, members); self._models(members)
        async with self._admit() as (generation, identity):
            prior = self._require(scene_id); targets = self._targets()
            desired = dict(prior, name=name.strip(), members=copy.deepcopy(members))
            stored, final = self._expected(prior, targets), self._expected(desired, targets)
            skip = self._skip(skip, targets, [t for t in targets if stored[str(t)] != final[str(t)]],
                              'A light whose settings change must answer before the scene is saved')
            baseline = await self._survey(prior['reference'], targets, generation, identity, skip, stored)
            if baseline != stored: raise RuntimeError('Stored scene changed externally; no writes sent')
            return await self._start('edit', prior, desired, baseline, targets, generation, identity, skip)

    async def remove(self, scene_id, skip=()):
        async with self._admit() as (generation, identity):
            prior = self._require(scene_id); targets = self._targets()
            stored = self._expected(prior, targets)
            skip = self._skip(skip, targets, [m['target'] for m in self._known(prior)],
                              'A light in this scene must answer before the scene can be removed from it')
            baseline = await self._survey(prior['reference'], targets, generation, identity, skip, stored)
            if baseline != stored: raise RuntimeError('Stored scene changed externally; no deletes sent')
            return await self._start('remove', prior, None, baseline, targets, generation, identity, skip)

    @staticmethod
    def _make_steps(schema, baseline, final, targets):
        steps = []
        for target in targets:
            t = str(target); before, after = baseline[t], final[t]
            if before == after: continue
            if after == EMPTY:
                steps.append({'target': target, 'property': 'remove', 'value': None})
            elif schema == 2:
                steps.append({'target': target, 'property': 'preset', 'value': copy.deepcopy(after)})
            else:
                for prop in ('colour', 'brightness'):
                    if before[prop] != after[prop]:
                        steps.append({'target': target, 'property': prop, 'value': after[prop]})
        return steps

    async def _start(self, action, prior, desired, baseline, targets, generation, identity, skip=()):
        row = desired or prior; final = self._expected(desired, targets)
        steps = [dict(step, status='not_started') for step in self._make_steps(
            self.plan_schema, baseline, final, targets)]
        detail = {'schema': self.plan_schema, 'scene_id': row['id'], 'reference': row['reference'], 'action': action,
                  'prior': prior, 'desired': desired, 'targets': targets, 'inventory': identity,
                  'baseline': baseline, 'observed': baseline, 'final': final, 'steps': steps,
                  'recovery_ready': False}
        if skip: detail['assumed'] = sorted(skip)
        detail['plan_sha256'] = self._plan_digest(detail)
        self._fence(generation, identity)
        identifier = self.r.store.begin('scene', (row['members'][0]['target']), detail)
        j = self._journal(identifier)
        self._revoke(row['id']); self._changed()
        return await self._execute(j, generation, identity)

    def _after_step(self, observed, step):
        updated = copy.deepcopy(observed); t = str(step['target'])
        if step['property'] == 'remove': updated[t] = dict(EMPTY)
        elif step['property'] == 'preset': updated[t] = copy.deepcopy(step['value'])
        else: updated[t][step['property']] = step['value']
        return updated

    def _command(self, step, ref):
        token = self._token(step['target'])
        if step['property'] == 'remove': return candidate_delete(step['target'], ref, token)
        value = bytes.fromhex(step['value'])
        if step['property'] == 'colour':
            return candidate_colour_insert(step['target'], ref, int.from_bytes(value[1:3], 'big'), token)
        return candidate_brightness_insert(step['target'], ref, value[0], token)

    async def _send_step(self, j, step, expected, generation, identity, continue_current=False):
        d = j['detail']; attempts = step.setdefault('attempts', [])
        if d['schema'] == 1:
            if len(attempts) >= 8: raise RuntimeError('Scene step attempt limit reached')
            command = self._command(step, d['reference'])
            attempts.append({'before': copy.deepcopy(d['observed'][str(step['target'])]),
                             'expected': copy.deepcopy(expected[str(step['target'])]),
                             'request_token': command[7]})
            step['status'] = 'attempted'; d['recovery_ready'] = False
            self._save_detail(j, 'attempted')
            self._track_mutation(command)
            await self.r.send(command, generation=generation)
            return
        if not continue_current:
            if len(attempts) >= 8: raise RuntimeError('Scene step attempt limit reached')
            phases = ['colour', 'brightness'] if step['property'] == 'preset' else ['remove']
            attempts.append({'before': copy.deepcopy(d.get('recovery_observed', d['observed'])[str(step['target'])]),
                'expected': copy.deepcopy(expected[str(step['target'])]),
                'commands': [{'property': phase, 'status': 'not_started', 'request_token': None}
                             for phase in phases]})
        step['status'] = 'attempted'; d['recovery_ready'] = False
        self._save_detail(j, 'attempted')
        for index, command_receipt in enumerate(attempts[-1]['commands']):
            if command_receipt['status'] != 'not_started': continue
            if index:
                # The captured app sends colour then brightness, 0.28–1.12 s
                # apart. It does not read an intermediate colour-only record.
                await asyncio.sleep(self.pair_gap)
            self._fence(generation, identity)
            phase = command_receipt['property']
            piece = dict(step, property=phase,
                         value=step['value'][phase] if step['property'] == 'preset' else None)
            command = self._command(piece, d['reference'])
            command_receipt.update(status='attempted', request_token=command[7])
            self._save_detail(j, 'attempted')  # Durable marker BEFORE every send.
            self._track_mutation(command)
            await self.r.send(command, generation=generation)
            command_receipt['status'] = 'transport_accepted'
            self._save_detail(j, 'attempted')

    async def _execute(self, j, generation, identity, *, continue_current=False):
        d = j['detail']
        try:
            for step in d['steps']:
                if step['status'] == 'verified': continue
                continuing = continue_current and d['schema'] == 2 and step['status'] == 'attempted'
                if step['status'] != 'not_started' and not continuing:
                    raise RuntimeError('Recovery is required before continuing')
                self._fence(generation, identity)
                expected = self._after_step(d['observed'], step)
                await self._send_step(j, step, expected, generation, identity, continuing)
                continue_current = False
                await asyncio.sleep(self.gap)
                value = await self._read(step['target'], d['reference'], generation, identity)
                attempt = step['attempts'][-1]
                attempt['readback'] = copy.deepcopy(value); attempt['readback_rounds'] = 2
                attempt['readback_matches_expected'] = value == expected[str(step['target'])]
                self._save_detail(j)
                if value != expected[str(step['target'])]: raise RuntimeError('Scene write was not confirmed')
                d['observed'] = expected; step['status'] = 'verified'; self._save_detail(j)
            observed = await self._survey(d['reference'], d['targets'], generation, identity,
                                          d.get('assumed', ()), d['final'])
            if observed != d['final']: raise RuntimeError('Final scene verification disagreed')
            return self._commit(j, generation, identity)
        except BaseException as error:
            d['error'] = str(error)[:240] or type(error).__name__; d['recovery_ready'] = False
            d['unreachable'] = error.target if isinstance(error, SceneLightSilent) else None
            self._save_detail(j, 'uncertain')
            if isinstance(error, asyncio.CancelledError): raise
            return {'transaction': j['id'], 'scene_id': d['scene_id'], 'verified': False,
                    'requires_review': True, 'retry_allowed': False, 'error': d['error'],
                    'unreachable': d['unreachable']}

    def _valid_intermediate(self, d, step, observed):
        if not isinstance(observed, dict) or set(observed) != set(d['observed']): return False
        target = str(step['target']); before = d['observed']; after = self._after_step(before, step)
        for t, value in observed.items():
            if t != target:
                if value != before[t]: return False
            elif (not isinstance(value, dict) or set(value) != set(EMPTY) or
                  any(value[prop] not in (before[t][prop], after[t][prop]) for prop in EMPTY)):
                return False
        return True

    def _recovery_ready(self, j, observed, mode, retry=False):
        d = j['detail']; d['recovery_ready'] = True; d['error'] = None
        d['recovery_mode'] = mode; d['retry_requires_explicit_action'] = retry
        d['recovery_observed'] = copy.deepcopy(observed)
        d['recovery_proof'] = {'plan_sha256': d['plan_sha256'], 'observed_sha256':
            hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest()}
        self._save_detail(j, 'uncertain')
        return {'transaction': j['id'], 'verified': False, 'recovered': True, 'resume_available': True,
                'recovery_mode': mode, 'retry_requires_explicit_action': retry,
                'message': ('Explicit resume will send only the untouched command in the saved preset.'
                    if mode == 'continue_unattempted' else
                    'Explicit resume will attempt the saved preset again after fresh readback.' if retry else
                    'Observed progress verified. Explicitly resume the untouched steps.')}

    def _recover_pair(self, j, observed, generation, identity):
        d = j['detail']; step = next((s for s in d['steps'] if s['status'] in ('attempted', 'not_applied', 'retry_required')), None)
        if step is None:
            if observed != d['observed']: raise RuntimeError('Scene changed outside the pending operation')
        elif observed == self._after_step(d['observed'], step) and all(
                c['status'] != 'not_started' for c in step['attempts'][-1]['commands']):
            step['status'] = 'verified'; d['observed'] = observed
        else:
            if not self._valid_intermediate(d, step, observed):
                raise RuntimeError('Unexpected scene properties; no continuation authorized')
            commands = step['attempts'][-1]['commands']
            if any(c['status'] == 'not_started' for c in commands):
                # Even empty records may hide a staged colour. Keep the intent
                # reserved and only offer the never-attempted next command.
                return self._recovery_ready(j, observed, 'continue_unattempted')
            # Both commands were attempted. Only baseline/desired values are
            # present, but this cannot rule out hidden staging. Retain the whole
            # intent; a separate, explicit retry rechecks these exact reads and
            # journals a new pair. Never label the original setter not applied.
            step['status'] = 'retry_required'
            step['attempts'][-1]['readback_outcome'] = (
                'baseline_readable_state' if observed == d['observed'] else 'partial_readable_state')
            return self._recovery_ready(j, observed, 'retry_pair', True)
        if observed == d['final']: return self._commit(j, generation, identity)
        return self._recovery_ready(j, observed, 'continue_steps')

    def _commit(self, j, generation, identity):
        self._fence(generation, identity); d = j['detail']
        with self.r.store.transaction():
            rows = [row for row in self._rows() if row['id'] != d['scene_id']]
            if d['desired']: rows.append(d['desired'])
            self.r.store.set(KEY, rows)
            d['completed'] = True; d['recovery_ready'] = False
            self.r.store.db.execute('UPDATE journals SET detail=?,status=? WHERE id=?',
                                   (json.dumps(d), 'verified', j['id']))
            # Every light was read back, so an earlier set-aside flag for this
            # scene no longer applies.
            self._clear_aside(d['scene_id'])
            self._note_unchecked(d['scene_id'], d.get('assumed', []) if d['desired'] else [])
        if d['desired']: self._grant(d['desired'])
        else: self._revoke(d['scene_id'])
        self._changed()
        return {'transaction': j['id'], 'scene_id': d['scene_id'], 'verified': True,
                'removed': d['desired'] is None, 'scene': d['desired']}

    async def recover(self, transaction, skip=()):
        from .scene_legacy import related_ids, migrate
        if related_ids(self, transaction):
            if skip: raise ValueError('An earlier scene save needs every light to answer')
            return await migrate(self, transaction)
        async with self._admit(transaction) as (generation, identity):
            j = self._journal(transaction); d = j['detail']
            if identity != d['inventory']: raise RuntimeError('Inventory changed; preserve the pending scene operation')
            # Only lights this plan never writes can be left unread. They keep
            # the value read before the first write; nothing is sent to them.
            skip = self._skip(sorted(set(skip) | set(d.get('assumed', []))), d['targets'],
                              [step['target'] for step in d['steps']],
                              'A light this save changes must answer before it can be checked')
            if skip: d['assumed'] = skip
            try:
                observed = await self._survey(d['reference'], d['targets'], generation, identity, skip, d['observed'])
            except SceneLightSilent as error:
                d['unreachable'] = error.target; d['error'] = str(error)[:240]
                self._save_detail(j)
                raise
            d['unreachable'] = None
            if d['schema'] == 2:
                return self._recover_pair(j, observed, generation, identity)
            # Only the in-flight step may differ from the last verified state.
            attempted = next((s for s in d['steps'] if s['status'] == 'attempted'), None)
            if attempted and observed == self._after_step(d['observed'], attempted):
                attempted['status'] = 'verified'; d['observed'] = observed
            elif observed != d['observed']:
                raise RuntimeError('Unexpected scene properties; recovery cannot authorize more writes')
            elif attempted:
                # No setter is repeated: retain the attempt for review. If nothing
                # changed at all, the operation can safely close without success.
                if observed == d['baseline']:
                    with self.r.store.transaction():
                        d['completed'] = False; d['outcome'] = 'unchanged'
                        self.r.store.db.execute('UPDATE journals SET detail=?,status=? WHERE id=?',
                            (json.dumps(d), 'verified', j['id']))
                    self._changed()
                    return {'transaction': j['id'], 'verified': True, 'desired_matched': False, 'outcome': 'unchanged'}
                attempted['status'] = 'not_applied'
                attempted['attempts'][-1]['readback_outcome'] = 'unchanged'
                # This state never dispatches automatically. Explicit resume
                # repeats the full survey and journals a new attempt/token.
                d['retry_requires_explicit_action'] = True
            if observed == d['final']:
                return self._commit(j, generation, identity)
            d['recovery_ready'] = True; d['error'] = None
            d['recovery_proof'] = {'plan_sha256': d['plan_sha256'], 'observed_sha256':
                hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest()}
            self._save_detail(j, 'uncertain')
            return {'transaction': j['id'], 'verified': False, 'recovered': True, 'resume_available': True,
                    'retry_requires_explicit_action': d.get('retry_requires_explicit_action', False),
                    'message': ('A failed step is verified unchanged. Explicit resume will attempt it again.'
                        if d.get('retry_requires_explicit_action') else
                        'Observed progress verified. Explicitly resume the untouched steps.')}

    async def resume(self, transaction):
        # Recovery is deliberately separate; a user action is needed to send any
        # untouched steps after restart or uncertain transport.
        async with self._admit(transaction) as (generation, identity):
            j = self._journal(transaction); d = j['detail']
            if not d.get('recovery_ready') or identity != d['inventory']:
                raise RuntimeError('Run read-only recovery before resuming')
            observed = await self._survey(d['reference'], d['targets'], generation, identity,
                                          d.get('assumed', ()), d['observed'])
            if observed != (d.get('recovery_observed', d['observed']) if d['schema'] == 2 else d['observed']):
                raise RuntimeError('Scene properties changed since recovery')
            for step in d['steps']:
                if step['status'] in ('not_applied', 'retry_required'): step['status'] = 'not_started'
            d['retry_requires_explicit_action'] = False
            self._save_detail(j, 'uncertain')
            return await self._execute(j, generation, identity, continue_current=(
                d['schema'] == 2 and d.get('recovery_mode') == 'continue_unattempted'))

    async def verify(self, scene_id, skip=()):
        async with self._admit() as (generation, identity):
            row = self._require(scene_id); self._present(row); targets = self._targets()
            skip = self._skip(skip, targets, [], '')
            if all(m['target'] in skip for m in row['members']):
                raise ValueError('At least one light in this scene must answer')
            stored = self._expected(row, targets)
            self._revoke(scene_id)
            observed = await self._survey(row['reference'], targets, generation, identity, skip, stored)
            if observed != stored: raise RuntimeError('Scene ownership or preset changed; activation blocked')
            self._clear_aside(scene_id)
            self._note_unchecked(scene_id, skip)
            self._grant(row)
            self._changed()
            return {'scene_id': scene_id, 'verified': True, 'unchecked': skip}

    def set_aside(self, transaction):
        """Owner decision without radio work: stop waiting for an unreadable result.

        The receipt is kept as retired. If any command may have reached a light,
        the reference stays reserved and is never reused or recalled. A scene
        that already existed stays listed and needs a fresh verification.
        """
        if self.r.operation.locked(): raise RuntimeError('Wait for the running operation to finish')
        j = next((j for j in self.r.store.journals(pending=True)
                  if j['id'] == transaction and j['kind'] == 'scene'), None)
        if j is None: raise ValueError('Unknown unfinished scene operation')
        d = j['detail'] if isinstance(j['detail'], dict) else {}
        steps = d.get('steps') if isinstance(d.get('steps'), list) else None
        untouched = steps is not None and all(isinstance(step, dict) and step.get('status') == 'not_started'
                                              and not step.get('attempts') for step in steps)
        reference, scene_id = d.get('reference'), d.get('scene_id')
        reserve = (not untouched and type(reference) is int and 1 <= reference <= 20
                   and isinstance(scene_id, str))
        try: kept = isinstance(scene_id, str) and self._row(scene_id) is not None
        except RuntimeError: kept = False
        with self.r.store.transaction():
            if reserve: self._reserve_aside(reference, scene_id, transaction, 'operation_set_aside')
            d.update(completed=False, outcome='set_aside', recovery_ready=False,
                     set_aside={'at': int(time.time()), 'commands_possible': not untouched,
                                'reference_reserved': reserve})
            changed = self.r.store.db.execute(
                "UPDATE journals SET detail=?,status='retired' WHERE id=? AND status=?",
                (json.dumps(d), transaction, j['status'])).rowcount
            if changed != 1: raise RuntimeError('Operation changed; nothing was set aside')
        if isinstance(scene_id, str): self._revoke(scene_id)
        self._changed()
        return {'transaction': transaction, 'set_aside': True, 'verified': False, 'write_sent': False,
                'reference_reserved': reserve, 'scene_kept': kept}

    def forget(self, scene_id):
        """Owner decision without radio work: stop using a scene the lights cannot confirm.

        Nothing is cleared from the lights, so the reference stays reserved.
        """
        if self.r.operation.locked(): raise RuntimeError('Wait for the running operation to finish')
        if any(j['detail']['scene_id'] == scene_id for j in self._pending()):
            raise RuntimeError('Set the unfinished scene operation aside first')
        rows = self._rows(); row = next((x for x in rows if x['id'] == scene_id), None)
        if row is None: raise ValueError('Unknown scene')
        with self.r.store.transaction():
            if not any(e['scene_id'] == scene_id for e in self._aside()):
                self._reserve_aside(row['reference'], scene_id, None, 'scene_forgotten')
            self.r.store.set(KEY, [x for x in rows if x['id'] != scene_id])
            self._note_unchecked(scene_id, [])
        self._revoke(scene_id)
        self._changed()
        return {'scene_id': scene_id, 'forgotten': True, 'write_sent': False,
                'reference_reserved': True, 'reference': row['reference']}

    async def activate(self, scene_id, *, admission=None, recall_profile='halo_app'):
        started = time.monotonic()
        async with self._admit() as (generation, identity):
            row = self._require(scene_id); self._present(row)
            if not self._leased(row):
                raise RuntimeError('Scene settings need verification. Open Edit / details and choose Verify saved settings, then activate again.')
            self._fence(generation, identity)
            before_send = time.monotonic()
            admission_started = admission_finished = None
            def admit_recall():
                nonlocal admission_started, admission_finished
                admission_started = time.monotonic()
                self._fence(generation, identity)
                self._available()
                if not self._leased(row):
                    raise RuntimeError('Scene changed before activation; verify saved settings first')
                if admission: admission()
                admission_finished = time.monotonic()
            await self.r.send(candidate_recall(row['reference'], profile=recall_profile), generation=generation, admission=admit_recall)
            submitted = time.monotonic()
            callback = getattr(self.r, 'scene_activated', None)
            if callback:
                try: callback(row)
                except Exception: pass
            finished = time.monotonic()
            elapsed = lambda end, begin: round(max(0, end - begin) * 1000, 1)
            # Submission is the transport coroutine returning, not a radio ACK
            # or physical actuation. Keep the pre-send durable sequence work
            # separate from final admission and the Bluetooth handoff.
            timing = {'checks_ms': elapsed(before_send, started),
                'prepare_frame_ms': elapsed(admission_started, before_send) if admission_started is not None else None,
                'dispatch_checks_ms': elapsed(admission_finished, admission_started) if admission_finished is not None else None,
                'submission_ms': elapsed(submitted, admission_finished) if admission_finished is not None else None,
                'handler_to_submission_ms': elapsed(submitted, started),
                'after_submission_ms': elapsed(finished, submitted),
                'physical_response_measured': False}
            return {'scene_id': scene_id, 'sent': True, 'physical_state_confirmed': False,
                    'dispatch_ms': elapsed(finished, started), 'timing': timing, 'recall_profile': recall_profile,
                    'verification_reads': 0, 'recall_commands': 1}

    def set_exposed(self, scene_id, exposed):
        if type(exposed) is not bool: raise ValueError('Expected boolean exposure')
        if any(j['detail']['scene_id'] == scene_id for j in self._pending()):
            raise RuntimeError('Recover the unfinished scene operation first')
        row = self._require(scene_id); old = self._stamp(row); rows = self._rows()
        for value in rows:
            if value['id'] == scene_id: value['exposed'] = exposed; row = value
        self.r.store.set(KEY, rows)
        if self.leases.get(scene_id) == old: self.leases[scene_id] = self._stamp(row)
        self._changed()
        return {'scene_id': scene_id, 'exposed': exposed}
