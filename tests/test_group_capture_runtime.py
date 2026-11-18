"""Offline integration evidence for group capture; no real transport or broker."""
import tempfile
import unittest

from avionmesh_pi.protocol import decrypt_mcp, encrypt_mcp
from avionmesh_pi.runtime import Runtime
from support import synthetic_store


KEY = bytes(range(16))
GROUP = 291
CONTROLLER = 40001
MEMBERS = [40002, 40003]
OUTSIDE = 40004
# Standard absolute group brightness: destination zero, noun 10, group 291.
GROUP_ON = bytes.fromhex('000073000a012300ff00000000')
INVENTORY = {
    'devices': [
        {'device_id': CONTROLLER, 'product_id': 81, 'name': 'Synthetic dimmer', 'exposed': False},
        *[{'device_id': target, 'product_id': 134, 'name': f'Synthetic light {target}', 'exposed': False}
          for target in MEMBERS + [OUTSIDE]],
    ],
    'groups': [{'group_id': GROUP, 'name': 'Synthetic group', 'members': MEMBERS,
                'exposed': False, 'protected': False}],
}


class CaptureTransport:
    """Record the exact wire call without decrypting, replying, or retrying it."""
    def __init__(self, now):
        self.now = now
        self.ready = True
        self.dropped = 0
        self.frames = []
        self.fail = False

    async def send(self, frame):
        self.frames.append(frame)
        self.now[0] += 7
        if self.fail:
            raise RuntimeError('Synthetic transport failure')


class GroupCaptureRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = synthetic_store(self.temp.name, KEY)
        self.store.import_inventory(INVENTORY)
        self.now = [1000]
        self.transport = CaptureTransport(self.now)
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: self.now[0])
        self.runtime.mesh_ready = True
        self.runtime.session()
        self.capture = self.runtime.diagnostics.start_group(GROUP, MEMBERS, seconds=30)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def exported(self):
        return self.runtime.diagnostics.export(self.capture['id'])

    def receive(self, frame, *, after_ms=0):
        self.now[0] += after_ms
        self.runtime.observe(frame, self.now[0])

    def assert_local_attempt(self, record, *, dispatched, accepted, duration):
        self.assertTrue(record['local_tx'])
        self.assertFalse(record['authenticated_rx'])
        self.assertIs(record['dispatch_started'], dispatched)
        self.assertIs(record['transport_accepted'], accepted)
        self.assertFalse(record['remote_acknowledged'])
        self.assertEqual(record['begun_ms'], 1000)
        self.assertEqual(record['ended_ms'], 1000 + duration)
        self.assertEqual(record['duration_ms'], duration)
        self.assertEqual(record['source'], 0x8000)
        self.assertEqual(record['address'], 0)
        self.assertEqual(record['payload'], GROUP_ON[3:].hex())
        self.assertEqual(record['wire_length'], len(GROUP_ON) + 14)
        self.assertEqual(record['transport_trailer'], 255)
        self.assertFalse(record['trailer_authenticated'])
        self.assertFalse(record['trailer_may_be_forwarded'])

    def test_invalid_authentication_never_enters_group_capture(self):
        valid = encrypt_mcp(GROUP_ON, KEY, 7, CONTROLLER)
        damaged = bytearray(valid)
        damaged[-2] ^= 1  # Change the authenticator, not the unauthenticated trailer.
        damaged[-1] = 42
        self.receive(bytes(damaged))
        result = self.exported()
        self.assertEqual(result['received'], 0)
        self.assertEqual(result['records'], [])
        self.assertEqual(self.runtime.receive_count, 0)
        self.assertEqual(self.runtime.core.counts['invalid_frame'], 1)
        self.assertEqual(self.transport.frames, [])

    def test_authenticated_native_duplicates_keep_wire_metadata_without_refreshing_state(self):
        original = encrypt_mcp(GROUP_ON, KEY, 7, CONTROLLER)
        relayed = original[:-1] + b'\x07'
        self.assertEqual(decrypt_mcp(original, KEY), decrypt_mcp(relayed, KEY))
        self.receive(original)
        self.receive(relayed, after_ms=5)
        result = self.exported()
        self.assertEqual((result['format'], result['scope'], result['group']), (3, 'group', GROUP))
        self.assertEqual(result['members'], MEMBERS)
        self.assertEqual(result['received'], 2)
        self.assertEqual(len(result['records']), 2)
        for index, (record, trailer) in enumerate(zip(result['records'], (255, 7)), 1):
            self.assertEqual(record['capture_index'], index)
            self.assertEqual(record['sequence'], 7)
            self.assertEqual(record['source'], CONTROLLER)
            self.assertEqual(record['address'], 0)
            self.assertEqual(record['payload'], GROUP_ON[3:].hex())
            self.assertEqual(record['wire_length'], len(original))
            self.assertEqual(record['transport_trailer'], trailer)
            self.assertTrue(record['authenticated_rx'])
            self.assertFalse(record['local_tx'])
            self.assertFalse(record['trailer_authenticated'])
            self.assertTrue(record['trailer_may_be_forwarded'])
            self.assertFalse(record['remote_acknowledged'])
            self.assertIsNone(record['transport_accepted'])
        # Diagnostics retains relay duplicates; normal controller state still
        # uses the first authenticated observation and does not gain freshness.
        self.assertEqual(self.runtime.native_observations[CONTROLLER][1], 1000)
        self.assertEqual(self.runtime.controller_events[CONTROLLER][1], 1000)
        self.assertTrue(result['authenticated_rx_only'])
        self.assertFalse(result['mesh_key_included'])
        self.assertFalse(result['masp_included'])
        self.assertEqual(self.transport.frames, [])

    def test_member_report_includes_authenticated_wire_metadata_and_excludes_nonmember(self):
        report = bytes.fromhex('008073010a0080')
        member = encrypt_mcp(report, KEY, 9, MEMBERS[0])[:-1] + b'\x12'
        outsider = encrypt_mcp(report, KEY, 10, OUTSIDE)[:-1] + b'\x34'
        self.receive(member)
        self.receive(outsider, after_ms=1)
        records = self.exported()['records']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['source'], MEMBERS[0])
        self.assertEqual(records[0]['wire_length'], len(member))
        self.assertEqual(records[0]['transport_trailer'], 18)
        self.assertTrue(records[0]['authenticated_rx'])
        self.assertFalse(records[0]['trailer_authenticated'])
        self.assertEqual(self.transport.frames, [])

    async def test_group_send_keeps_the_single_original_wire_frame_and_sequence(self):
        sequence = self.store.get('sequence', 0) + 1
        expected = encrypt_mcp(GROUP_ON, KEY, sequence)
        await self.runtime.send(GROUP_ON)
        self.assertEqual(self.transport.frames, [expected])
        self.assertEqual(self.store.get('sequence'), sequence)
        records = self.exported()['records']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['sequence'], sequence)
        self.assert_local_attempt(records[0], dispatched=True, accepted=True, duration=7)
        self.assertFalse(self.exported()['authenticated_rx_only'])
        self.assertFalse(self.exported()['local_tx_is_delivery_proof'])
        self.assertEqual(self.runtime.diagnostics.tx_attempts, 1)
        self.assertEqual(self.runtime.diagnostics.tx_failures, 0)

    async def test_admission_refusal_records_no_dispatch_and_sends_no_radio_frame(self):
        sequence = self.store.get('sequence', 0) + 1
        admissions = []

        def refuse():
            admissions.append(self.store.get('sequence'))
            self.now[0] += 3
            raise RuntimeError('Synthetic admission refusal')

        with self.assertRaisesRegex(RuntimeError, 'Synthetic admission refusal'):
            await self.runtime.send(GROUP_ON, admission=refuse)
        self.assertEqual(admissions, [sequence], 'admission must follow durable sequence allocation')
        self.assertEqual(self.transport.frames, [])
        self.assertEqual(self.store.get('sequence'), sequence)
        records = self.exported()['records']
        self.assertEqual(len(records), 1)
        self.assert_local_attempt(records[0], dispatched=False, accepted=False, duration=3)
        self.assertEqual(self.runtime.diagnostics.tx_attempts, 1)
        self.assertEqual(self.runtime.diagnostics.tx_failures, 1)

    async def test_transport_failure_records_attempted_dispatch_without_retry(self):
        self.transport.fail = True
        sequence = self.store.get('sequence', 0) + 1
        expected = encrypt_mcp(GROUP_ON, KEY, sequence)
        with self.assertRaisesRegex(RuntimeError, 'Synthetic transport failure'):
            await self.runtime.send(GROUP_ON)
        self.assertEqual(self.transport.frames, [expected])
        self.assertEqual(self.store.get('sequence'), sequence)
        records = self.exported()['records']
        self.assertEqual(len(records), 1)
        self.assert_local_attempt(records[0], dispatched=True, accepted=False, duration=7)
        self.assertEqual(self.runtime.diagnostics.tx_attempts, 1)
        self.assertEqual(self.runtime.diagnostics.tx_failures, 1)


if __name__ == '__main__':
    unittest.main()
