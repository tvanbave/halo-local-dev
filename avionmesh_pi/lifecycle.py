"""Retained removal receipts and exact-UUID reconciliation; never resend removal."""
import asyncio
import copy
import hashlib
import json
import uuid

from .bridge import Bridge, LIGHTS, load_devices
from .association import uuid_hash
from .protocol import integer, ping_command, ping_match
from .identity import advertised_identity, paired_identity, ping_token
from .errors import OperationNotStarted
from .cloud_provenance import device_source, retain_cloud_devices


def reload_inventory(runtime):
    """Refresh publication state after an already committed metadata transaction."""
    # Keep original timestamps only for unchanged identities and exposure.
    # Newly exposed/retyped devices require new observations; real disconnects
    # still clear all evidence through the ordinary session() path.
    before = {d.device_id: (d.product_id, d.exposed) for d in runtime.core.devices.values()}
    observed = {target: runtime._light_observations(target) for target in before}
    old = {m['topic'] for m in runtime.discovery() if m['topic'].startswith('homeassistant/')}
    runtime.core = Bridge(load_devices(runtime.store.inventory()['devices']), runtime.key, runtime.core.node)
    runtime.core.controllers.local_modes = set(runtime.store.get('local_modes', []))
    new = {m['topic'] for m in runtime.discovery() if m['topic'].startswith('homeassistant/')}
    tombstones = set(runtime.store.get('discovery_tombstones', [])) | (old - new)
    runtime.store.set('discovery_tombstones', sorted(tombstones))
    retained = {d.device_id: observed[d.device_id] for d in runtime.core.devices.values()
                if before.get(d.device_id) == (d.product_id, d.exposed) and d.product_id in LIGHTS
                and observed[d.device_id]}
    runtime.local_light_states = retained
    runtime.session(preserve_lights=retained)
    runtime.note_added_lights(set(runtime.core.devices) - set(before))
    runtime.emit(runtime.discovery())


class Lifecycle:
    def __init__(self, runtime, commissioning):
        self.runtime, self.commissioning = runtime, commissioning

    def _removal_claim(self, target, *, legacy_accessory_reset=False):
        """Only one current, complete retained association can authorize removal."""
        r = self.runtime
        devices = [d for d in r.store.inventory()['devices'] if d['device_id'] == target]
        retired = set(r.store.get('retired_claims', []))
        claims = [j for j in r.store.journals() if j['kind'] == 'claim'
                  and j['target'] == target and j['status'] == 'verified' and j['id'] not in retired]
        if (len(devices) != 1 or len(claims) != 1 or target not in r.core.devices
                or target in r.store.get('retired_addresses', [])):
            raise ValueError('Unique verified retained identity required for removal')
        claim, device = claims[0], devices[0]
        detail = claim['detail']
        identity = detail.get('uuid')
        try:
            raw = bytes.fromhex(identity) if isinstance(identity, str) and len(identity) == 32 else b''
        except ValueError:
            raw = b''
        if (len(raw) != 16 or not any(raw) or detail.get('product_id') != device['product_id']
                or r.core.devices[target].product_id != device['product_id']
                or (detail.get('uuid_hash') != uuid_hash(raw) and not
                    (legacy_accessory_reset and device['product_id'] == 91 and 'uuid_hash' not in detail))):
            raise ValueError('Exact full UUID and matching retained product required')
        if any(j['kind'] == 'claim' and j['status'] == 'verified' and j['id'] not in retired
               and j['id'] != claim['id'] and str(j['detail'].get('uuid', '')).lower() == identity.lower()
               for j in r.store.journals()):
            raise ValueError('Retained identity is ambiguous')
        return claim, device

    def _removal_authority(self, target):
        """Keep imported origin distinct from completed association evidence."""
        r = self.runtime
        # Any claim history (including retired/incomplete) must use its existing
        # strict path; cloud metadata never repairs a missing or corrupt claim.
        if any(j['kind']=='claim' and j['target']==target for j in r.store.journals()):
            claim, device = self._removal_claim(target)
            return {'identity_source':'local_claim','claim':claim}, device
        device = next((d for d in r.store.inventory()['devices'] if d['device_id']==target),None)
        if (device is None or target not in r.core.devices or device['product_id'] not in LIGHTS
                or r.core.devices[target].product_id != device['product_id']
                or target in r.store.get('retired_addresses', [])):
            raise ValueError('Supported imported light identity required')
        source = device_source(r.store, target)
        if source['product_id'] != device['product_id']:
            raise ValueError('Imported product identity changed')
        return {'identity_source':'cloud_import','cloud_source':source}, device

    def _removal_dependencies(self, target):
        r = self.runtime
        inventory = r.store.inventory()
        known = {d['device_id'] for d in inventory['devices']}
        references = set()
        # Retained tables remain useful dependency evidence after a reconnect has
        # invalidated their current-session verification. Never use them as proof
        # of an assignment's absence.
        for source, raw in r.store.db.execute('SELECT target,snapshot FROM bindings'):
            if source in known and source != target:
                for row in json.loads(raw).get('slots', []):
                    if type(row.get('target')) is int and row['target'] != 65535:
                        references.add((source, row['target']))
        for source, (event, _) in r.native_observations.items():
            if source in known and source != target and type(event.get('target')) is int:
                references.add((source, event['target']))
        for destination, sources in r.store.get('target_sources', {}).items():
            if str(destination).isdigit():
                references.update((source, int(destination)) for source in sources
                                  if source in known and source != target)
        for journal in r.store.journals():
            if (journal['kind'] in ('accessory_native_setup', 'dimmer_edit') and journal['status'] == 'verified'
                    and journal['target'] in known and journal['target'] != target):
                destination = journal['detail'].get('reservation', {}).get('group_id')
                if type(destination) is int:
                    references.add((journal['target'], destination))
        memberships = [copy.deepcopy(g) for g in inventory['groups'] if target in g['members']]
        destinations = {target} | {g['group_id'] for g in memberships}
        dependencies = [{'controller': source, 'destination': destination,
                         'direct': destination == target}
                        for source, destination in sorted(references) if destination in destinations]
        return memberships, dependencies

    def preview_remove(self, target):
        """Read-only impact review; capability enablement does not supply identity."""
        r = self.runtime
        integer(target, 32896, 65407, 'device')
        authority, device = self._removal_authority(target)
        imported = authority['identity_source']=='cloud_import'
        groups, dependencies = self._removal_dependencies(target)
        address = None if imported else ':'.join(f'{b:02X}' for b in bytes.fromhex(authority['claim']['detail']['uuid'])[:6][::-1])
        active_relay_address = getattr(r.transport, 'address', None)
        active_relay = address is not None and isinstance(active_relay_address, str) and active_relay_address.upper() == address
        blockers = []
        if active_relay:
            blockers.append('active_relay_requires_reconnection')
        if any(d['direct'] for d in dependencies):
            blockers.append('native_controller_targets_device')
        emptied = {g['group_id'] for g in groups if g['members'] == [target]}
        if any(d['destination'] in emptied for d in dependencies):
            blockers.append('native_controller_group_would_be_empty')
        review = {'target': target, 'name': device['name'], 'product_id': device['product_id'],
                  'exposed': device['exposed'], 'affected_groups': groups,
                  'native_dependencies': dependencies, 'blockers': blockers,
                  'native_dependency_evidence': 'retained_and_observed_only',
                  'requires_exact_uuid_scan': not imported, 'ha_automation_dependencies_verified': False,
                  'identity_source':authority['identity_source'], 'requires_owner_confirmation':imported,
                  'verification_mode':'owner_confirmed_unpaired' if imported else 'exact_uuid',
                  'active_relay_identity_known':not imported}
        authority = {'authority': authority, 'device': device, 'groups': groups,
                     'dependencies': dependencies, 'active_relay': active_relay,
                     'room': r.store.get('device_rooms', {}).get(str(target))}
        review['revision'] = hashlib.sha256(json.dumps(authority, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        review['eligible'] = not blockers
        review['enabled'] = bool(not blockers and r.capabilities.get('removal')
            and r.mesh_ready and r.transport.ready and not r.operation.locked()
            and not r.store.journals(pending=True))
        return review

    def _anyplace_reset_claim(self, setup):
        """Validate retained identity and reservation; this supplies no reset command."""
        store = self.runtime.store
        detail = setup.get('detail', {})
        if (setup['kind'] != 'anyplace_setup' or setup['status'] != 'uncertain'
                or detail.get('product_id') != 81 or detail.get('assignment_attempted') is not True
                or detail.get('original_assignment') != 'unknown'):
            raise ValueError('Expected an attempted Anyplace setup with retained unknown original')
        claims = [j for j in store.journals() if j['id'] == detail.get('claim')
                  and j['kind'] == 'claim' and j['target'] == setup['target']
                  and j['status'] == 'verified' and j['id'] not in store.get('retired_claims', [])
                  and j.get('detail', {}).get('product_id') == 81]
        inventory = store.inventory()
        devices = [d for d in inventory['devices'] if d['device_id'] == setup['target']]
        reservation = detail.get('reservation', {})
        group = reservation.get('group_id')
        if (len(claims) != 1 or len(devices) != 1 or devices[0]['product_id'] != 81
                or setup['target'] in store.get('retired_addresses', [])
                or type(group) is not int or not 256 <= group <= 24575
                or reservation.get('protected') is not True or reservation.get('members') != []
                or reservation.get('exposed') is not False or reservation not in inventory['groups']
                or sum(g['group_id'] == group for g in inventory['groups']) != 1):
            raise ValueError('Anyplace reset identity or retained reservation changed')
        return claims[0]

    def _accessory_reset_claim(self, setup):
        """Validate old Accessory claims without inventing missing stored fields."""
        detail = setup.get('detail', {})
        if (setup['kind'] != 'accessory_setup' or setup['status'] != 'uncertain'
                or detail.get('assignment_attempted') is not True
                or detail.get('original_assignment') != 'unknown'):
            raise ValueError('Expected an attempted Accessory assignment')
        claim, device = self._removal_claim(setup['target'], legacy_accessory_reset=True)
        if device['product_id'] != 91 or claim['id'] != detail.get('claim'):
            raise ValueError('Accessory reset claim no longer matches retained identity')
        return claim

    async def reconcile_reset(self, identifier, owner_reset):
        """Observe an owner-performed reset; no reset, remove or programming TX."""
        r = self.runtime
        if owner_reset is not True:
            raise ValueError('Owner-performed reset must be explicitly declared')
        pending = r.store.journals(pending=True)
        prior = next((j for j in pending if j['id']==identifier),None)
        if not prior or prior['kind'] not in ('accessory_setup','anyplace_setup','accessory_native_setup') or prior['status']!='uncertain' or prior['target']==32898:
            raise ValueError('Only nonlegacy interrupted managed dimmer setup supports reset reconciliation')
        if r.operation.locked() or not r.mesh_ready:
            raise RuntimeError('Mesh busy or offline')
        claim = (self._anyplace_reset_claim(prior) if prior['kind']=='anyplace_setup' else
                 self._removal_claim(prior['target'])[0] if prior['kind']=='accessory_native_setup' else
                 self._accessory_reset_claim(prior))
        if prior['kind'] == 'accessory_native_setup' and (claim['id'] != prior['detail'].get('claim')
                or prior['detail'].get('assignment_attempted') is not True
                or prior['detail'].get('product_id') != 91):
            raise ValueError('Native setup reset requires its retained attempted assignment')
        identity = claim['detail']['uuid']
        if len(identity)!=32 or not any(bytes.fromhex(identity)):
            raise ValueError('Exact full claim UUID required')
        existing = next((j for j in pending if j['kind']=='external_reset' and j['detail']['setup']==identifier),None)
        if any(j['id'] not in (identifier, existing['id'] if existing else None) for j in pending):
            raise RuntimeError('Another pending operation prevents reset reconciliation')
        async with r.operation:
            if existing:
                record = existing['id']
            else:
                record = uuid.uuid4().hex
                detail = {'uuid':identity,'uuid_hash':uuid_hash(bytes.fromhex(identity)),
                          'claim':claim['id'],'claim_receipt':copy.deepcopy(claim),
                          'setup':identifier,'original_receipt':copy.deepcopy(prior),
                          'backup':copy.deepcopy(r.store.inventory()),'reset_dispatched':False}
                with r.store.transaction():
                    r.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
                        (record,'external_reset',prior['target'],'prepared',json.dumps(detail)))
        return await self.reconcile(record)

    async def remove(self, target, *, expected_revision=None):
        r = self.runtime
        integer(target, 32896, 65407, 'device')
        if not r.capabilities.get('removal'):
            raise ValueError('Removal capability disabled')
        if r.operation.locked() or not r.mesh_ready or r.store.journals(pending=True):
            raise RuntimeError('Mesh busy, offline or a transaction needs reconciliation')
        review = self.preview_remove(target)
        if not review['eligible']:
            raise ValueError('Removal dependency requires review: ' + ', '.join(review['blockers']))
        if expected_revision is not None and expected_revision != review['revision']:
            raise ValueError('Removal review changed; preview again')
        authority, device = self._removal_authority(target)
        if authority['identity_source']=='cloud_import':
            return await self._remove_imported(target, authority, device, review)
        claim = authority['claim']
        identity, generation = claim['detail']['uuid'], r.generation
        async with r.operation:
            if (generation != r.generation or not r.mesh_ready or not r.transport.ready
                    or r.store.journals(pending=True)
                    or self.preview_remove(target)['revision'] != review['revision']):
                raise RuntimeError('Removal admission changed')
            record = r.store.begin('remove', target, {'uuid': identity, 'claim': claim['id'],
                'product_id': device['product_id'], 'review': review,
                'uuid_hash': uuid_hash(bytes.fromhex(identity)),
                'backup': copy.deepcopy(r.store.inventory()), 'bindings': r.store.bindings()})
            r.store.transition(record, 'prepared', 'attempted')
            r.session(preserve_lights=set(r.core.devices) - {target})
            r.emit(r.discovery())
            generation = r.generation
            def admit():
                if (generation != r.generation or not r.mesh_ready or not r.transport.ready
                        or self.preview_remove(target)['revision'] != review['revision']
                        or any(j['id'] != record for j in r.store.journals(pending=True))):
                    raise RuntimeError('Removal admission changed before dispatch')
            try:
                # recsrmesh mcp::build_disassociate: direct destination, opcode04,
                # ten zeros. Transport acceptance/ACK/silence never proves reset.
                await r.send(target.to_bytes(2, 'little') + b'\x04' + bytes(10),
                             generation=generation, admission=admit)
            except BaseException:
                try:
                    r.store.transition(record, 'attempted', 'uncertain')
                finally:
                    await r.transport.disconnect()
                    r.mesh_ready = False
                    r.session()
                raise
            r.store.transition(record, 'attempted', 'uncertain')
            return {'transaction': record, 'transport_accepted': True,
                    'removed': False, 'needs_exact_uuid_scan': True}

    async def remove_and_verify(self, target, *, expected_revision=None):
        """Send removal once, then make at most three fresh unpaired scans.

        A missing reply only permits another scan. Failed/interrupted verification
        leaves the durable uncertain receipt for Operations, never another setter.
        """
        sent = await self.remove(target, expected_revision=expected_revision)
        if sent.get('identity_source') == 'cloud_import':
            try:
                return dict(sent, **await self.reconcile(sent['transaction']))
            except Exception:
                return dict(sent, requires_review=True, owner_confirmation_required=True,
                            verification='owner_confirmation_required', removal_candidates=[], write_retried=False)
        generation = self.runtime.generation
        pending = dict(sent, write_retried=False, requires_review=True,
                       verification='needs_review', verification_attempts=0)
        for attempt in range(1, 4):
            if (generation != self.runtime.generation or not self.runtime.mesh_ready
                    or not self.runtime.transport.ready):
                break
            pending['verification_attempts'] = attempt
            try:
                result = await asyncio.wait_for(self.reconcile(sent['transaction']), timeout=7)
            except Exception:
                # Preserve the attempt identity even if verification fails. A task
                # cancellation is deliberately propagated for orderly shutdown.
                break
            if result.get('removed') is True:
                return dict(sent, **result, needs_exact_uuid_scan=False,
                            verification='verified', verification_attempts=attempt)
            if attempt < 3:
                await asyncio.sleep(1)
        return pending

    async def _remove_imported(self, target, authority, device, review):
        r = self.runtime
        generation = r.generation
        try:
            baseline = await self.commissioning.discover()
            if getattr(self.commissioning, 'scan_truncated', False):
                raise ValueError('Truncated baseline')
            identities = self._scan_identities(baseline)
        except Exception:
            raise OperationNotStarted('removal_baseline_unavailable') from None
        if (generation != r.generation or not r.mesh_ready or not r.transport.ready):
            raise OperationNotStarted('session_changed')
        if r.operation.locked() or r.store.journals(pending=True):
            raise OperationNotStarted('mesh_busy')
        async with r.operation:
            if self.preview_remove(target)['revision'] != review['revision']:
                raise OperationNotStarted('removal_review_changed')
            try:
                token = ping_token(r.store)
                reply = await r.exchange(ping_command(target, token),
                    lambda p: ping_match(p, target, token), timeout=5)
                paired_identity(reply.payload)
            except Exception:
                raise OperationNotStarted('removal_target_unresponsive') from None
            if generation != r.generation or not r.mesh_ready or not r.transport.ready:
                raise OperationNotStarted('session_changed')
            if self.preview_remove(target)['revision'] != review['revision']:
                raise OperationNotStarted('removal_review_changed')
            # Keep legacy owner-selected addressing distinct from cloud-record
            # identity. Preview never modifies provenance.
            sources=r.store.get('cloud_device_sources',{})
            sources.setdefault(str(target),copy.deepcopy(authority['cloud_source']))
            r.store.set('cloud_device_sources',sources)
            retained, current = self._removal_authority(target)
            if retained != authority or current != device:
                raise OperationNotStarted('removal_review_changed')
            record = r.store.begin('remove', target, {'identity_source':'cloud_import',
                'cloud_source':authority['cloud_source'], 'product_id':device['product_id'],
                'baseline_uuids':sorted(identities), 'review':review,
                'relay_at_dispatch':getattr(r.transport,'address',None),
                'backup':copy.deepcopy(r.store.inventory()), 'bindings':r.store.bindings(),
                'paired_ping':{'sequence':reply.sequence,'token':token},
                'verification_mode':'owner_confirmed_unpaired','offered_candidates':[]})
            r.store.transition(record, 'prepared', 'attempted')
            r.session(preserve_lights=set(r.core.devices) - {target})
            r.emit(r.discovery())
            generation = r.generation
            def admit():
                current_authority, current_device = self._removal_authority(target)
                if (generation != r.generation or not r.mesh_ready or not r.transport.ready
                        or current_authority != authority or current_device != device
                        or self.preview_remove(target)['revision'] != review['revision']
                        or any(j['id']!=record for j in r.store.journals(pending=True))):
                    raise RuntimeError('Imported removal changed before dispatch')
            try:
                await r.send(target.to_bytes(2,'little') + b'\x04' + bytes(10),
                             generation=generation, admission=admit)
            except BaseException:
                try:r.store.transition(record,'attempted','uncertain')
                finally:
                    await r.transport.disconnect();r.mesh_ready=False;r.session()
                raise
            r.store.transition(record,'attempted','uncertain')
            return {'transaction':record,'identity_source':'cloud_import',
                'transport_accepted':True,'removed':False,'needs_exact_uuid_scan':False,
                'owner_confirmation_required':True,'verification':'owner_confirmation_required'}

    @staticmethod
    def _scan_identities(observations):
        if not isinstance(observations,list) or len(observations)>128:
            raise ValueError('Bounded complete scan required')
        result = {}
        for row in observations:
            identity = row.get('uuid')
            raw = bytes.fromhex(identity) if isinstance(identity,str) and len(identity)==32 else b''
            if (len(raw)!=16 or not any(raw) or raw.hex()!=identity
                    or row.get('uuid_hash')!=uuid_hash(raw) or identity in result):
                raise ValueError('Exact unique scan identity required')
            result[identity]=row
        return result

    async def _imported_observations(self, journal):
        r = self.runtime
        authority, device = self._removal_authority(journal['target'])
        if (authority.get('cloud_source') != journal['detail']['cloud_source']
                or device['product_id'] != journal['detail']['product_id']):
            raise ValueError('Imported removal authority changed')
        generation = r.generation
        observations = self._scan_identities(await self.commissioning.discover())
        if (getattr(self.commissioning,'scan_truncated',False) or generation != r.generation
                or not r.mesh_ready or not r.transport.ready):
            raise RuntimeError('Imported removal scan incomplete or session changed')
        current = next((j for j in r.store.journals() if j['id']==journal['id']),None)
        if current != journal:
            raise ValueError('Imported removal receipt changed')
        baseline=set(journal['detail']['baseline_uuids'])
        known={str(j['detail'].get('uuid','')).lower() for j in r.store.journals()
               if j['kind']=='claim'}
        conflicts=set(r.store.get('unpaired_hash_conflicts',[]))
        candidates=[{'uuid':identity,'uuid_hash':row['uuid_hash'],'product_id':device['product_id']}
            for identity,row in observations.items() if identity not in baseline and identity not in known
            and row['uuid_hash'] not in conflicts
            and advertised_identity(identity)['candidate_model']==device['product_id']]
        return candidates, generation

    async def confirm_imported_removal(self, identifier, identity, owner_confirmed):
        """Owner identifies physical reset plus fresh unpaired evidence; no reset TX."""
        r=self.runtime
        if owner_confirmed is not True:
            raise ValueError('Physical reset confirmation required')
        journal=next((j for j in r.store.journals() if j['id']==identifier),None)
        if (journal is None or journal['kind']!='remove'
                or journal['detail'].get('identity_source')!='cloud_import'):
            raise ValueError('Expected imported removal receipt')
        if journal['status']=='verified':
            if journal['detail'].get('uuid')!=identity:
                raise ValueError('Confirmed unpaired identity changed')
            return await self.reconcile(identifier)
        if journal['status']!='uncertain' or any(j['id']!=identifier for j in r.store.journals(pending=True)):
            raise RuntimeError('Imported removal is not awaiting confirmation')
        if identity not in journal['detail'].get('offered_candidates',[]):
            raise ValueError('Read fresh removal candidates before confirmation')
        candidates,generation=await self._imported_observations(journal)
        # Selection must remain unique and fresh, not a value supplied by the UI.
        matches=[c for c in candidates if c['uuid']==identity]
        if len(candidates)!=1 or len(matches)!=1:
            raise ValueError('Exactly one fresh matching unpaired candidate is required')
        if r.operation.locked() or generation!=r.generation:
            raise RuntimeError('Imported removal confirmation became busy')
        async with r.operation:
            current=next(j for j in r.store.journals() if j['id']==identifier)
            if current!=journal or any(j['id']!=identifier for j in r.store.journals(pending=True)):
                raise RuntimeError('Imported removal changed during confirmation')
            authority, device=self._removal_authority(journal['target'])
            if authority['cloud_source']!=journal['detail']['cloud_source']:
                raise ValueError('Imported authority changed')
            proof=dict(journal['detail'],uuid=identity,uuid_hash=matches[0]['uuid_hash'],
                verification_proof={'kind':'owner_confirmed_observed_unpaired','owner_confirmed':True,
                                    'prior_uuid_known':False})
            return self._finish_removal(dict(journal,detail=proof))

    def _finish_removal(self, journal):
        """Retire inventory atomically after the branch-specific evidence check."""
        r=self.runtime;target=journal['target'];identifier=journal['id']
        old_topics={m['topic'] for m in r.discovery() if m['topic'].startswith('homeassistant/')
                    and (f'_{target}/' in m['topic'] or f'_{target}_' in m['topic'])}
        with r.store.transaction():
            data=copy.deepcopy(r.store.inventory())
            data['devices']=[d for d in data['devices'] if d['device_id']!=target]
            for group in data['groups']:group['members']=[i for i in group['members'] if i!=target]
            r.store.set('inventory',data)
            claim=journal['detail'].get('claim')
            if claim is not None:r.store.set('retired_claims',sorted(set(r.store.get('retired_claims',[]))|{claim}))
            r.store.set('retired_addresses',sorted(set(r.store.get('retired_addresses',[]))|{target}))
            r.store.set('local_modes',[i for i in r.store.get('local_modes',[]) if i!=target])
            rooms=r.store.get('device_rooms',{})
            if isinstance(rooms,dict):r.store.set('device_rooms',{k:v for k,v in rooms.items() if str(k)!=str(target)})
            r.store.set('discovery_tombstones',sorted(set(r.store.get('discovery_tombstones',[]))|old_topics))
            r.store.db.execute('DELETE FROM bindings WHERE target=?',(target,))
            r.store.db.execute("UPDATE journals SET status='verified',detail=? WHERE id=?",
                (json.dumps(dict(journal['detail'],publication_pending=True)),identifier))
            r.store.db.execute("UPDATE journals SET status='retired' WHERE target=? AND kind IN ('accessory_setup','anyplace_setup','keypad_setup','accessory_native_setup','dimmer_edit')",(target,))
        return self._publish_removal(identifier)

    def _publish_removal(self, identifier):
        """Refresh committed inventory/publication only; never send a mesh write."""
        r = self.runtime
        result = {'removed': True, 'transaction': identifier, 'write_retried': False,
                  'address_retired': True, 'receipt_retained': True}
        try:
            reload_inventory(r)
            current = next(j for j in r.store.journals() if j['id'] == identifier)
            detail = dict(current['detail'], publication_pending=False)
            r.store.db.execute('UPDATE journals SET detail=? WHERE id=?',
                               (json.dumps(detail), identifier))
        except Exception:
            # The exact-UUID proof and retirement are already durable. Do not
            # turn a publication failure into an ambiguous removal or retry it.
            r.mesh_ready = False
            return dict(result, publication_pending=True, requires_review=True)
        return dict(result, publication_pending=False)

    async def reconcile(self, identifier):
        r = self.runtime
        journal = next((j for j in r.store.journals() if j['id'] == identifier), None)
        if not journal or journal['kind'] not in ('remove','external_reset'):
            raise ValueError('Expected removal receipt')
        if journal['status'] == 'verified':
            if journal['detail'].get('publication_pending') or journal['target'] in r.core.devices:
                if r.operation.locked() or r.store.journals(pending=True):
                    raise RuntimeError('Another operation prevents publication cleanup')
                async with r.operation:
                    return self._publish_removal(identifier)
            return {'removed': True, 'transaction': identifier, 'write_retried': False,
                    'address_retired': True, 'publication_pending': False}

        if journal['status'] != ('prepared' if journal['kind']=='external_reset' else 'uncertain'):
            raise ValueError('Removal has not been attempted')
        allowed = {identifier}
        if journal['kind'] == 'external_reset':
            allowed.add(journal['detail']['setup'])
        if any(j['id'] not in allowed for j in r.store.journals(pending=True)):
            raise RuntimeError('Another transaction needs reconciliation')
        if journal['detail'].get('identity_source')=='cloud_import':
            candidates, _ = await self._imported_observations(journal)
            detail=dict(journal['detail'],offered_candidates=sorted(c['uuid'] for c in candidates))
            r.store.db.execute('UPDATE journals SET detail=? WHERE id=?',(json.dumps(detail),identifier))
            return {'removed':False,'transaction':identifier,'requires_review':True,'write_retried':False,
                    'identity_source':'cloud_import','owner_confirmation_required':True,
                    'verification':'owner_confirmation_required','removal_candidates':candidates}
        generation = r.generation
        # Always initiate a NEW scan after the attempt, including after restart.
        # Never accept a caller-provided UUID or a previous discovery cache.
        observations = await self.commissioning.discover()
        identity, fingerprint = journal['detail']['uuid'], journal['detail']['uuid_hash']
        matches = [o for o in observations if o['uuid_hash'] == fingerprint]
        if len(matches) != 1 or matches[0]['uuid'] != identity:
            return {'removed': False, 'requires_review': True, 'write_retried': False}
        if r.operation.locked() or generation != r.generation or not r.mesh_ready or not r.transport.ready:
            raise RuntimeError('Removal scan belongs to a busy or disconnected session')
        async with r.operation:
            target = journal['target']
            old_topics = {m['topic'] for m in r.discovery() if m['topic'].startswith('homeassistant/')
                          and (f'_{target}/' in m['topic'] or f'_{target}_' in m['topic'])}
            with r.store.transaction():
                current = next(j for j in r.store.journals() if j['id'] == identifier)
                if current != journal:
                    raise ValueError('Removal state changed')
                if any(j['id'] not in allowed for j in r.store.journals(pending=True)):
                    raise RuntimeError('Another transaction needs reconciliation')
                if journal['kind'] == 'remove':
                    claim, device = self._removal_claim(target)
                    # Older receipts carry the product in their inventory backup.
                    original = next((d for d in journal['detail']['backup']['devices']
                                     if d['device_id'] == target), None)
                    if (claim['id'] != journal['detail']['claim'] or claim['detail']['uuid'] != identity
                            or uuid_hash(bytes.fromhex(identity)) != fingerprint
                            or not original or original['product_id'] != device['product_id']):
                        raise ValueError('Removal claim no longer matches retained identity')
                if journal['kind']=='external_reset':
                    child = next(j for j in r.store.journals() if j['id']==journal['detail']['setup'])
                    if child != journal['detail']['original_receipt'] or child['status']!='uncertain':
                        raise ValueError('Reset authority no longer matches the retained original receipt')
                    if child['kind']=='accessory_setup':
                        claim = self._accessory_reset_claim(child)
                        original = next((d for d in journal['detail']['backup']['devices']
                                         if d['device_id'] == target), None)
                        retained = journal['detail'].get('claim_receipt')
                        if (child['target'] != target or claim['id'] != journal['detail']['claim']
                                or claim['detail']['uuid'] != identity
                                or uuid_hash(bytes.fromhex(identity)) != fingerprint
                                or not original or original['product_id'] != 91
                                or (retained is not None and claim != retained)):
                            raise ValueError('Accessory reset claim no longer matches retained identity')
                    if child['kind']=='anyplace_setup':
                        claim = self._anyplace_reset_claim(child)
                        if (child['target'] != target or claim['target'] != target
                                or claim['id'] != journal['detail']['claim']
                                or claim['detail']['uuid'] != identity
                                or uuid_hash(bytes.fromhex(identity)) != fingerprint):
                            raise ValueError('Anyplace reset claim no longer matches its retained identity')
                    if child['kind']=='accessory_native_setup':
                        claim, device = self._removal_claim(target)
                        if (child['target'] != target or device['product_id'] != 91
                                or claim['id'] != journal['detail']['claim']
                                or claim['id'] != child['detail'].get('claim')
                                or claim['detail']['uuid'] != identity
                                or uuid_hash(bytes.fromhex(identity)) != fingerprint):
                            raise ValueError('Native setup reset claim no longer matches its retained identity')
                data = copy.deepcopy(r.store.inventory())
                data['devices'] = [d for d in data['devices'] if d['device_id'] != target]
                for group in data['groups']:
                    group['members'] = [i for i in group['members'] if i != target]
                # Protected event destinations remain reserved forever, including
                # those belonging to a removed device. Reclaim allocates anew.
                r.store.set('inventory', data)
                r.store.set('retired_claims', sorted(set(r.store.get('retired_claims', [])) | {journal['detail']['claim']}))
                r.store.set('retired_addresses', sorted(set(r.store.get('retired_addresses', [])) | {target}))
                r.store.set('local_modes', [i for i in r.store.get('local_modes', []) if i != target])
                rooms = r.store.get('device_rooms', {})
                if isinstance(rooms, dict):
                    r.store.set('device_rooms', {k: v for k, v in rooms.items() if str(k) != str(target)})
                r.store.set('discovery_tombstones', sorted(set(r.store.get('discovery_tombstones', [])) | old_topics))
                r.store.db.execute('DELETE FROM bindings WHERE target=?', (target,))
                r.store.db.execute("UPDATE journals SET status='verified',detail=? WHERE id=?",
                    (json.dumps(dict(journal['detail'], publication_pending=True)), identifier))
                # Preserve original records verbatim while retiring their active
                # authority. Their complete pre-reset versions are in the receipt.
                r.store.db.execute("UPDATE journals SET status='retired' WHERE target=? AND kind IN ('accessory_setup','anyplace_setup','keypad_setup','accessory_native_setup','dimmer_edit')",
                                   (target,))
            return self._publish_removal(identifier)
