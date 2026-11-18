"""Metadata edits and explicit recovery advice; no radio or invented protocols."""
import copy
import json
from .migration import canonical_digest
from .protocol import integer
from .membership import MEMBERSHIP_SCHEMAS
from .identity import (wired_reset_profile, bootstrap_reset_profile, pre_key_claim_profile, rl4_public_key_claim_profile, a19_public_key_claim_profile,
                       rl4_pre_network_key_claim_profile)


def revision(store):
    return canonical_digest(store.inventory())


def edit_metadata(store, request):
    if not isinstance(request,dict) or request.get('revision')!=revision(store):
        raise ValueError('Inventory changed; refresh before editing')
    data=copy.deepcopy(store.inventory())
    operation=request.get('operation')
    if operation=='update':
        if set(request)!={'operation','revision','kind','target','name','exposed'} or request['kind'] not in ('device','group'):
            raise ValueError('Expected exact metadata update')
        collection,key=('devices','device_id') if request['kind']=='device' else ('groups','group_id')
        integer(request['target'],1,65407,'target')
        item=next((i for i in data[collection] if i[key]==request['target']),None)
        if item is None or item.get('protected'):
            raise ValueError('Unknown or protected object')
        item.update(name=request['name'],exposed=request['exposed'])
    elif operation=='create_group':
        if set(request)!={'operation','revision','name'}:
            raise ValueError('Expected group name')
        reserved=set(store.get('known_targets',[])) | {g['group_id'] for g in data['groups']} | {24566}
        target=next((i for i in range(256,24576) if i not in reserved),None)
        if target is None:raise ValueError('No unused group identifiers')
        data['groups'].append({'group_id':target,'name':request['name'],'members':[],
                               'protected':False,'exposed':False})
    elif operation=='forget_empty_group':
        if set(request)!={'operation','revision','target'}:
            raise ValueError('Expected group target')
        group=next((g for g in data['groups'] if g['group_id']==request['target']),None)
        if group is None or group['protected'] or group['members']:
            raise ValueError('Only ordinary empty metadata groups can be forgotten')
        target=group['group_id']
        data['groups'].remove(group)
    else:
        raise ValueError('Unknown metadata operation')
    store.import_inventory(data)
    result = {'saved':True,'revision':revision(store),'physical_memberships_changed':False}
    if operation == 'create_group':
        result['group_id'] = target
    return result


def recovery_advice(journal):
    kind,state=journal['kind'],journal['status']
    if kind in ('remove','external_reset') and state == 'verified' and journal['detail'].get('publication_pending') is True:
        return {'actions':['refresh_removed_publication'],'reason':'removal_publication_pending','retry_allowed':False}
    if state not in ('prepared','attempted','uncertain'):
        return {'actions':[],'reason':'terminal_receipt_retained'}
    if kind=='remove' and state=='uncertain' and journal['detail'].get('identity_source')=='cloud_import':
        return {'actions':['reconcile','confirm_imported_removal'],
                'reason':'imported_removal_owner_confirmation','retry_allowed':False}
    if kind == 'scene':
        return {'actions': ['reconcile'], 'reason': 'read_only_scene_property_verification',
                'retry_allowed': False, 'scene_id': journal['detail'].get('scene_id')}
    if kind=='anyplace_setup':
        detail=journal['detail']
        if detail.get('product_id') != 81:
            return {'actions':[],'reason':'invalid_anyplace_setup_receipt','retry_allowed':False}
        if state=='prepared' and detail.get('assignment_attempted') is False:
            return {'actions':['cancel_unattempted'],'reason':'no_dispatch_attempt_recorded',
                    'retry_allowed':False,'reservation_retained':True}
        if state=='uncertain' and detail.get('assignment_attempted') is True:
            actions=['reconcile']
            if journal['target']!=32898:actions.append('observe_owner_reset')
            return {'actions':actions,'reason':'observation_only; owner reset requires fresh exact unpaired identity',
                    'retry_allowed':False,'assignment_read_back':False}
        return {'actions':[],'reason':'review_anyplace_attempt_state','retry_allowed':False}
    if state=='prepared' and kind in ('claim','remove','keypad_edit','keypad_setup','accessory_setup','accessory_native_setup','dimmer_edit','group_membership'):
        # Only before any durable attempt. Allocations and receipts remain burned.
        return {'actions':['cancel_unattempted'],'reason':'no_dispatch_attempt_recorded'}
    if pre_key_claim_profile(journal):
        return {'actions':['retire_pre_key_claim'],'reason':'Only initial association request submitted; verify fresh exact unpaired UUID, retain receipt/address, never auto-retry'}
    if rl4_public_key_claim_profile(journal) or a19_public_key_claim_profile(journal):
        return {'actions':['retire_pre_key_claim'],
                'reason':'Only public-key exchange submitted, before address or network key; require isolated worker and fresh exact unpaired evidence, retain receipt/address, never auto-retry',
                'retry_allowed':False, 'physical_reset_verified':False}
    if rl4_pre_network_key_claim_profile(journal):
        return {'actions':['retire_pre_network_key_claim'],
                'reason':'Stopped after the proposed address but before the network key; require isolated worker and fresh exact unpaired evidence, permanently retire the address, never auto-retry',
                'retry_allowed':False, 'physical_reset_verified':False}
    if kind=='group_membership' and journal['detail'].get('readback_schema') in MEMBERSHIP_SCHEMAS.values():
        return {'actions':['reconcile'],'reason':'read_only_membership_verification','retry_allowed':False}
    if kind=='group_membership':
        return {'actions':[],'reason':'qualified_membership_readback_required',
                'evidence_required':['firmware-specific COUNT/READ schema','authenticated target and reply correlation',
                                     'exact complete table after restart','physical regression for add/delete'],
                'retry_allowed':False}
    if (kind=='claim' and state=='uncertain' and journal['target']!=32898
            and bootstrap_reset_profile(journal['detail'])
            and journal['detail'].get('inventory_complete') is True):
        actions=['verify_bootstrap_owner_reset']
        if journal['detail'].get('association_complete') is True:
            actions.insert(0,'reconcile_claim')
        return {'actions':actions,'reason':'first_device_only; explicit owner reset and fresh unpaired proof required',
                'retry_allowed':False}
    if (kind=='claim' and state=='uncertain' and journal['target']!=32898
            and wired_reset_profile(journal['detail'].get('uuid'), journal['detail'].get('product_id'))
            and journal['detail'].get('inventory_complete') is True):
        actions=['verify_claim_owner_reset']
        if journal['detail'].get('association_complete') is True:
            actions.insert(0,'reconcile_claim')
        return {'actions':actions,'reason':'fresh_proof_required; no association retry','retry_allowed':False}
    if kind=='claim' and journal['detail'].get('association_complete') is True and journal['detail'].get('inventory_complete') is True:
        return {'actions':['reconcile_claim'],'reason':'authenticate_reserved_address_after_uncertain_association',
                'no_proof_action':'retain_quarantine_and_address; do not retry association'}
    if kind == 'keypad_setup' and state == 'uncertain':
        detail = journal['detail']
        planned, attempted = detail.get('planned'), detail.get('attempted')
        valid_slots = lambda rows: (isinstance(rows, list) and bool(rows)
            and all(type(slot) is int and 1 <= slot <= 4 for slot in rows)
            and len(set(rows)) == len(rows))
        if (detail.get('product_id') in (126, 127) and valid_slots(planned)
                and valid_slots(attempted) and set(attempted) <= set(planned)):
            return {'actions':['resolve_keypad_setup', 'reconcile'],
                    'reason':'partial_keypad_setup_read_or_continue', 'retry_allowed':False}
    if kind in ('remove','external_reset','keypad_edit','keypad_setup','accessory_setup','accessory_native_setup','dimmer_edit'):
        actions=['reconcile']
        if kind=='keypad_edit' and journal['detail'].get('delete_checkpoint') and journal['detail'].get('stages')==['delete']:
            actions.append('resume_unattempted_insertion')
        if kind=='accessory_setup' and journal['target']!=32898:actions.append('observe_owner_reset')
        if kind=='dimmer_edit':actions.append('accept_fresh_observation')
        return {'actions':actions,'reason':'evidence_required; ambiguous writes are not retried'}
    return {'actions':[],'reason':'no_qualified_recovery_path'}


def cancel_unattempted(store, identifier):
    journal=next((j for j in store.journals() if j['id']==identifier),None)
    if journal is None or 'cancel_unattempted' not in recovery_advice(journal)['actions']:
        raise ValueError('Only a prepared operation without a durable attempt can be cancelled')
    # Stage markers must also be absent; refuse inconsistent/corrupt prepared rows.
    detail=journal['detail']
    if detail.get('stages') or detail.get('attempted') or detail.get('assignment_attempted') or detail.get('ping_attempted'):
        raise ValueError('Attempt evidence prevents cancellation')
    with store.transaction():
        current=next(j for j in store.journals() if j['id']==identifier)
        if current!=journal:
            raise ValueError('Receipt changed before cancellation')
        detail['cancelled_before_dispatch']=True
        if journal['kind']=='anyplace_setup':
            detail['dispatch_not_started']=True
        store.db.execute("UPDATE journals SET status='cancelled',detail=? WHERE id=? AND status='prepared'",(json.dumps(detail),identifier))
    return {'cancelled':True,'receipt_and_reservations_retained':True,'write_sent':False}
