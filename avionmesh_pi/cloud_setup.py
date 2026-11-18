"""First-install HALO/Avi-on import, derived from the ESP avion_import workflow.

Only the reviewed key and inventory are persisted. Account credentials and tokens
are short-lived; there is no background cloud login or automatic re-pairing.
"""
import asyncio
import copy
import time
import uuid

from .cloud import _CloudReader, name, pid, records
from .protocol import derive_mcp_key


class ImportSetupError(ValueError):
    MESSAGES = {
        'invalid_request': 'Check the account details and selected location.',
        'cloud_unavailable': 'Could not load the HALO account. Check your login and connection, then try again.',
        'preview_expired': 'This import preview expired. Sign in again to review a fresh copy.',
        'import_busy': 'An import request is already running.',
        'location_unavailable': 'Select a location from this account.',
        'invalid_inventory': 'This location contains unsupported or incomplete records. Nothing was imported.',
    }
    def __init__(self, code):
        self.code = code
        super().__init__(self.MESSAGES[code])


class CloudSetup:
    """One bounded, cancellable, private preview per empty installation."""
    TTL = 600

    def __init__(self, session_factory=None, *, clock=time.monotonic):
        self.session_factory, self.clock = session_factory, clock
        self.lock = asyncio.Lock()
        self.epoch = 0
        self.expiry_handle = None
        self.cancel()

    def cancel(self):
        if self.expiry_handle is not None:
            self.expiry_handle.cancel()
            self.expiry_handle = None
        self.epoch += 1
        self.token = self.session_id = self.plan = None
        self.locations = []
        self.expires = 0

    def _check(self, session_id):
        if not isinstance(session_id, str) or session_id != self.session_id or self.clock() >= self.expires:
            self.cancel()
            raise ImportSetupError('preview_expired')

    async def _locations(self, email, password):
        async with _CloudReader(self.session_factory) as reader:
            await reader.login(email, password)
            rows = records(await reader.request('user/locations'), 'locations', 32)
            found, seen = [], set()
            for row in rows:
                identifier = pid(row.get('pid'))
                if identifier in seen:
                    raise ValueError('Duplicate location')
                seen.add(identifier)
                found.append({'id':identifier, 'name':name(row.get('name', 'Location ' + identifier))})
            return found, reader.token

    async def _inventory(self, selected):
        async with _CloudReader(self.session_factory) as reader:
            reader.token = self.token
            document = await reader.request('locations/' + selected['id'])
            location = document.get('location')
            if not isinstance(location, dict):
                raise ValueError('Invalid location')
            phrase = location.get('passphrase')
            if (not isinstance(phrase, str) or not phrase or phrase != phrase.strip()
                    or len(phrase.encode()) > 252 or any(ord(c) < 32 for c in phrase)):
                raise ValueError('Invalid mesh passphrase')
            derive_mcp_key(phrase)
            inventory = await reader.inventory(selected['id'])
            from .bridge import load_devices
            load_devices(inventory['devices'])
            return phrase, inventory, reader.skipped_unaddressed

    async def login(self, email, password):
        if (not isinstance(email, str) or not 1 <= len(email) <= 320 or not email.strip()
                or not isinstance(password, str) or not 1 <= len(password) <= 4096):
            raise ImportSetupError('invalid_request')
        if self.lock.locked():
            raise ImportSetupError('import_busy')
        async with self.lock:
            self.cancel()
            epoch = self.epoch
            try:
                found, token = await asyncio.wait_for(self._locations(email.strip(), password), 60)
                if self.epoch != epoch:
                    raise ImportSetupError('preview_expired')
                self.token, self.locations = token, found
                self.session_id, self.expires = uuid.uuid4().hex, self.clock() + self.TTL
                self.expiry_handle = asyncio.get_running_loop().call_later(self.TTL, self.cancel)
                return {'session': self.session_id, 'locations': copy.deepcopy(found), 'expires_in': self.TTL}
            except ImportSetupError:
                raise
            except asyncio.CancelledError:
                self.cancel()
                raise
            except Exception:
                self.cancel()
                raise ImportSetupError('cloud_unavailable') from None

    async def preview(self, session_id, location_id):
        if self.lock.locked():
            raise ImportSetupError('import_busy')
        async with self.lock:
            self._check(session_id)
            selected = next((x for x in self.locations if x['id'] == location_id), None)
            self.plan = None
            if selected is None:
                raise ImportSetupError('location_unavailable')
            epoch = self.epoch
            try:
                phrase, inventory, skipped = await asyncio.wait_for(self._inventory(selected), 180)
                self._check(session_id)
                if epoch != self.epoch:
                    raise ImportSetupError('preview_expired')
                self.plan = {'preview': uuid.uuid4().hex, 'location': dict(selected),
                             'passphrase': phrase, 'inventory': inventory}
                return {'preview': self.plan['preview'], 'location': dict(selected),
                        'inventory': copy.deepcopy(inventory), 'local_control': True,
                        'physical_memberships_changed': False, 'scenes_imported': False,
                        'skipped_unaddressed': skipped}
            except ImportSetupError:
                raise
            except asyncio.CancelledError:
                self.cancel()
                raise
            except Exception:
                self.plan = None
                raise ImportSetupError('invalid_inventory') from None

    async def apply(self, session_id, preview_id, save):
        if self.lock.locked():
            raise ImportSetupError('import_busy')
        async with self.lock:
            self._check(session_id)
            if self.plan is None or not isinstance(preview_id, str) or preview_id != self.plan['preview']:
                raise ImportSetupError('preview_expired')
            # Consume before persistence; a failed/uncertain save is never replayed.
            plan = self.plan
            self.cancel()
            return save(plan)
