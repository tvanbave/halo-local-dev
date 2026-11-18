"""Read-only controller presentation. Action availability is not hardware qualification.

Consumes current-session bindings only. Endpoint/service admission remains authoritative.
No transport, storage writes, recovery dispatch, or automatic write retries occur here.
"""
from .keypad import snapshot
from .keypad_ownership import owned_assignments, owned_targets, stored_tables
from .dimmer_control import dimmer_control_status, retained_dimmer_claim
from .anyplace import cancelled_setups_allow_new
from .accessory import fresh_native_claim
from .protocol import Packet, decode_controller

MODELS = {127: 'Wired Scene Keypad', 126: 'Battery Scene Keypad',
          91: 'Accessory Dimmer', 81: 'Anyplace Dimmer'}
PENDING = ('prepared', 'attempted', 'uncertain')


def accessory_ha_status(target, journals, groups, retired_claims, observed):
    """Describe retained setup evidence; a group name alone never establishes ownership."""
    setups = [j for j in journals if j['kind'] == 'accessory_setup' and j['target'] == target]
    result = {'state': 'not_configured', 'target': None, 'transaction': None,
              'observed_in_session': False, 'assignment_read_back': False}
    if not setups:
        return result
    result['state'] = 'needs_review'
    if len(setups) != 1 or setups[0]['status'] != 'verified':
        return result
    setup = setups[0]
    detail = setup.get('detail', {})
    reservation = detail.get('reservation', {})
    group = reservation.get('group_id')
    claim = next((j for j in journals if j['id'] == detail.get('claim')
                  and j['kind'] == 'claim' and j['target'] == target
                  and j['status'] == 'verified' and j['id'] not in retired_claims
                  and j.get('detail', {}).get('product_id') == 91), None)
    if (claim is None or type(group) is not int or not 256 <= group <= 24575
            or reservation.get('protected') is not True or reservation.get('members') != []
            or reservation.get('exposed') is not False or reservation not in groups
            or sum(g.get('group_id') == group for g in groups) != 1):
        return result
    result.update(state='configured', target=group, transaction=setup['id'],
                  observed_in_session=bool(observed and observed['target'] == group))
    return result


def accessory_native_status(target, journals, groups, retired_claims, observed, *, retired_addresses=()):
    """Describe exact retained behavior evidence, not a stored assignment read."""
    setups = [j for j in journals if j['kind'] == 'accessory_native_setup' and j['target'] == target]
    result = {'state':'not_configured', 'target':None, 'transaction':None,
              'observed_in_session':False, 'assignment_read_back':False}
    if not setups:
        return result
    result['state'] = 'needs_review'
    if len(setups) != 1 or setups[0]['status'] != 'verified':
        return result
    setup = setups[0]
    detail = setup.get('detail', {})
    reservation = detail.get('reservation', {})
    group = reservation.get('group_id')
    evidence = detail.get('observed', {})
    sequence = evidence.get('sequence')
    baseline = detail.get('baseline', {}).get('sequence')
    try:
        claim = fresh_native_claim(target, journals, retired_claims, retired_addresses, receipt=setup)
        payload = bytes.fromhex(evidence.get('payload', ''))
    except (ValueError, TypeError):
        return result
    destinations = [g for g in groups if g.get('group_id') == group]
    if (not isinstance(setup.get('id'), str) or not setup['id']
            or detail.get('mode') != 'native' or detail.get('product_id') != 91
            or detail.get('assignment_attempted') is not True
            or detail.get('assignment_read_back') is not False
            or claim['id'] != detail.get('claim') or claim != detail.get('claim_receipt')
            or type(group) is not int or not 256 <= group <= 24575
            or reservation.get('protected') is not False or not reservation.get('members')
            or len(destinations) != 1 or destinations[0].get('protected') is not False
            or type(sequence) is not int or not 0 <= sequence < 0x1000000
            or type(baseline) is not int or not 0 <= baseline < 0x1000000
            or not 0 < ((sequence-baseline) & 0xffffff) < 0x800000):
        return result
    event = decode_controller(Packet(sequence, target, 0x8000, 115, payload), 91)
    if not event or 'brightness' not in event or event.get('target') != group:
        return result
    # Ordinary group names, membership and exposure may legitimately change after
    # setup. The retained target and association identity must still be intact.
    result.update(state='configured', target=group, transaction=setup['id'],
                  observed_in_session=bool(observed and observed['target'] == group))
    return result


def _anyplace_claim(target, journals, retired_claims):
    claims = [j for j in journals if j['kind'] == 'claim' and j['target'] == target
              and j['status'] == 'verified' and j['id'] not in retired_claims]
    return claims[0] if len(claims) == 1 and claims[0].get('detail', {}).get('product_id') == 81 else None


def anyplace_ha_status(target, journals, groups, retired_claims, observed, *, target_sources=None,
                       known_targets=(), dimmer_token=0):
    """Retained experimental model-81 setup evidence, never isolation/readback proof."""
    result = {'state': 'not_configured', 'target': None, 'transaction': None,
              'observed_in_session': False, 'assignment_read_back': False,
              'original_assignment': 'unknown', 'experimental': True,
              'ha_receipt_verified': False, 'physical_isolation_verified': False}
    claim = _anyplace_claim(target, journals, retired_claims)
    operations = [j for j in journals if j['target'] == target and
                  (j['kind'].endswith('_setup') or j['kind'] in ('dimmer_edit', 'keypad_edit'))]
    if not operations:
        return result
    result['state'] = 'needs_review'
    setups = [j for j in operations if j['kind'] == 'anyplace_setup' and j['status'] == 'verified']
    if len(setups) > 1:
        return result
    setup = setups[0] if setups else None
    prior = [j for j in operations if j is not setup]
    if not claim or not cancelled_setups_allow_new(prior, target=target, claim=claim,
            groups=groups, known_targets=known_targets, target_sources=target_sources, dimmer_token=dimmer_token):
        return result
    if setup is None:
        result['state'] = 'not_configured'
        return result
    detail = setup.get('detail', {})
    reservation = detail.get('reservation', {})
    group = reservation.get('group_id')
    if (detail.get('product_id') != 81 or detail.get('assignment_attempted') is not True
            or detail.get('original_assignment') != 'unknown'
            or not claim or claim['id'] != detail.get('claim')
            or type(group) is not int or not 256 <= group <= 24575
            or reservation.get('protected') is not True or reservation.get('members') != []
            or reservation.get('exposed') is not False or reservation not in groups
            or sum(g.get('group_id') == group for g in groups) != 1
            or any(source != target for source in (target_sources or {}).get(str(group), []))):
        return result
    result.update(state='configured', target=group, transaction=setup['id'],
                  observed_in_session=bool(observed and observed['target'] == group))
    return result


def _table(value):
    """Reject incomplete or inconsistent presentation data rather than guessing slots."""
    if not isinstance(value, dict):
        return None
    try:
        rebuilt = snapshot(bytes.fromhex(value['count']),
                           [bytes.fromhex(row['raw']) for row in value['slots']])
        return rebuilt if rebuilt == value else None
    except (KeyError, TypeError, ValueError):
        return None


def controller_workflow(device, *, capabilities=None, mesh_ready=False, busy=False,
                        preview=False, table=None, journals=(), local_mode=False,
                        local_baseline=False, native_observation=None, now=0,
                        mqtt_enabled=False, mqtt_ready=False, retired_claims=(), retired_addresses=(),
                        groups=(), last_event=None, owned_ha=None, target_sources=None, known_targets=(),
                        dimmer_token=0, bindings=None, cached_mapping=None):
    """Return safe UI data for one inventory device, or None for a non-controller.

    ``table`` must come from the live router, not persisted bindings. Native observation
    is the runtime's (event, monotonic milliseconds) tuple. No receipt proves physical
    model qualification. Action ``enabled`` only means this presentation preflight passes.
    """
    model, target = device['product_id'], device['device_id']
    if model not in MODELS:
        return None
    caps = capabilities or {}
    pending = [j for j in journals if j['status'] in PENDING]
    own_pending = [j for j in pending if j['target'] == target]
    current = _table(table) if model in (126, 127) else None
    blocked = ('read_only_preview' if preview else 'mesh_offline' if not mesh_ready else
               'operation_in_progress' if busy else 'resolve_pending_operations' if pending else None)
    actions = {}

    def action(name, supported, reason=None, *, physical=True):
        failure = reason if not supported else (blocked if physical else
                  'read_only_preview' if preview else 'operation_in_progress' if busy else
                  'resolve_pending_operations' if pending else None)
        actions[name] = {'enabled': bool(supported and not failure),
                         'reason': failure or 'available'}

    action('read_assignments', model in (126, 127), 'assignment_readback_unsupported')
    edit_flag = 'wired_editor' if model == 127 else 'battery_editor'
    editor_supported = model in (126, 127) and caps.get(edit_flag) is True and bool(current)
    # Draft presentation survives transient radio work. Physical admission below
    # still includes busy, and cached mappings never count as a current table.
    editor_available = bool(editor_supported and not preview and mesh_ready and not pending)
    action('edit_assignment', editor_supported,
           'read_assignments_first' if model in (126, 127) and caps.get(edit_flag) is True
           else 'model_editor_disabled')
    setup_flag = {127: 'wired_onboarding', 126: 'battery_onboarding', 91: 'accessory_onboarding',
                  81: 'anyplace_onboarding'}.get(model)
    setup_reason = None
    if not setup_flag or caps.get(setup_flag) is not True:
        setup_reason = 'model_setup_disabled'
    elif caps.get('inventory_complete') is not True:
        setup_reason = 'authoritative_inventory_required'
    elif model == 91:
        claim = any(j['kind'] == 'claim' and j['target'] == target and j['status'] == 'verified'
                    and j['id'] not in retired_claims and j.get('detail', {}).get('product_id') == 91
                    for j in journals)
        if target == 32898 or not claim or any(j['kind'] in ('accessory_setup','accessory_native_setup','dimmer_edit') and j['target'] == target for j in journals):
            setup_reason = 'fresh_retained_claim_required'
    elif model == 81:
        claim = _anyplace_claim(target, journals, retired_claims)
        prior = [j for j in journals if j['target'] == target and
                 (j['kind'].endswith('_setup') or j['kind'] in ('dimmer_edit', 'keypad_edit'))]
        if not claim or not cancelled_setups_allow_new(prior, target=target, claim=claim,
                groups=groups, known_targets=known_targets, target_sources=target_sources, dimmer_token=dimmer_token):
            setup_reason = 'fresh_retained_claim_required'
    action('setup_ha', setup_reason is None, setup_reason)
    native_setup_reason = (setup_reason if model == 91 else 'model_setup_disabled')
    if native_setup_reason is None:
        try:
            fresh_native_claim(target, journals, retired_claims, retired_addresses)
        except ValueError:
            native_setup_reason = 'fresh_retained_claim_required'
    if native_setup_reason is None and not any(g.get('protected') is False and g.get('members')
            and type(g.get('group_id')) is int and 256 <= g['group_id'] <= 24575 for g in groups):
        native_setup_reason = 'ordinary_destination_required'
    action('setup_native', native_setup_reason is None, native_setup_reason)
    action('local_input', model == 91, 'local_input_only_accessory_91', physical=False)
    observed = None
    if model in (81, 91) and native_observation:
        event, when = native_observation
        age = now - when
        generation = event.get('target_generation', 0)
        if (0 <= age <= 15000 and type(event.get('target')) is int
                and 256 <= event['target'] <= 24575 and type(generation) is int and generation >= 0):
            observed = {'target': event['target'], 'age_ms': age, 'valid_for_ms': 15000 - age,
                        'baseline': {'target': event['target'], 'observed_ms': when,
                                     'target_generation': generation},
                        'evidence': 'observed_native_command', 'assignment_read_back': False}
    # This opens a deliberate review, not an immediate write. The service separately
    # validates the selected destination, expected baseline, deadline and wake.
    native_flag = 'anyplace_editor' if model == 81 else 'dimmer_editor'
    native_reason = ('model_editor_disabled' if model not in (81, 91) or caps.get(native_flag) is not True
        else 'fresh_native_baseline_required' if not observed
        else 'protected_native_source' if any(g.get('protected') and g.get('group_id') == observed['target'] for g in groups)
        else 'ordinary_destination_required' if not any(not g.get('protected') and type(g.get('group_id')) is int
            and 256 <= g['group_id'] <= 24575 and g['group_id'] != observed['target'] for g in groups)
        else None)
    action('assign_native_target', native_reason is None, native_reason)
    action('clear_native_assignment', False, 'assignment_clear_unsupported')
    control_configuration = None
    if model in (81, 91):
        control_configuration = dimmer_control_status(target, model, journals, groups,
            retired_claims=retired_claims, retired_addresses=retired_addresses,
            target_sources=target_sources, bindings=bindings)
        configure_reason = ('model_editor_disabled' if caps.get(native_flag) is not True else
                            'authoritative_inventory_required' if caps.get('inventory_complete') is not True else None)
        if configure_reason is None:
            try:
                retained_dimmer_claim(target, model, journals, retired_claims=retired_claims,
                                      retired_addresses=retired_addresses)
            except ValueError:
                configure_reason = 'retained_dimmer_claim_required'
        # Opening the form must not race a fifteen-second observation lifetime.
        # Preview and dispatch validate a fresh signal after the owner chooses.
        action('configure_control', configure_reason is None, configure_reason)


    protected_targets = {g['group_id'] for g in groups if g.get('protected')}
    owned_ha = (owned_assignments(target, journals, groups) if owned_ha is None else owned_ha)
    slots = []
    for row in current['slots'] if current else []:
        unused = row['target'] == 65535 and row['flags'] == 0 and row['level'] == 0
        known = row['flags'] == 8 and 256 <= row['target'] <= 24575 and row['level'] > 0
        unique = sum(r['target'] == row['target'] for r in current['slots']) == 1
        status = 'unused' if unused else 'verified_mapping' if known and unique else 'ambiguous_target' if known else 'unsupported_assignment'
        owned = (owned_ha.get(row['target'], {}).get('slot') == row['slot']
                 and known and unique and row['level'] == 255)
        mode = ('unassigned' if unused else 'ha' if owned else
                'native' if known and row['target'] not in protected_targets else 'unknown')
        slot_editable = ((unused or known)
                         and (row['target'] not in protected_targets or owned and model in (126, 127)
                              and caps.get('inventory_complete') is True))
        editable = actions['edit_assignment']['enabled'] and slot_editable
        draft_editable = editor_available and slot_editable
        ha_reason = ('authoritative_inventory_required' if caps.get('inventory_complete') is not True else
                     'assignment_read_only' if not editable else None)
        slots.append({'slot': row['slot'], 'target': row['target'], 'preset': row['level'], 'raw': row['raw'],
                      'status': status, 'editable': bool(editable), 'draft_editable': bool(draft_editable),
                      'ha_draft_available': bool(draft_editable and caps.get('inventory_complete') is True),
                      'assignment_mode': mode,
                      'ha_owned': bool(owned), 'ha_available': ha_reason is None,
                      'ha_reason': ha_reason, 'ha_ready': bool(owned and mqtt_enabled and mqtt_ready
                          and device.get('exposed', False) and mesh_ready and not pending),
                      'preserved_by_setup': not unused})

    keypad_ha_setup = None
    # A completed setup job may be presented as verified only against its exact
    # retained receipt, the current complete table and owned planned HA slots.
    # A local mode, a group label or an unrelated previous setup proves nothing.
    if model in (126, 127) and current:
        for journal in reversed(journals):
            detail = journal.get('detail', {})
            planned = detail.get('planned')
            if (journal['target'] != target or journal['kind'] != 'keypad_setup'
                    or journal['status'] != 'verified' or detail.get('product_id') != model
                    or not isinstance(planned, list) or not planned
                    or any(type(slot) is not int or not 1 <= slot <= 4 for slot in planned)
                    or len(set(planned)) != len(planned) or _table(detail.get('desired')) != current):
                continue
            if all(slots[slot - 1]['ha_owned'] for slot in planned):
                keypad_ha_setup = {'transaction': journal['id'], 'slots': list(planned),
                                   'revision': current['revision']}
                break

    recent = None
    if last_event:
        event, when = last_event
        age = now - when
        if 0 <= age and event.get('event_type') in ('on', 'off', 'brightness', 'raise', 'lower', 'mesh_command'):
            # No raw capture, payload or inferred gesture is exposed by this view.
            recent = {key: event[key] for key in ('event_type', 'target', 'brightness', 'direction', 'input_mode') if key in event}
            recent.update(age_ms=age, observed_ms=when, evidence='observed_controller_event', assignment_read_back=False)
            if (model in (126, 127) and event.get('button_mapping') == 'verified_table' and current
                    and any(row['slot'] == event.get('button') and row['target'] == event.get('target')
                            and row['status'] == 'verified_mapping' for row in slots)):
                recent['button'] = event['button']
                recent['button_mapping'] = 'verified_table'
                if event.get('activation') is True and any(row['slot'] == event['button'] and row['preset'] == event.get('brightness') for row in slots):
                    recent['activation'] = True
    if (recent is not None and model == 126 and cached_mapping and cached_mapping.get('status') == 'cached'
            and last_event[0].get('button_mapping') == 'cached_table'):
        event = last_event[0]
        row = next((row for row in cached_mapping['slots']
                    if row['slot'] == event.get('button') and row['target'] == event.get('target')), None)
        if row is not None:
            recent.update(button=row['slot'], button_mapping='cached_table')
            if event.get('activation') is True and event.get('brightness') == row['preset']:
                recent['activation'] = True
    notes = ['Capability switches are not physical model qualification.',
             'Unknown write outcomes require review; never repeat automatically.']
    if model in (126, 127):
        notes.append('Read all four assignments twice. HA setup fills only unused slots and preserves occupied or unknown records.')
        notes.append('Button identity requires a verified table with a unique target; native commands alone do not identify a button.')
    if model == 126:
        if cached_mapping and cached_mapping.get('status') == 'cached':
            notes.append('Owned HA button events use a cached previously verified table. No assignments have been read back this session; wake and read before editing. Out-of-band swaps between existing targets cannot be detected from a press alone.')
        notes.append('Close the HALO app. When prompted, press the bottom Power/Off button to wake the keypad for each read round. A 20-second cooldown may apply. Battery writers are disabled by default.')
    if model == 91:
        notes.append('Local input changes only the event decoder. It does not read, clear or change native assignments, or prove HA-only operation.')
        notes.append('The first local report after reconnect and query replies are silent. Brightness is a reported level, not a press/release gesture; smooth held dimming is unqualified.')
    if model == 81:
        notes.append('Fresh HA setup is experimental and separately gated for a freshly claimed factory-reset spare. The original assignment is unknown; stored assignment reads, clear and automatic restoration are unsupported.')
        notes.append('When prompted, press the small Wake-Up button on the bottom once. Then use On, Off or the slider to observe the target. An observed command does not prove HA receipt or physical isolation.')
    if model in (81, 91):
        notes.append('Observed target behavior is not stored assignment readback.')
    recovery = []
    for j in own_pending:
        kind = j['kind']
        mode = ('observation_only' if kind in ('accessory_setup', 'accessory_native_setup', 'anyplace_setup', 'dimmer_edit') else
                'readback_only' if kind == 'keypad_edit' else
                'explicit_guarded_continuation' if kind == 'keypad_setup' else 'review_required')
        recovery.append({'transaction': j['id'], 'kind': kind, 'status': j['status'],
                         'mode': mode, 'automatic_resume': False,
                         'warning': 'Keypad setup reconciliation can write previously unattempted slots; review before continuing.'
                         if kind == 'keypad_setup' else 'Do not resend an uncertain write.'})
    native_receipts = [j for j in journals if j['target'] == target and j['kind'] == 'dimmer_edit']
    accepted_native = native_receipts[-1]['id'] if native_receipts and native_receipts[-1]['status'] == 'verified' else None
    return {'device_id': target, 'product_id': model, 'model': MODELS[model],
            'keypad_ha_setup': keypad_ha_setup, 'control_configuration': control_configuration,
            'accessory_ha': accessory_ha_status(target, journals, groups, retired_claims, observed)
            if model == 91 else None,
            'anyplace_ha': anyplace_ha_status(target, journals, groups, retired_claims, observed,
                                            target_sources=target_sources, known_targets=known_targets,
                                            dimmer_token=dimmer_token) if model == 81 else None,
            'accessory_native': accessory_native_status(target, journals, groups, retired_claims, observed,
                retired_addresses=retired_addresses) if model == 91 else None,
            'qualification': 'not_established_by_this_view', 'actions': actions, 'notes': notes,
            'editor_available': editor_available,
            'assignment_evidence': 'verified_current_session_table' if current else 'cached_table' if cached_mapping and cached_mapping.get('status') == 'cached' else 'unknown',
            'cached_mapping': cached_mapping,
            'assignment_read_back': bool(current), 'revision': current['revision'] if current else None,
            'slots': slots, 'observed_target': observed, 'last_event': recent, 'recovery': recovery,
            'accepted_native_transaction': accepted_native,
            'input_mode': 'local_level_reports' if model == 91 and local_mode else 'native_commands',
            'input_status': 'waiting_for_baseline' if model == 91 and local_mode and not local_baseline else 'observing',
            'ha_status': 'disabled' if not mqtt_enabled else 'disconnected' if not mqtt_ready else
                         'not_exposed' if not device.get('exposed', False) else 'publishing_enabled'}


def controller_workflows(runtime, *, read_only=False):
    """Adapter for /api/snapshot; reads metadata only and never triggers an operation."""
    journals = runtime.store.journals()
    router = runtime.core.controllers
    result = []
    inventory = runtime.store.inventory()
    for device in inventory['devices']:
        view = controller_workflow(device, capabilities=runtime.capabilities,
            mesh_ready=runtime.mesh_ready, busy=runtime.operation.locked(),
            preview=read_only or getattr(runtime, 'preview_only', False),
            table=router.bindings.get(device['device_id']), journals=journals,
            local_mode=device['device_id'] in router.local_modes,
            local_baseline=device['device_id'] in router.baselines,
            native_observation=runtime.native_observations.get(device['device_id']), now=runtime.clock(),
            mqtt_enabled=runtime.mqtt_enabled, mqtt_ready=runtime.mqtt_ready,
            retired_claims=runtime.store.get('retired_claims', []),
            retired_addresses=runtime.store.get('retired_addresses', []), groups=inventory.get('groups', []),
            target_sources=runtime.store.get('target_sources', {}),
            known_targets=runtime.store.get('known_targets', []),
            dimmer_token=runtime.store.get('dimmer_token', 0), bindings=stored_tables(runtime.store) if device['product_id'] in (81, 91) else None,
            owned_ha=owned_targets(runtime.store, device['device_id']),
            last_event=getattr(runtime, 'controller_events', {}).get(device['device_id']),
            cached_mapping=router.battery_cache.status(device['device_id'])
                if not read_only and not getattr(runtime, 'preview_only', False)
                and device['product_id'] == 126 and getattr(router, 'battery_cache', None) is not None
                and device['device_id'] not in router.bindings else None)
        if view is not None:
            result.append(view)
    return result
