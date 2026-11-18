"""Durable, explicitly stale HA mapping evidence for sleeping model-126 keypads.

This cache never supplies editor/readback evidence and never sends radio traffic.
Only targets owned by verified HA receipts can recover numbered activations.
"""
import copy
import hashlib
import json
import time

from .keypad_ownership import owned_targets
from .protocol import decode_controller

# Keep the metadata key so old revoked entries remain visibly revoked. A fresh
# explicit read is required to replace an eligible legacy context with v2.
CACHE_KEY = 'battery_mapping_cache_v1'
CONTEXT_SCHEMA = 'battery-mapping-context-v2'
PENDING = {'prepared', 'attempted', 'uncertain'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _table(value):
    # Import lazily: KeypadService owns creation of the complete readback shape.
    from .keypad import snapshot
    try:
        rebuilt = snapshot(bytes.fromhex(value['count']), [bytes.fromhex(row['raw']) for row in value['slots']])
        return rebuilt if rebuilt == value else None
    except (KeyError, TypeError, ValueError):
        return None


class BatteryMappingCache:
    def __init__(self, runtime):
        self.runtime = runtime
        self.sequences = {}
        self.observation_sequences = {}

    def attach_current_router(self):
        router = self.runtime.core.controllers
        if router.battery_cache is not self:
            router.battery_cache = self
            self.reset()

    def _records(self):
        value = self.runtime.store.get(CACHE_KEY, {})
        return {key: row for key, row in value.items() if isinstance(key, str) and isinstance(row, dict)} if isinstance(value, dict) else {}

    def _save(self, records):
        # Only current battery devices can retain cache metadata. Removed devices
        # cannot accumulate entries or later recover an old address's mapping.
        ids = {str(d['device_id']) for d in self.runtime.store.inventory()['devices']
               if d['product_id'] == 126}
        self.runtime.store.set(CACHE_KEY, {key: value for key, value in records.items() if key in ids})

    def revoke(self, target, reason):
        records = self._records()
        if str(target) in records and records[str(target)].get('state') != 'revoked':
            records[str(target)] = {'state': 'revoked', 'reason': reason}
            self._save(records)

    def _context(self, target, table):
        r = self.runtime
        inventory = r.store.inventory()
        devices = [d for d in inventory['devices'] if d['device_id'] == target]
        retired_addresses = r.store.get('retired_addresses', [])
        retired_claims = r.store.get('retired_claims', [])
        if (not isinstance(retired_addresses, list) or any(type(value) is not int for value in retired_addresses)
                or not isinstance(retired_claims, list) or any(not isinstance(value, str) or not value for value in retired_claims)
                or len(devices) != 1 or devices[0]['product_id'] != 126 or target in retired_addresses):
            return None
        device = devices[0]
        all_journals = r.store.journals()
        journals = [j for j in all_journals if j['target'] == target]
        claims = [j for j in journals if j['kind'] == 'claim']
        identities = set()
        for claim in claims:
            identity = claim['detail'].get('uuid')
            if (not isinstance(identity, str) or len(identity) != 32
                    or any(ch not in '0123456789abcdefABCDEF' for ch in identity) or not int(identity, 16)):
                return None
            identities.add(identity.lower())
        # Same-UUID claims at another address are related identity history, even
        # though unrelated controllers and their retirement must not affect us.
        peers = [j for j in all_journals if j['kind'] == 'claim' and j['target'] != target
                 and isinstance(j['detail'].get('uuid'), str) and j['detail']['uuid'].lower() in identities]
        if any(j['status'] == 'verified' and j['id'] not in retired_claims for j in peers):
            return None
        related_claims = {j['id'] for j in claims + peers}
        related_addresses = {target} | {j['target'] for j in peers}
        if any(j['status'] == 'verified' and j['id'] in retired_claims for j in claims):
            return None
        if any(j['status'] in PENDING for j in journals):
            return None
        row = r.store.db.execute('SELECT snapshot FROM bindings WHERE target=?', (target,)).fetchone()
        if not row or json.loads(row[0]) != table:
            return None
        owned = owned_targets(r.store, target)
        slots = [row['slot'] for row in table['slots']
                 if row['flags'] == 8 and row['level'] == 255
                 and owned.get(row['target'], {}).get('slot') == row['slot']
                 and sum(s['target'] == row['target'] for s in table['slots']) == 1]
        if not slots:
            return None
        # Scope stale routing evidence to its real dependencies. Names and room
        # labels do not change assignments; unrelated inventory is irrelevant.
        # Exposure remains significant, and owned_targets still checks exact
        # protected reservations, competing observed sources and stored tables.
        return {'schema': CONTEXT_SCHEMA, 'sha256': _digest({
                    'device': {key: value for key, value in device.items() if key != 'name'},
                    'table': table, 'journals': journals, 'identity_peers': peers,
                    'state_id': r.store.get('state_id'), 'mesh': r.store.get('mesh_fingerprint'),
                    'retired_claims': sorted(related_claims.intersection(retired_claims)),
                    'retired_addresses': sorted(related_addresses.intersection(retired_addresses)),
                    'owned': {str(row['target']): owned[row['target']] for row in table['slots']
                              if row['slot'] in slots}}), 'slots': slots}

    def remember(self, target, table):
        r = self.runtime
        device = r.core.devices.get(target)
        if not device or device.product_id != 126:
            return
        table = _table(table)
        if (table is None or r.core.controllers.bindings.get(target) != table
                or r.store.bindings().get(target) != table):
            raise ValueError('Battery cache requires a complete current verified binding')
        context = self._context(target, table)
        records = self._records()
        if context is None:
            records.pop(str(target), None)
        else:
            records[str(target)] = {'state': 'eligible', 'table': copy.deepcopy(table),
                'context': context, 'recorded_unix_ms': int(time.time() * 1000)}
        self._save(records)

    def mapping(self, target):
        if getattr(self.runtime, 'preview_only', False):
            return None
        record = self._records().get(str(target), {})
        if record.get('state') != 'eligible':
            return None
        if not isinstance(record.get('context'), dict) or record['context'].get('schema') != CONTEXT_SCHEMA:
            self.revoke(target, 'cache_schema_requires_fresh_readback')
            return None
        table = _table(record.get('table'))
        try:
            context = self._context(target, table) if table is not None else None
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            context = None
        if context is None or context != record.get('context'):
            self.revoke(target, 'identity_inventory_or_receipts_changed')
            return None
        return dict(copy.deepcopy(table), mapping_evidence='cached_table',
                    cached_slots=list(context['slots']), assignment_read_back=False)

    def status(self, target):
        mapping = self.mapping(target)
        record = self._records().get(str(target), {})
        return {'status': 'cached' if mapping else record.get('state', 'unavailable'),
                'reason': record.get('reason'), 'assignment_read_back': False,
                'recorded_unix_ms': record.get('recorded_unix_ms') if mapping else None,
                'slots': [{'slot': row['slot'], 'target': row['target'], 'preset': row['level']}
                          for row in mapping['slots'] if row['slot'] in mapping['cached_slots']] if mapping else []}

    def reset(self):
        self.sequences.clear()
        self.observation_sequences.clear()
        for target in list(self._records()):
            if target.isdecimal():
                self.mapping(int(target))  # revoke changed inventory/receipts durably

    def _contradiction(self, target, reason):
        self.revoke(target, reason)
        self.runtime.core.controllers.bindings.pop(target, None)
        self.runtime.store.db.execute('UPDATE bindings SET verified=0 WHERE target=?', (target,))
        self.runtime.emit(self.runtime.discovery())

    def observe(self, packet, received_ms):
        """Contradictory authenticated input revokes before event decoding."""
        device = self.runtime.core.devices.get(packet.crypto_source)
        if (not device or device.product_id != 126 or self.runtime.controller_since is None
                or received_ms < self.runtime.controller_since):
            return
        previous = self.observation_sequences.get(packet.crypto_source)
        if previous is not None and not 0 < ((packet.sequence - previous) & 0xffffff) < 0x800000:
            return
        self.observation_sequences[packet.crypto_source] = packet.sequence
        mapping = self.mapping(packet.crypto_source)
        if not mapping or packet.opcode != 115:
            return
        p = packet.payload
        mismatch = (len(p) == 5 and p[:3] == b'\x04\x30\0'
                    and p.hex() != mapping['count'])
        if len(p) == 8 and p[:3] == b'\x01\x30\0' and 1 <= p[3] <= 4:
            mismatch = p.hex() != mapping['slots'][p[3] - 1]['raw']
        event = decode_controller(packet, 126)
        if event and ('brightness' in event or 'delta' in event):
            target = event.get('target') or packet.mcp_address
            if not any(row['target'] == target for row in mapping['slots']):
                self._contradiction(packet.crypto_source, 'observed_target_not_in_cached_table')
                return
        if mismatch:
            self._contradiction(packet.crypto_source, 'contradictory_assignment_report')

    def for_event(self, packet, event):
        mapping = self.mapping(packet.crypto_source)
        if not mapping or packet.mcp_address != 0 or not ('brightness' in event or 'delta' in event):
            return None
        admitted = self.observation_sequences.get(packet.crypto_source)
        if admitted is not None and admitted != packet.sequence:
            return None
        previous = self.sequences.get(packet.crypto_source)
        if previous is not None and not 0 < ((packet.sequence - previous) & 0xffffff) < 0x800000:
            return None
        self.sequences[packet.crypto_source] = packet.sequence
        matches = [row for row in mapping['slots'] if row['target'] == event.get('target')]
        if not matches:
            self._contradiction(packet.crypto_source, 'observed_target_not_in_cached_table')
            return None
        # Known native slots may still operate physically, but only receipt-owned
        # HA slots are eligible for restored numbered HA events.
        return mapping
