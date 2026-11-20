"""CSRMesh bearer discovery, separate from owner-initiated claim or reset."""
import asyncio
import re
import time
from .bluez import BlueZTransport, SERVICE
from .bridge import CONTROLLERS, LIGHTS


def address_text(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}',value):
        raise ValueError('Expected Bluetooth address')
    value=value.upper()
    if value=='00:00:00:00:00:00':raise ValueError('Invalid Bluetooth address')
    return value


# A paired light is preferred as the relay unless its signal is weaker than this.
PREFERRED_RELAY_MIN_RSSI = -85


def relay_devices(store):
    """Match a current device to a Bluetooth address only through a verified claim."""
    owned = {d['device_id']: d for d in store.inventory()['devices'] if d['product_id'] in LIGHTS or d['product_id'] in CONTROLLERS}
    retired = set(store.get('retired_claims', []))
    addresses = {}
    targets = {}
    for journal in store.journals():
        detail = journal.get('detail')
        if not isinstance(detail, dict):
            continue
        identity = detail.get('uuid')
        if (journal.get('kind') != 'claim' or journal.get('status') != 'verified'
                or journal.get('id') in retired or journal.get('target') not in owned
                or detail.get('product_id') != owned[journal['target']]['product_id']
                or not isinstance(identity, str) or len(identity) != 32):
            continue
        try:
            raw = bytes.fromhex(identity)
        except ValueError:
            continue
        if len(raw) != 16:
            continue
        try:
            address = address_text(':'.join(f'{b:02X}' for b in raw[:6][::-1]))
        except ValueError:
            continue
        target = journal['target']
        addresses.setdefault(address, set()).add(target)
        targets.setdefault(target, set()).add(address)
    devices = {}
    for address, matches in addresses.items():
        if len(matches) == 1:
            target, = matches
            if len(targets[target]) == 1:
                devices[address] = {'device_id': target, 'name': owned[target]['name'],
                                    'kind': 'controller' if owned[target]['product_id'] in CONTROLLERS else 'light'}
    return devices


def relay_lights(store):
    return {a: d for a, d in relay_devices(store).items() if d['kind'] == 'light'}


def paired_relay_addresses(store):
    return set(relay_lights(store))


class RelayScanBusy(RuntimeError):
    pass


EXTENDED_SCAN_SECONDS = 60


def relay_advertisement(device, adv):
    if SERVICE not in (str(s).lower() for s in (adv.service_uuids or [])):
        return None
    try:
        address = address_text(device.address)
    except ValueError:
        return None
    details = getattr(device, 'details', None)
    props = details.get('props', {}) if isinstance(details, dict) else {}
    # These high bits identify private addresses only when BlueZ reports "random".
    # A public address with identical leading bits and a static random address
    # are both eligible, including on a fresh mesh with no known lights.
    if props.get('AddressType') == 'random' and int(address[:2], 16) >> 6 in (0, 1):
        return None
    if type(adv.rssi) is not int or not -127 <= adv.rssi <= 20:
        return None
    return address, adv.rssi


class AutoBlueZTransport(BlueZTransport):
    """Choose a nearby CSRMesh bearer; advertisement is not ownership proof.

    Selection is transient and repeated after disconnection, as on the ESP
    bridge. Only GATT connection and notifications happen here. MASP discovery,
    association, and all mesh writes remain explicit runtime operations.
    """
    def __init__(self, adapter='hci0', **kwargs):
        super().__init__('00:00:00:00:00:00', adapter, **kwargs)
        self._connecting = False
        self._scan_lock = asyncio.Lock()
        # Optional callable returning addresses of this bridge's own paired lights.
        self.preferred_relays = None
        self.relay_preferred = None
        self.scan_policy = None
        self.observations = {}
        self.latest_candidates = set()
        self.extended_task = None
        self.extended_phase = 'idle'
        self.extended_deadline = 0
        self.scanned_at = None
        self.scan_number = 0
        self.attempted_preference = None

    @property
    def selected_address(self):
        return self.address if self.ready else None

    def _scanner(self):
        if self.scanner is None:
            from bleak import BleakScanner
            self.scanner = BleakScanner
        return self.scanner

    def _commit_scan(self, samples):
        self.scan_number += 1
        self.scanned_at = int(time.time())
        self.latest_candidates = set(samples)
        history = dict(samples)
        for address, sample in self.observations.items():
            if len(history) >= 128:
                break
            history.setdefault(address, sample)
        self.observations = history

    async def scan(self, *, manual=False):
        if manual and (self._connecting or self._scan_lock.locked() or self.extended_phase == 'running'):
            raise RelayScanBusy('Relay discovery is already running')
        async with self._scan_lock:
            rows = await asyncio.wait_for(self._scanner().discover(
                timeout=5, return_adv=True, bluez={'adapter': self.adapter}), 8)
            candidates = {}
            for device, adv in rows.values():
                sample = relay_advertisement(device, adv)
                if sample:
                    address, rssi = sample
                    candidates[address] = max(rssi, candidates.get(address, rssi))
                    if len(candidates) > 128:
                        raise RuntimeError('Bluetooth candidate scan truncated; narrow scope')
            stamp = int(time.time())
            self._commit_scan({a: (rssi, stamp) for a, rssi in candidates.items()})
            if manual:
                self.extended_phase = 'idle'
            return candidates

    def start_extended_scan(self):
        if not self.ready or self._connecting or self._scan_lock.locked() or self.extended_phase == 'running':
            raise RelayScanBusy('Extended scan requires a connected, idle scanner.')
        self.extended_phase = 'running'
        self.extended_deadline = time.monotonic() + EXTENDED_SCAN_SECONDS
        self.extended_task = asyncio.create_task(self._extended_scan())

    def extended_status(self):
        return {'phase': self.extended_phase,
                'remaining_seconds': max(0, int(self.extended_deadline - time.monotonic() + .999))
                if self.extended_phase == 'running' else 0}

    async def _extended_scan(self):
        samples = {}
        overflow = asyncio.Event()
        def hear(device, adv):
            sample = relay_advertisement(device, adv)
            if sample:
                address, rssi = sample
                if address not in samples and len(samples) >= 128:
                    overflow.set()
                    return
                samples[address] = (rssi, int(time.time()))

        async def listen():
            async with self._scanner()(detection_callback=hear, bluez={'adapter': self.adapter}):
                try:
                    await asyncio.wait_for(overflow.wait(), EXTENDED_SCAN_SECONDS)
                except asyncio.TimeoutError:
                    return
                raise RuntimeError('Too many relay candidates')

        try:
            async with self._scan_lock:
                await asyncio.wait_for(listen(), EXTENDED_SCAN_SECONDS + 8)
                if not self.ready:
                    self.extended_phase = 'interrupted'
                    return
                self._commit_scan(samples)
                self.extended_phase = 'complete'
        except asyncio.CancelledError:
            self.extended_phase = 'interrupted'
            raise
        except Exception:
            self.extended_phase = 'failed'

    async def stop_extended_scan(self):
        task = self.extended_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.extended_phase = 'interrupted'

    async def connect(self):
        if self._connecting:
            raise RuntimeError('Bluetooth connection attempt already active')
        if self.client is not None and self.client.is_connected:
            raise RuntimeError('Previous Bluetooth connection has not closed')
        self._connecting = True
        self.ready = False
        try:
            await self.stop_extended_scan()
            candidates = await self.scan()
            if self.client_factory is None:
                from bleak import BleakClient
                self.client_factory = BleakClient
            if not candidates:
                raise RuntimeError('No CSRMesh Bluetooth bearer found')
            # Prefer this bridge's own paired lights with a usable signal: any
            # nearby device advertising the mesh service (an unpaired light, even
            # one about to be paired, or another installation's light) could
            # otherwise become the relay. Then try a bounded RSSI-ranked set, so
            # an incompatible or vanished advertiser cannot hide a usable bearer.
            # Stable address ordering breaks ties without inferring identity.
            try:
                if self.scan_policy:
                    preferred, choice, controllers = self.scan_policy()
                else:
                    preferred = set(self.preferred_relays()) if self.preferred_relays else set()
                    choice, controllers = None, set()
            except Exception:
                preferred, choice, controllers = set(), None, set()  # A preference must never block connecting.
            self.attempted_preference = choice
            def rank(address):
                usable = address in preferred and candidates[address] >= PREFERRED_RELAY_MIN_RSSI
                return (address != choice, not usable, -candidates[address], address)
            ranked = sorted(candidates.keys() - controllers, key=rank)[:4]
            if not ranked:
                raise RuntimeError('No eligible CSRMesh Bluetooth bearer found')
            for index, address in enumerate(ranked):
                self.address = address
                self.relay_preferred = address in preferred
                try:
                    # Reuse characteristic, subscription, callback and disconnect
                    # checks, including a fresh exact-address device lookup.
                    await super().connect()
                    return
                except Exception:
                    # Cancellation is a BaseException and must never try another
                    # bearer. Failed disconnects also forbid any next connection.
                    if self.client is not None and self.client.is_connected:
                        raise
                    if index == len(ranked) - 1:
                        raise
        finally:
            self._connecting = False


class AutomaticBootstrap:
    """Read-only bootstrap status; a live bearer does not identify the claimant."""
    enabled = True
    automatic = True

    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def selected_address(self):
        return self.runtime.transport.selected_address

    @property
    def needs_first_claim(self):
        return not self.runtime.store.inventory()['devices']

    def status(self):
        return {'enabled': True, 'automatic': True,
                'selected_address': self.selected_address,
                'needs_first_claim': self.needs_first_claim}


class DeferredBlueZTransport(BlueZTransport):
    def __init__(self, selected_address=None, adapter='hci0', **kwargs):
        super().__init__(address_text(selected_address) if selected_address else '00:00:00:00:00:00',adapter,**kwargs)
        self.selection=asyncio.Event()
        if selected_address:self.selection.set()

    def select(self,address):
        address=address_text(address)
        if self.selection.is_set() and address!=self.address:
            raise ValueError('First-device bearer cannot be replaced')
        self.address=address
        self.selection.set()

    async def connect(self):
        await self.selection.wait()
        await super().connect()


class BootstrapSelection:
    enabled=True
    def __init__(self,runtime,*,scanner=None):
        self.runtime=runtime
        self.scanner=scanner
        self.observations={}
        self.scanned_at=None

    @property
    def selected_address(self):
        value=self.runtime.store.get('bootstrap_relay_address')
        return address_text(value) if value else None

    @property
    def needs_first_claim(self):
        return not self.runtime.store.inventory()['devices']

    def status(self):
        return {'enabled':True,'selected_address':self.selected_address,
                'needs_first_claim':self.needs_first_claim}

    def _empty(self):
        r=self.runtime
        r.store.require_tx_ready()
        data=r.store.inventory()
        if data['devices'] or data['groups'] or r.store.journals() or r.store.get('sequence')!=0:
            raise ValueError('Selection requires empty unused fresh-mesh state')
        receipt=r.store.get('tx_admission',{})
        if receipt.get('document',{}).get('mode')!='fresh_mesh':
            raise ValueError('Selection requires fresh-mesh admission')

    async def scan(self):
        r=self.runtime
        if self.selected_address or r.operation.locked():raise RuntimeError('First-device selection unavailable')
        self._empty()
        async with r.operation:
            self.observations={};self.scanned_at=None
            scanner=self.scanner
            if scanner is None:
                from bleak import BleakScanner
                scanner=BleakScanner
            rows=await asyncio.wait_for(scanner.discover(timeout=5,return_adv=True,
                bluez={'adapter':r.transport.adapter}),8)
            candidates={}
            for device,adv in rows.values():
                if SERVICE not in [str(s).lower() for s in (adv.service_uuids or [])]:continue
                address=address_text(device.address)
                candidates[address]={'address':address,'rssi':adv.rssi}
                if len(candidates)>128:raise RuntimeError('Bluetooth candidate scan truncated; narrow scope')
            self.observations=candidates
            self.scanned_at=r.clock()
            return {'candidates':list(candidates.values()),'factory_reset_verified':False,
                    'instructions':'Select only the physical factory-reset first RL4. Advertisement alone is not unpaired proof.'}

    async def select(self,address,owner_reset):
        r=self.runtime
        address=address_text(address)
        if owner_reset is not True:raise ValueError('Declare the exact selected device factory-reset first')
        if r.operation.locked():raise RuntimeError('Another operation is active')
        if self.selected_address:
            if self.selected_address!=address:raise ValueError('Retained first-device selection cannot be replaced')
            return dict(self.status(),selected=True,association_started=False)
        self._empty()
        if self.scanned_at is None or not 0<=r.clock()-self.scanned_at<=30000 or address not in self.observations:
            raise ValueError('Fresh exact Bluetooth advertisement required')
        async with r.operation:
            with r.store.transaction():
                self._empty()
                r.store.set('bootstrap_relay_address',address)
                r.store.set('bootstrap_owner_reset_declared',True)
            r.transport.select(address)
        return dict(self.status(),selected=True,association_started=False)
