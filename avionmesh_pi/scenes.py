"""Offline scene plans and a durable uncertainty ledger; no radio integration.

This module is research tooling. The live scene workflow is ``scene_engine``,
which keeps its own receipts and does not use these plans. Plans here contain
intent, never encrypted frames, allocated sequences, or permission to transmit.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid

from .protocol import integer

MAX_RESEARCH_REFERENCE = 20  # Investigation bound, not discovered hardware capacity.
MAX_MEMBERS = 32
_MAX_RECEIPTS = 64
_KEY = 'scene_research_operations_v1'


def _target(target, product_id):
    integer(target, 32896, 65407, 'scene light')
    if type(product_id) is not int or product_id != 134:
        raise ValueError('Only the A19 candidate scene shape is available for research')


@dataclass(frozen=True)
class SceneMember:
    target: int
    product_id: int = 134

    def __post_init__(self):
        _target(self.target, self.product_id)


@dataclass(frozen=True)
class ScenePreset:
    target: int
    brightness: int
    kelvin: int
    product_id: int = 134

    def __post_init__(self):
        _target(self.target, self.product_id)
        integer(self.brightness, 0, 255, 'scene brightness')
        integer(self.kelvin, 2700, 5000, 'research colour temperature')


@dataclass(frozen=True)
class SceneStep:
    target: int
    property: str
    value: int | None

    def __post_init__(self):
        _target(self.target, 134)
        if self.property == 'colour':
            integer(self.value, 2700, 5000, 'research colour temperature')
        elif self.property == 'brightness':
            integer(self.value, 0, 255, 'scene brightness')
        elif self.property != 'remove_reference' or self.value is not None:
            raise ValueError('Invalid scene step')


@dataclass(frozen=True)
class ScenePlan:
    operation: str
    scene_reference: int
    name: str
    members: tuple

    def __post_init__(self):
        if self.operation not in ('create', 'remove'):
            raise ValueError('Invalid scene operation')
        integer(self.scene_reference, 1, MAX_RESEARCH_REFERENCE, 'research scene reference')
        if (not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 128
                or any(ord(c) < 32 or ord(c) == 127 for c in self.name)):
            raise ValueError('Expected a scene name of 1 to 128 characters without controls')
        member_type = ScenePreset if self.operation == 'create' else SceneMember
        if (type(self.members) is not tuple or not 1 <= len(self.members) <= MAX_MEMBERS
                or any(type(m) is not member_type for m in self.members)
                or len({m.target for m in self.members}) != len(self.members)):
            raise ValueError('Expected bounded, unique, explicitly listed scene lights')

    @property
    def steps(self):
        if self.operation == 'remove':
            return tuple(SceneStep(m.target, 'remove_reference', None) for m in self.members)
        return tuple(step for m in self.members for step in (
            SceneStep(m.target, 'colour', m.kelvin),
            SceneStep(m.target, 'brightness', m.brightness)))

    def candidate_command(self, step_index, token):
        """Build plaintext research bytes using an explicit step and caller token.

        This does not consult progress or permit execution; no live executor is
        implemented. Tokens must be correlated and scoped by a future service.
        """
        from .scene_protocol import (candidate_colour_insert, candidate_brightness_insert,
                                     candidate_delete)
        integer(step_index, 0, len(self.steps) - 1, 'scene step')
        step = self.steps[step_index]
        if step.property == 'colour':
            return candidate_colour_insert(step.target, self.scene_reference, step.value, token)
        if step.property == 'brightness':
            return candidate_brightness_insert(step.target, self.scene_reference, step.value, token)
        return candidate_delete(step.target, self.scene_reference, token)

    def as_dict(self):
        return {'operation': self.operation, 'scene_reference': self.scene_reference,
                'name': self.name, 'members': [dict(vars(m)) for m in self.members]}

    @property
    def fingerprint(self):
        raw = json.dumps(self.as_dict(), sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(raw).hexdigest()


def _members(members):
    if type(members) not in (list, tuple) or not 1 <= len(members) <= MAX_MEMBERS:
        raise ValueError('Expected an explicit bounded member list')
    return tuple(members)


def plan_create(scene_reference, name, presets):
    """Plan colour then brightness for each explicitly selected A19, without I/O."""
    return ScenePlan('create', scene_reference, name, _members(presets))


def plan_remove(scene_reference, name, members):
    """Plan app-shaped removal; its extent across properties is still unknown."""
    return ScenePlan('remove', scene_reference, name, _members(members))


def _restore_plan(value):
    if (type(value) is not dict or set(value) != {'operation', 'scene_reference', 'name', 'members'}
            or type(value['members']) is not list or not 1 <= len(value['members']) <= MAX_MEMBERS):
        raise ValueError('Invalid stored scene plan')
    kind = ScenePreset if value['operation'] == 'create' else SceneMember
    keys = {'target', 'product_id', 'brightness', 'kelvin'} if kind is ScenePreset else {'target', 'product_id'}
    if any(type(m) is not dict or set(m) != keys for m in value['members']):
        raise ValueError('Invalid stored scene members')
    return ScenePlan(value['operation'], value['scene_reference'], value['name'],
                     tuple(kind(**m) for m in value['members']))


class SceneJournal:
    """Store-backed research ledger, deliberately unable to verify or dispatch.

    Use a synthetic offline Store. Calling mark_attempted records durable intent
    before a future external action; it is not an execution API. An attempted
    step blocks further steps, including after restart. Raw replies can be kept
    for investigation, but no reply, transport result or recall establishes that
    all scene properties were stored or removed. There is no completion shortcut.
    """
    def __init__(self, store):
        self.store = store
        with store.transaction():
            rows = self._load()
            changed = False
            for row in rows:
                for i, status in enumerate(row['states']):
                    if status == 'attempted':
                        row['states'][i] = 'uncertain'
                        changed = True
            if changed:
                self._save(rows)

    def _load(self):
        value = self.store.get(_KEY, {'format': 1, 'receipts': []})
        if (type(value) is not dict or set(value) != {'format', 'receipts'}
                or type(value['format']) is not int or value['format'] != 1
                or type(value['receipts']) is not list or len(value['receipts']) > _MAX_RECEIPTS):
            raise ValueError('Invalid scene research ledger')
        ids = set()
        active_references = set()
        for row in value['receipts']:
            if (type(row) is not dict or set(row) != {'id', 'plan', 'fingerprint', 'states', 'replies', 'cancelled'}
                    or type(row['id']) is not str or len(row['id']) != 32
                    or any(c not in '0123456789abcdef' for c in row['id']) or row['id'] in ids
                    or type(row['cancelled']) is not bool):
                raise ValueError('Invalid scene research receipt')
            ids.add(row['id'])
            plan = _restore_plan(row['plan'])
            if not row['cancelled']:
                if plan.scene_reference in active_references:
                    raise ValueError('Duplicate unresolved scene reference')
                active_references.add(plan.scene_reference)
            n = len(plan.steps)
            if (row['fingerprint'] != plan.fingerprint or type(row['states']) is not list
                    or len(row['states']) != n or type(row['replies']) is not list or len(row['replies']) != n
                    or any(s not in ('not_started', 'attempted', 'uncertain') for s in row['states'])
                    or sum(s != 'not_started' for s in row['states']) > 1
                    or any(s != 'not_started' for s in row['states'][1:])
                    or row['cancelled'] and any(s != 'not_started' for s in row['states'])):
                raise ValueError('Scene plan or progress changed unexpectedly')
            for status, reply in zip(row['states'], row['replies']):
                if reply is not None and (status != 'uncertain' or type(reply) is not str
                        or not 4 <= len(reply) <= 46 or len(reply) % 2
                        or any(c not in '0123456789abcdef' for c in reply)):
                    raise ValueError('Invalid stored scene observation')
        return value['receipts']

    def _save(self, rows):
        self.store.set(_KEY, {'format': 1, 'receipts': rows})

    @staticmethod
    def _find(rows, identifier):
        if type(identifier) is not str:
            raise ValueError('Invalid scene receipt ID')
        row = next((r for r in rows if r['id'] == identifier), None)
        if row is None:
            raise ValueError('Unknown scene receipt')
        return row

    def prepare(self, plan):
        if type(plan) is not ScenePlan:
            raise ValueError('Expected an immutable scene plan')
        with self.store.transaction():
            rows = self._load()
            if len(rows) >= _MAX_RECEIPTS:
                raise ValueError('Scene research ledger is full')
            if any(not row['cancelled'] and row['plan']['scene_reference'] == plan.scene_reference for row in rows):
                raise ValueError('Scene reference has an unresolved research plan')
            identifier = uuid.uuid4().hex
            rows.append({'id': identifier, 'plan': plan.as_dict(), 'fingerprint': plan.fingerprint,
                         'states': ['not_started'] * len(plan.steps),
                         'replies': [None] * len(plan.steps), 'cancelled': False})
            self._save(rows)
        return identifier

    def mark_attempted(self, identifier, step_index):
        with self.store.transaction():
            rows = self._load()
            row = self._find(rows, identifier)
            integer(step_index, 0, len(row['states']) - 1, 'scene step')
            if row['cancelled'] or any(s != 'not_started' for s in row['states']) or step_index != 0:
                raise ValueError('Unverified scene steps cannot be continued or retried')
            row['states'][step_index] = 'attempted'
            self._save(rows)

    def record_reply(self, identifier, step_index, payload):
        """Keep a raw, untrusted reply; never interpret it as successful completion."""
        if type(payload) is not bytes or not 2 <= len(payload) <= 23:
            raise ValueError('Expected a bounded raw scene observation')
        with self.store.transaction():
            rows = self._load()
            row = self._find(rows, identifier)
            integer(step_index, 0, len(row['states']) - 1, 'scene step')
            if row['states'][step_index] != 'attempted':
                raise ValueError('No fresh scene attempt awaits this observation')
            row['states'][step_index] = 'uncertain'
            row['replies'][step_index] = payload.hex()
            self._save(rows)

    def cancel(self, identifier):
        with self.store.transaction():
            rows = self._load()
            row = self._find(rows, identifier)
            if row['cancelled'] or any(s != 'not_started' for s in row['states']):
                raise ValueError('Only an untouched plan can be cancelled')
            row['cancelled'] = True
            self._save(rows)

    def snapshot(self, identifier):
        row = self._find(self._load(), identifier)
        plan = _restore_plan(row['plan'])
        attempted = any(s != 'not_started' for s in row['states'])
        return {'id': row['id'], 'plan': plan.as_dict(), 'fingerprint': plan.fingerprint,
                'steps': [dict(vars(step), state=state, reply=reply)
                          for step, state, reply in zip(plan.steps, row['states'], row['replies'])],
                'status': 'cancelled' if row['cancelled'] else 'verification_unavailable' if attempted else 'prepared',
                'requires_review': attempted, 'retry_allowed': False,
                'physical_verified': False, 'programming_supported': False}
