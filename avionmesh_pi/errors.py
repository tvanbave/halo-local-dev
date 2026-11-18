"""Explicit admission failures raised before a requested operation starts."""


class OperationNotStarted(RuntimeError):
    MESSAGES = {
        'background_read_busy': 'The bridge is still finishing a background state read. No requested operation was started. Try again in a moment.',
        'mesh_busy': 'The bridge is busy with another operation. No requested operation was started. Try again when it finishes.',
        'mesh_offline': 'The bridge is disconnected from the lights. No requested operation was started. Try again after it reconnects.',
        'session_changed': 'The mesh connection changed while this request was waiting. No requested operation was started. Refresh and review it again.',
        'inventory_changed': 'The device or group inventory changed while this request was waiting. No requested operation was started. Refresh and review it again.',
        'pending_operation': 'An unfinished operation needs review. This request was not started. Review Operations before another change.',
        'removal_identity_unavailable': 'The bridge has no complete pairing identity or supported cloud-import record for this device. No removal was sent. Refresh the import or review the device identity before trying again.',
        'removal_identity_ambiguous': 'The saved device identity is ambiguous. No removal was sent. Resolve the conflicting device records before removing it.',
        'removal_review_changed': 'The device or removal requirements changed. No removal was sent. Refresh the page and review removal again.',
        'removal_active_relay': 'This light is the bridge\'s current Bluetooth relay. No removal was sent. Connect through another powered light before removing it.',
        'removal_native_dependency': 'A wall control still depends on this light or its group. No removal was sent. Change that assignment before removing the light.',
        'removal_disabled': 'Device removal is switched off on this bridge. No removal was sent. Enable removal in the bridge configuration first.',
        'removal_target_unknown': 'This device is no longer in the bridge inventory. No removal was sent. Refresh the page before continuing.',
        'removal_baseline_unavailable': 'The bridge could not complete the initial unpaired-device scan. No removal was sent. Keep the lights powered and try again after Bluetooth reconnects.',
        'removal_target_unresponsive': 'This light did not answer the identity check. No removal was sent. Keep it powered on and check its connection before trying again.',
        'anyplace_wake_timeout': 'No fresh Anyplace wake was received. No controller command was sent. Review setup and start a new request when the dimmer is ready.',
        'anyplace_admission_expired': 'The Anyplace wake or setup conditions changed before dispatch. No controller command was sent. Refresh and review setup before starting a new request.',
    }

    def __init__(self, code):
        if code not in self.MESSAGES:
            raise ValueError('Unknown operation admission failure')
        self.code = code
        self.requires_review = code == 'pending_operation'
        super().__init__(self.MESSAGES[code])

    def response(self):
        return {'error': self.code, 'message': str(self), 'operation_started': False,
                'requires_review': self.requires_review, 'retry_allowed': False}
