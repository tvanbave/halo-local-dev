"""Default-disabled fixed-host cloud metadata sync; never sends mesh commands.

Ported from the ESP import and staged cloud_sync/cloud_inventory workflows.
Requests run only through explicit import, preview or sync actions.
Credentials/tokens are ephemeral; Python does not promise secret zeroization.
"""
import asyncio
import copy
import hmac
import hashlib
import json
import re
import sqlite3
import time
import uuid

from .protocol import derive_mcp_key, integer
from .lifecycle import reload_inventory

BASE = 'https://api.avi-on.com/'


def pid(value):
    if type(value) is int and 0 < value < 2**64:
        value = str(value)
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value):
        raise ValueError('Invalid cloud resource identifier')
    return value


def name(value):
    if (not isinstance(value,str) or not 1<=len(value.encode())<=128
            or any(ord(c)<32 or c in '"\\' for c in value)):
        raise ValueError('Invalid cloud resource name')
    return value


def records(document, field, maximum):
    rows = document.get(field) if isinstance(document,dict) else None
    if not isinstance(rows,list) or len(rows)>maximum or any(not isinstance(r,dict) for r in rows):
        raise ValueError('Invalid cloud response collection')
    return rows


def parse_inventory(devices_document, group_documents):
    devices,groups,pids = [],[],{}
    ids = set()
    for row in records(devices_document,'abstract_devices',512):
        if row.get('type') not in ('device','controller'):
            continue
        identifier = integer(row.get('avid'),32896,65407,'cloud device ID')
        resource = pid(row.get('pid'))
        if identifier in ids or resource in pids:
            raise ValueError('Duplicate cloud device')
        ids.add(identifier);pids[resource] = identifier
        devices.append({'device_id':identifier,'product_id':integer(row.get('product_id'),0,255,'product'),
                        'name':name(row.get('name')),'exposed':False})
    if not devices or len(group_documents)>128:
        raise ValueError('Empty or excessive cloud inventory')
    seen = set()
    for document in group_documents:
        row = document.get('group') if isinstance(document,dict) else None
        if not isinstance(row,dict):raise ValueError('Invalid cloud group')
        identifier = integer(row.get('avid'),256,24575,'cloud group ID')
        title = name(row.get('name'))
        if identifier in seen or title.startswith(('HA event target:','HA keypad target:','HA dimmer target:')):
            raise ValueError('Duplicate or reserved cloud group')
        seen.add(identifier)
        members = row.get('devices')
        if not isinstance(members,list) or len(members)>512:raise ValueError('Invalid group members')
        resolved = set()
        for member in members:
            if type(member) is int:
                target = member
            elif isinstance(member,str):
                target = pids.get(member)
            elif isinstance(member,dict):
                aliases = [member[k] for k in ('avid', 'avion_id', 'device_id', 'id') if k in member]
                if aliases:
                    if any(type(v) is not int or v != aliases[0] for v in aliases):
                        raise ValueError('Ambiguous group member')
                    target = aliases[0]
                else:
                    target = pids.get(pid(member.get('pid')))
            else:raise ValueError('Invalid member')
            if type(target) is not int or target not in ids:raise ValueError('Unresolved cloud group member')
            resolved.add(target)
        groups.append({'group_id':identifier,'name':title,'members':sorted(resolved),'exposed':False,'protected':False})
    return {'devices':devices,'groups':groups}


def merge_inventory(local, remote):
    result = copy.deepcopy(local)
    for device in remote['devices']:
        old = next((d for d in result['devices'] if d['device_id']==device['device_id']),None)
        if old:
            if old['product_id']!=device['product_id'] or old['name'].lower()!=device['name'].lower():
                raise ValueError('Cloud device conflicts with local identity')
        else:result['devices'].append(device)
    for group in remote['groups']:
        old = next((g for g in result['groups'] if g['group_id']==group['group_id']),None)
        if old:
            if old['protected'] or old['name']!=group['name'] or not set(old['members'])<=set(group['members']):
                raise ValueError('Cloud group conflicts with local reservation or membership')
            old['members'] = group['members']
        else:result['groups'].append(group)
    return result


class CloudSync:
    def __init__(self, runtime, session_factory=None):
        self.runtime,self.session_factory = runtime,session_factory

    async def sync(self,email,password):
        r = self.runtime
        if not r.capabilities.get('cloud_sync'):
            raise ValueError('Cloud sync disabled')
        if (not isinstance(email,str) or not 1<=len(email)<=320
                or not isinstance(password,str) or not 1<=len(password)<=4096):
            raise ValueError('Invalid cloud credentials')
        if r.operation.locked() or r.store.journals(pending=True):
            raise RuntimeError('Pending mesh transaction prevents cloud merge')
        async with r.operation:
            remote = await asyncio.wait_for(self._fetch(email,password),180)
            data = merge_inventory(r.store.inventory(),remote)
            r.store.import_inventory(data)
            reload_inventory(r)
            return {'merged':True,'devices':len(data['devices']),'groups':len(data['groups']),
                    'physical_memberships_changed':False,'new_devices_exposed':False}

    async def _fetch(self,email,password):
        async with _CloudReader(self.session_factory) as reader:
            await reader.login(email, password)
            locations = await reader.locations(self.runtime.key)
            matches = [row['id'] for row in locations if row['matching_mesh']]
            if len(matches) != 1:
                raise ValueError('Cloud account mesh mismatch or ambiguity')
            return await reader.inventory(matches[0])


class _CloudReader:
    """One request-scoped fixed-host client; never retained by the import plan."""
    def __init__(self, session_factory=None):
        self.factory = session_factory
        self.session = self.context = None
        self.token = None
        self.received = 0
        self.skipped_unaddressed = 0

    async def __aenter__(self):
        import aiohttp
        factory = self.factory or aiohttp.ClientSession
        self.context = factory(timeout=aiohttp.ClientTimeout(total=30), trust_env=False)
        self.session = await self.context.__aenter__()
        return self

    async def __aexit__(self, *args):
        try:
            return await self.context.__aexit__(*args)
        finally:
            self.token = self.session = self.context = None

    async def request(self, path, login=None):
        headers = {'Accept': 'application/api.avi-on.v3'}
        if self.token:
            headers['Authorization'] = 'Token ' + self.token
        async with self.session.request('POST' if login else 'GET', BASE + path,
                headers=headers, json=login, allow_redirects=False) as response:
            if response.status not in (200, 201):
                raise ValueError('Cloud request rejected')
            data = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                self.received += len(chunk)
                if self.received > 4 * 1024 * 1024:
                    raise ValueError('Cloud sync response budget exhausted')
                data.extend(chunk)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError('Cloud response too large')
            try:
                result = json.loads(data)
            except (ValueError, RecursionError):
                raise ValueError('Invalid cloud document') from None
            if not isinstance(result, dict):
                raise ValueError('Invalid cloud document')
            stack, nodes = [(result, 0)], 0
            while stack:
                nodes += 1
                if nodes > 50000:
                    raise ValueError('Cloud document object budget exhausted')
                value, depth = stack.pop()
                if depth > 16:
                    raise ValueError('Cloud document nesting too deep')
                if isinstance(value, dict):
                    stack.extend((v, depth + 1) for v in value.values())
                elif isinstance(value, list):
                    stack.extend((v, depth + 1) for v in value)
            return result

    async def login(self, email, password):
        document = await self.request('sessions', login={'email': email, 'password': password})
        credentials = document.get('credentials')
        if not isinstance(credentials, dict):
            raise ValueError('Invalid cloud credentials document')
        token = credentials.get('auth_token')
        if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                or '\r' in token or '\n' in token):
            raise ValueError('Invalid cloud token')
        self.token = token

    async def locations(self, key):
        found, seen = [], set()
        for row in records(await self.request('user/locations'), 'locations', 32):
            resource = pid(row.get('pid'))
            if resource in seen:
                raise ValueError('Duplicate cloud location')
            seen.add(resource)
            document = await self.request('locations/' + resource)
            location = document.get('location')
            if not isinstance(location, dict):
                raise ValueError('Invalid cloud location')
            phrase = location.get('passphrase')
            matching = (isinstance(phrase, str) and phrase and '\0' not in phrase
                and len(phrase.encode()) <= 252
                and hmac.compare_digest(derive_mcp_key(phrase), key))
            title = location.get('name', row.get('name'))
            found.append({'id': resource, 'name': name(title) if title is not None else 'Location ' + resource,
                          'matching_mesh': bool(matching)})
        return found

    async def inventory(self, resource):
        resource = pid(resource)
        devices = await self.request('locations/' + resource + '/abstract_devices')
        addressed = []
        for row in records(devices, 'abstract_devices', 512):
            # Match the ESP importer's omission of cloud records that have not
            # been assigned a mesh address. Malformed addresses still fail.
            if type(row.get('avid')) is int and row['avid'] == 0:
                self.skipped_unaddressed += 1
            else:
                addressed.append(row)
        devices = {'abstract_devices': addressed}
        groups = records(await self.request('locations/' + resource + '/groups'), 'groups', 128)
        documents, seen = [], set()
        for group in groups:
            if type(group.get('avid')) is int and group['avid'] == 0:
                self.skipped_unaddressed += 1
                continue
            identifier = pid(group.get('pid'))
            if identifier in seen:
                raise ValueError('Duplicate cloud group resource')
            seen.add(identifier)
            detail = await self.request('groups/' + identifier)
            if isinstance(detail.get('group'), dict):
                detail = copy.deepcopy(detail)
                # The ESP importer takes identity/name from the list endpoint;
                # some detail replies supply only membership.
                for field in ('avid', 'name'):
                    if field not in detail['group'] and field in group:
                        detail['group'][field] = group[field]
                    elif field in group and detail['group'].get(field) != group[field]:
                        raise ValueError('Conflicting cloud group identity')
            documents.append(detail)
        return parse_inventory(devices, documents)


def _review_merge(local, remote, known_targets):
    """Preserve local labels/exposure and never remove existing memberships."""
    result = copy.deepcopy(local)
    changes = {'devices_added': [], 'groups_added': [], 'members_added': []}
    devices = {d['device_id']: d for d in result['devices']}
    groups = {g['group_id']: g for g in result['groups']}
    for device in remote['devices']:
        old = devices.get(device['device_id'])
        if old is not None:
            if old['product_id'] != device['product_id']:
                raise ValueError('Cloud device conflicts with local identity')
            continue
        item = copy.deepcopy(device)
        item['exposed'] = False
        result['devices'].append(item)
        changes['devices_added'].append(copy.deepcopy(item))
    for group in remote['groups']:
        old = groups.get(group['group_id'])
        if old is None:
            if group['group_id'] in set(known_targets) | {24566}:
                raise ValueError('Cloud group collides with a reserved target')
            item = copy.deepcopy(group)
            item.update(exposed=False, protected=False)
            result['groups'].append(item)
            changes['groups_added'].append(copy.deepcopy(item))
        else:
            if old['protected']:
                raise ValueError('Cloud group collides with a protected target')
            added = sorted(set(group['members']) - set(old['members']))
            if added:
                old['members'] = sorted(set(old['members']) | set(added))
                changes['members_added'].append({'group_id': old['group_id'],
                                                'name': old['name'], 'members': added})
    return result, changes


class CloudImportError(ValueError):
    """Public cloud-review failures; messages never include provider responses."""
    MESSAGES = {
        'cloud_invalid_request': 'Expected a reviewed cloud preview and revision.',
        'cloud_invalid_credentials': 'Enter a cloud email and password.',
        'cloud_busy': 'The bridge is busy. Try a new preview when it is idle.',
        'cloud_pending_operation': 'Resolve unfinished operations before cloud import.',
        'cloud_identity_changed': 'The bridge mesh identity changed. Import was not started.',
        'cloud_not_admitted': 'The bridge transmit history must be admitted before importing inventory.',
        'cloud_preview_unavailable': 'The cloud preview is unavailable. Preview again.',
        'cloud_preview_expired': 'The cloud preview expired. Preview again.',
        'cloud_preview_stale': 'The inventory or protected state changed. Preview again.',
        'cloud_cancelled': 'The cloud preview was cancelled. Nothing was imported.',
        'cloud_inventory_conflict': 'The cloud inventory conflicts with a local device, protected group or reserved identity. Nothing was imported.',
        'cloud_location_unavailable': 'The selected cloud location is no longer available.',
        'cloud_timeout': 'The cloud request timed out. Nothing was imported.',
    }

    def __init__(self, code):
        if code not in self.MESSAGES:
            raise ValueError('Unknown cloud import failure')
        self.code = code
        self.message = self.MESSAGES[code]
        self.status = 504 if code == 'cloud_timeout' else 400 if code in ('cloud_invalid_request', 'cloud_invalid_credentials') else 409
        super().__init__(self.message)


class CloudImport:
    """Explicit preview/apply, with one short-lived metadata-only plan in RAM.

    This is a same-mesh inventory import, not a key adoption or transmitter
    admission mechanism. Normal bridge operation never invokes the cloud client.
    """
    def __init__(self, runtime, session_factory=None, *, clock=None):
        self.runtime, self.session_factory = runtime, session_factory
        self.clock = clock or time.monotonic
        self._plan = None
        self._lock = asyncio.Lock()
        self._epoch = 0

    def _revision(self):
        store = self.runtime.store
        keys = ('inventory', 'known_targets', 'target_sources', 'retired_addresses',
                'retired_claims', 'state_id', 'mesh_fingerprint', 'tx_admission',
                'tx_retirement', 'managed_keypads', 'local_modes', 'cloud_device_sources')
        data = {key: store.get(key) for key in keys}
        data['journals'] = store.journals()
        data['bindings'] = list(store.db.execute('SELECT target,snapshot,verified FROM bindings ORDER BY target'))
        data['runtime_key_fingerprint'] = hashlib.sha256(self.runtime.key).hexdigest()
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _admit(self):
        store = self.runtime.store
        if store.journals(pending=True):
            raise CloudImportError('cloud_pending_operation')
        try:
            store.require_tx_ready()
        except RuntimeError:
            raise CloudImportError('cloud_not_admitted') from None
        if not hmac.compare_digest(store.get('mesh_fingerprint', ''), hashlib.sha256(self.runtime.key).hexdigest()):
            raise CloudImportError('cloud_identity_changed')

    def _validate(self, data):
        # Exercise every production Store import guard against an in-memory copy.
        # No writes (even temporary ones) are made to the live durable database.
        from .storage import Store
        if self.runtime.store.db.in_transaction:
            raise CloudImportError('cloud_busy')
        clone = Store.__new__(Store)
        clone.lock_fd = None
        clone.db = sqlite3.connect(':memory:', isolation_level=None)
        try:
            self.runtime.store.db.backup(clone.db)
            clone.import_inventory(data)
        finally:
            clone.close()

    async def _fetch(self, email, password, location_id):
        async with _CloudReader(self.session_factory) as reader:
            await reader.login(email, password)
            locations = await reader.locations(self.runtime.key)
            if location_id is None:
                selected = locations[0] if len(locations) == 1 else None
            else:
                selected = next((row for row in locations if row['id'] == location_id), None)
                if selected is None:
                    raise CloudImportError('cloud_location_unavailable')
            remote = await reader.inventory(selected['id']) if selected and selected['matching_mesh'] else None
            return locations, selected, remote

    async def preview(self, email, password, location_id=None):
        if (not isinstance(email, str) or not 1 <= len(email) <= 320
                or not isinstance(password, str) or not 1 <= len(password) <= 4096):
            raise CloudImportError('cloud_invalid_credentials')
        if location_id is not None:
            try:
                location_id = pid(location_id)
            except ValueError:
                raise CloudImportError('cloud_invalid_request') from None
        if self._lock.locked():
            raise CloudImportError('cloud_busy')
        async with self._lock:
            self._plan = None
            self._epoch += 1
            epoch = self._epoch
            self._admit()
            revision = self._revision()
            try:
                locations, selected, remote = await asyncio.wait_for(self._fetch(email, password, location_id), 180)
            except asyncio.TimeoutError:
                raise CloudImportError('cloud_timeout') from None
            finally:
                email = password = None
            if epoch != self._epoch:
                raise CloudImportError('cloud_cancelled')
            self._admit()
            if self._revision() != revision:
                raise CloudImportError('cloud_preview_stale')
            response = {'status': 'select_location', 'locations': locations, 'selected_location': selected,
                'preview_id': None, 'revision': revision, 'expires_in': None, 'can_apply': False,
                'changes': {'devices_added': [], 'groups_added': [], 'members_added': []},
                'message': 'Select a location and enter your credentials again to preview its inventory.',
                'physical_memberships_changed': False, 'new_devices_exposed': False}
            if not locations:
                response.update(status='blocked', message='No cloud locations were found for this account.')
            elif selected is not None and not selected['matching_mesh']:
                response.update(status='blocked', message='This location uses a different mesh key. Import cannot replace the current mesh; a separate takeover is required.')
            elif remote is not None:
                try:
                    data, changes = _review_merge(self.runtime.store.inventory(), remote,
                                                  self.runtime.store.get('known_targets', []))
                    self._validate(data)
                except ValueError:
                    raise CloudImportError('cloud_inventory_conflict') from None
                identifier = uuid.uuid4().hex
                self._plan = {'id': identifier, 'revision': revision, 'expires': self.clock() + 300,
                              'data': copy.deepcopy(data), 'cloud_devices':copy.deepcopy(remote['devices']),
                              'location_id':selected['id']}
                response.update(status='ready', preview_id=identifier, expires_in=300, can_apply=True,
                                changes=changes, message='Review the additions before importing. Existing names, sharing choices and memberships are preserved.')
            return response

    async def apply(self, request):
        if (not isinstance(request, dict) or set(request) != {'preview_id', 'revision'}
                or not isinstance(request['preview_id'], str) or not isinstance(request['revision'], str)):
            raise CloudImportError('cloud_invalid_request')
        if self._lock.locked():
            raise CloudImportError('cloud_busy')
        async with self._lock:
            plan = self._plan
            if (plan is None or not hmac.compare_digest(request['preview_id'], plan['id'])
                    or not hmac.compare_digest(request['revision'], plan['revision'])):
                raise CloudImportError('cloud_preview_unavailable')
            # A submitted plan is single-use even if admission later fails.
            self._plan = None
            if self.clock() >= plan['expires']:
                raise CloudImportError('cloud_preview_expired')
            async with self.runtime.foreground_request(timeout=5):
                if self.runtime.operation.locked():
                    raise CloudImportError('cloud_busy')
                async with self.runtime.operation:
                    self._admit()
                    if self.clock() >= plan['expires']:
                        raise CloudImportError('cloud_preview_expired')
                    if self._revision() != plan['revision']:
                        raise CloudImportError('cloud_preview_stale')
                    try:
                        self._validate(plan['data'])
                        from .cloud_provenance import retain_cloud_devices
                        self.runtime.store.import_inventory(plan['data'])
                        retain_cloud_devices(self.runtime.store,plan['cloud_devices'],plan['location_id'])
                    except ValueError:
                        raise CloudImportError('cloud_inventory_conflict') from None
                    reload_inventory(self.runtime)
                    from .management import revision
                    return {'saved': True, 'revision': revision(self.runtime.store),
                            'devices': len(plan['data']['devices']), 'groups': len(plan['data']['groups']),
                            'physical_memberships_changed': False, 'new_devices_exposed': False}

    def cancel(self):
        self._epoch += 1
        self._plan = None
        return {'cancelled': True, 'saved': False}
