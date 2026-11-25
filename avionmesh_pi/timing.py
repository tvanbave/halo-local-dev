"""Bounded command-stage timings; observations are correlated, never delivery proof.

No payload bytes, credentials or persistent state are retained. MQTT completion
means the client publish call returned, not that Home Assistant handled a state.
"""
from collections import OrderedDict
from copy import deepcopy


class CommandTimings:
    def __init__(self, clock, prefix, capacity=32, lifetime_ms=30000):
        self.clock, self.prefix = clock, prefix
        self.capacity, self.lifetime_ms = capacity, lifetime_ms
        self.records = OrderedDict()
        self.serial, self.overwritten = 0, 0

    def _expire(self):
        now = self.clock()
        for r in self.records.values():
            if r['closed'] is None and now - r['received'] >= self.lifetime_ms:
                r['closed'] = 'expired'

    def start(self, target, members, expected, *, source, received_ms=None, shared=False):
        self._expire()
        self.serial += 1
        now = self.clock()
        received = received_ms if type(received_ms) is int and 0 <= received_ms <= now else now
        if len(self.records) == self.capacity:
            self.records.popitem(last=False)
            self.overwritten += 1
        self.records[self.serial] = {'id': self.serial, 'target': target,
            'members': frozenset(members), 'expected': dict(expected), 'reports': {},
            'source': source if source in ('ui', 'mqtt', 'native', 'bridge') else 'bridge',
            'received': received, 'dispatch': None, 'sent': None, 'first_report': None,
            'all_reports': None, 'first_published': None, 'all_published': None,
            'publish_queue_ms': None, 'publish_call_ms': None,
            'shared': bool(shared), 'closed': None}
        return self.serial

    def dispatch(self, identifier):
        self._expire()
        r = self.records.get(identifier)
        if r is None or r['closed'] is not None:
            return
        for older in self.records.values():
            if (older['id'] != identifier and older['closed'] is None
                    and older['dispatch'] is not None
                    and older['members'] & r['members']
                    and older['expected'].keys() & r['expected'].keys()):
                older['closed'] = 'superseded'
        r['dispatch'] = r['received'] if r['source'] == 'native' else self.clock()

    def sent(self, identifier):
        r = self.records.get(identifier)
        if r is not None and r['closed'] is None:
            r['sent'] = self.clock()

    def failed(self, identifier):
        r = self.records.get(identifier)
        if r is not None and r['closed'] is None:
            r['closed'] = 'dispatch_uncertain' if r['dispatch'] is not None else 'not_dispatched'

    def boundary(self):
        for r in self.records.values():
            if r['closed'] is None:
                r['closed'] = 'session_changed'

    @staticmethod
    def _matched(r):
        return sum(all(field in r['reports'].get(member, {}) and (value is None or r['reports'][member][field] == value)
                       for field, value in r['expected'].items()) for member in r['members'])

    def observe(self, target, update, received_ms):
        self._expire()
        for r in self.records.values():
            if (r['closed'] is not None or r['dispatch'] is None or target not in r['members']
                    or received_ms < r['dispatch']):
                continue
            fields = {k: v for k, v in update.items() if k in r['expected']}
            if not fields:
                continue
            r['reports'].setdefault(target, {}).update(fields)
            if r['first_report'] is None:
                r['first_report'] = received_ms
            if r['all_reports'] is None and self._matched(r) == len(r['members']):
                r['all_reports'] = received_ms

    def publication_tokens(self, message):
        """Freeze correlation when a state is queued, before it can be replaced."""
        self._expire()
        payload, topic = message.get('payload'), message.get('topic')
        if not isinstance(payload, dict) or not isinstance(topic, str):
            return ()
        tokens = []
        for r in self.records.values():
            if (r['closed'] is not None or r['first_report'] is None
                    or topic != f"{self.prefix}/light/{r['target']}/state"):
                continue
            matches = all((('brightness' in payload or payload.get('state') in ('ON', 'OFF')) if value is None
                           else (payload.get('state') == ('ON' if value else 'OFF')
                           if field == 'brightness' and 'brightness' not in payload
                           else payload.get(field) == value))
                          for field, value in r['expected'].items())
            if matches:
                tokens.append((r['id'], r['all_reports'] is not None))
        return tuple(tokens)

    def published(self, tokens, queued_ms, begun_ms, ended_ms):
        self._expire()
        for identifier, all_reported in tokens:
            r = self.records.get(identifier)
            if r is None or r['closed'] is not None:
                continue
            if r['first_published'] is None:
                r['first_published'] = ended_ms
                r['publish_queue_ms'] = max(0, begun_ms - queued_ms)
                r['publish_call_ms'] = max(0, ended_ms - begun_ms)
            if all_reported and r['all_published'] is None:
                r['all_published'] = ended_ms

    def latest_levels(self, window_ms=15000):
        """The newest dispatched brightness per target, for a brief display hint.

        A requested level is never an observation: the page may show it only until each
        member reports, and only within this window. Commands that never started or whose
        send failed are excluded, as is everything before a session boundary.
        """
        self._expire()
        now, latest = self.clock(), {}
        for r in self.records.values():
            level = r['expected'].get('brightness')
            if (type(level) is not int or r['dispatch'] is None
                    or r['closed'] not in (None, 'superseded')):
                continue
            at = r['sent'] if r['sent'] is not None else r['dispatch']
            age = now - at
            if not 0 <= age <= window_ms:
                continue
            prior = latest.get(r['target'])
            if prior is None or at >= prior['sent_ms']:
                latest[r['target']] = {'brightness': level, 'sent_ms': at, 'age_ms': age,
                                       'source': r['source'], 'members': sorted(r['members'])}
        return {str(target): value for target, value in latest.items()}

    def summary(self):
        self._expire()
        recent = []
        for r in reversed(list(self.records.values())[-8:]):
            elapsed = lambda field: None if r[field] is None else max(0, r[field] - r['received'])
            status = r['closed'] or ('published' if r['all_published'] is not None else
                'reported' if r['all_reports'] is not None else
                'waiting_reports' if r['sent'] is not None else 'dispatching' if r['dispatch'] is not None else 'queued')
            recent.append({'id': r['id'], 'target': r['target'], 'source': r['source'],
                'status': status, 'fields': sorted(r['expected']), 'members': len(r['members']),
                'members_reported': len(r['reports']), 'members_matched': self._matched(r),
                'shared': r['shared'], 'age_ms': max(0, self.clock() - r['received']),
                'queue_ms': elapsed('dispatch') if r['source'] != 'native' else None,
                'dispatch_ms': None if r['source'] == 'native' or r['sent'] is None or r['dispatch'] is None else max(0, r['sent'] - r['dispatch']),
                'first_report_ms': elapsed('first_report'), 'all_reports_ms': elapsed('all_reports'),
                'mqtt_sent_ms': elapsed('first_published'), 'mqtt_all_reports_sent_ms': elapsed('all_published'),
                'mqtt_queue_ms': r['publish_queue_ms'], 'mqtt_publish_call_ms': r['publish_call_ms']})
        return deepcopy({'capacity': self.capacity, 'overwritten': self.overwritten,
            'correlation': 'temporal_only_no_wire_command_identifier',
            'physical_delivery_proven': False, 'ha_receipt_measured': False,
            'mqtt_completion': 'client_publish_returned', 'recent': recent})
