"""Read-only recognition and explicit migration of pre-paired scene receipts.

A legacy colour write may be staged even when both stored banks read empty.
Merely loading this module/status never edits receipts or sends radio commands.
"""
import copy
import json
import uuid

_PENDING = ('prepared', 'attempted', 'uncertain')
_MESSAGE = ('An earlier colour write may still be staged. Read-only recovery can '
            'prepare explicit completion; nothing will retry automatically.')


def _ambiguous(journal):
    d = journal.get('detail', {})
    if (journal.get('kind') != 'scene' or d.get('schema') != 1 or
            not d.get('desired') or d.get('completed') is True or
            (journal.get('status') not in _PENDING and d.get('outcome') != 'unchanged')):
        return False
    for step in d.get('steps', []):
        if step.get('property') != 'colour' or not step.get('attempts'):
            continue
        brightness = next((s for s in d.get('steps', []) if
                           s.get('target') == step.get('target') and s.get('property') == 'brightness'), None)
        if brightness is None or brightness.get('status') != 'verified':
            return True
    return False


def _signature(journal):
    """Hardware intent, excluding names/UUIDs of repeated create attempts."""
    d = journal['detail']
    return json.dumps({k: d.get(k) for k in
        ('reference', 'action', 'targets', 'inventory', 'baseline', 'final', 'prior')}, sort_keys=True)


def _groups(engine):
    journals = engine.r.store.journals()
    candidates = [j for j in journals if _ambiguous(j)]
    claimed = set()
    for newer in journals:
        d = newer.get('detail', {})
        if (newer.get('kind') != 'scene' or d.get('schema') != 2 or
                not isinstance(d.get('legacy_sources'), list) or
                (newer.get('status') not in _PENDING and not
                 (newer.get('status') == 'verified' and d.get('completed') is True))):
            continue
        try:
            engine._validate_journal(d, completed_history=newer.get('status') == 'verified')
        except (RuntimeError, ValueError):
            continue
        for old in candidates:
            if old['id'] in d['legacy_sources'] and _signature(old) == _signature(newer):
                claimed.add(old['id'])
    groups = {}
    for old in candidates:
        if old['id'] not in claimed:
            groups.setdefault(old['detail'].get('reference'), []).append(old)
    return list(groups.values())


def unresolved(engine):
    """One virtual quarantine per old reference; does not persist anything."""
    result = []
    for group in _groups(engine):
        row = copy.deepcopy(group[-1])
        d = row['detail']
        row['status'] = 'uncertain'
        d['legacy_ambiguous'] = True
        d['legacy_sources'] = [j['id'] for j in group]
        d['legacy_conflict'] = len({_signature(j) for j in group}) != 1
        d['recovery_ready'] = False
        d['error'] = ('Earlier writes for this reference disagree. Keep this reference reserved for review.'
                      if d['legacy_conflict'] else _MESSAGE)
        result.append(row)
    return result


def related_ids(engine, transaction):
    for group in _groups(engine):
        identifiers = [j['id'] for j in group]
        if transaction in identifiers:
            return identifiers
    return []


def _convert(engine, group):
    """Convert only an unambiguous colour-only attempt, never invent a send."""
    if len({_signature(j) for j in group}) != 1:
        raise RuntimeError('Legacy scene intents disagree; no commands or migration performed')
    for source in group:
        engine._validate_journal(source['detail'])
        old = source['detail']
        # Earlier completed per-light steps need their own paired proof. Leave
        # such rare legacy operations quarantined rather than inventing it.
        sent = [s for s in old['steps'] if s.get('attempts')]
        if (len(sent) != 1 or sent[0]['property'] != 'colour' or
                sent[0]['status'] not in ('attempted', 'not_applied', 'verified') or
                len(sent[0]['attempts']) != 1):
            raise RuntimeError('Legacy scene needs individual review; no commands sent')
        if any(s is not sent[0] and s['status'] != 'not_started' for s in old['steps']):
            raise RuntimeError('Legacy scene progress is not a single colour attempt')
    latest = group[-1]['detail']
    attempted = next(s for s in latest['steps'] if s.get('attempts'))
    if any(next(s for s in j['detail']['steps'] if s.get('attempts'))['target'] != attempted['target']
           for j in group):
        raise RuntimeError('Legacy scene targets disagree; no commands sent')
    d = {k: copy.deepcopy(latest[k]) for k in
         ('scene_id', 'reference', 'action', 'prior', 'desired', 'targets', 'inventory', 'baseline', 'final')}
    d.update(schema=2, observed=copy.deepcopy(d['baseline']), recovery_ready=False,
             legacy_sources=[j['id'] for j in group], legacy_migrated=True)
    d['steps'] = [dict(s, status='not_started') for s in
                  engine._make_steps(2, d['baseline'], d['final'], d['targets'])]
    step = next((s for s in d['steps'] if s['target'] == attempted['target']), None)
    if step is None or step is not d['steps'][0] or step['property'] != 'preset':
        raise RuntimeError('Legacy colour does not match the first saved preset')
    target = str(step['target'])
    step['status'] = 'attempted'
    step['attempts'] = [{
        'before': copy.deepcopy(d['baseline'][target]),
        'expected': copy.deepcopy(d['final'][target]),
        'commands': [
            {'property': 'colour', 'status': 'attempted',
             'request_token': attempted['attempts'][-1]['request_token']},
            {'property': 'brightness', 'status': 'not_started', 'request_token': None}]}]
    d['plan_sha256'] = engine._plan_digest(d)
    engine._validate_journal(d)
    return d, step


async def migrate(engine, transaction):
    """An explicit Recover surveys then transfers uncertainty; no setter calls."""
    async with engine._admit(transaction) as (generation, identity):
        group = next((g for g in _groups(engine) if transaction in [j['id'] for j in g]), None)
        if group is None:
            raise ValueError('Legacy scene was already recovered; use its current operation')
        detail, step = _convert(engine, group)
        if identity != detail['inventory']:
            raise RuntimeError('Inventory changed; preserve the legacy scene for review')
        observed = await engine._survey(detail['reference'], detail['targets'], generation, identity)
        if not engine._valid_intermediate(detail, step, observed):
            raise RuntimeError('Unexpected scene properties; legacy intent remains reserved')
        engine._fence(generation, identity)
        identifier = uuid.uuid4().hex
        # This one atomic transfer is an explicit user recovery action. Old
        # closed receipts stay byte-for-byte unchanged. Pending receipts retain
        # their detail and are superseded by the new unresolved receipt.
        with engine.r.store.transaction():
            for source in group:
                if source['status'] in _PENDING:
                    changed = engine.r.store.db.execute(
                        'UPDATE journals SET status=? WHERE id=? AND status=?',
                        ('cancelled', source['id'], source['status'])).rowcount
                    if changed != 1:
                        raise RuntimeError('Legacy operation changed during recovery')
            engine.r.store.db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
                (identifier, 'scene', step['target'], 'uncertain', json.dumps(detail)))
        engine._revoke(detail['scene_id'])
        journal = dict(id=identifier, kind='scene', target=step['target'], status='uncertain', detail=detail)
        result = engine._recover_pair(journal, observed, generation, identity)
        result['legacy_sources'] = detail['legacy_sources']
        engine._changed()
        return result
