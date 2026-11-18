"""Room presentation over guarded membership, never a second programming path.

Names and recorded members are inventory metadata, not fresh physical evidence.
The physical operation is available only with both existing capability gates.
"""
from .membership import Membership, MEMBERSHIP_SCHEMAS
from .protocol import integer


class Rooms:
    def __init__(self, runtime):
        self.runtime = runtime
        self.membership = Membership(runtime)

    @property
    def enabled(self):
        return all(self.runtime.capabilities.get(key) is True for key in
                   ('group_membership', 'group_membership_readback'))

    def overview(self):
        inventory = self.runtime.store.inventory()
        return {
            'programming_enabled': self.enabled,
            'disabled_reason': None if self.enabled else
                'Physical room setup is unavailable until membership programming and physical readback are enabled after qualification.',
            'metadata_notice': 'Room names and recorded members are metadata, not fresh physical membership evidence.',
            'automatic_resume': False,
            'batch_limit': 32,
            'rooms': [dict(group, membership_evidence='recorded_metadata')
                      for group in inventory['groups'] if not group['protected'] and group['group_id'] != 24566],
            'lights': [{'target': device['device_id'], 'name': device['name'],
                        'eligible': device['product_id'] in MEMBERSHIP_SCHEMAS,
                        'reason': None if device['product_id'] in MEMBERSHIP_SCHEMAS else 'Membership readback is not qualified for this model.'}
                       for device in inventory['devices'] if device['product_id'] in (90, 93, 94, 97, 134, 137, 162, 167)],
        }

    async def change(self, targets, group, member):
        if not self.enabled:
            raise ValueError('Physical room setup requires enabled membership programming and readback')
        # The lower layer validates every selection and group before any radio I/O.
        integer(group, 256, 24575, 'group')
        result = await self.membership.change_many(targets, group, member)
        names = {d['device_id']: d['name'] for d in self.runtime.store.inventory()['devices']}
        pending = self.runtime.store.journals(pending=True)
        rows = []
        for outcome in result['results']:
            row = dict(outcome, name=names.get(outcome['target'], str(outcome['target'])))
            if row.get('membership_verified'):
                row['status'] = ('verified' if row.get('desired_matched') else 'not_applied')
                if row.get('publication_pending') and row.get('desired_matched'):
                    row['status'] = 'verified_publication_pending'
            else:
                journal = next((j for j in pending if j['kind'] == 'group_membership'
                                and j['target'] == row['target'] and j['detail'].get('group') == group), None)
                row['status'] = 'needs_readback' if journal else 'not_verified'
                row['transaction'] = journal['id'] if journal else None
                row['recovery_action'] = ('reconcile' if journal and journal['status'] == 'uncertain'
                                          and journal['detail'].get('readback_schema') in MEMBERSHIP_SCHEMAS.values() else None)
            row['retry_allowed'] = False
            rows.append(row)
        rows.extend({'target': target, 'name': names.get(target, str(target)),
                     'status': 'not_attempted', 'membership_verified': False,
                     'retry_allowed': False} for target in result['remaining'])
        return dict(result, rows=rows, group=group, desired_member=member,
                    physical_memberships_verified=result['complete'],
                    message=('Selected lights have verified physical membership.' if result['complete'] else
                             'Stopped. Review each light below. Remaining lights were not attempted; nothing resumes automatically.'))
