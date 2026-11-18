"""Synthetic GATT submission diagnostics; no Bluetooth or credentials."""
import asyncio
import unittest
from avionmesh_pi.bluez import BlueZTransport, LOW, HIGH


class Client:
    is_connected = True
    def __init__(self, fail=None):
        self.fail = fail
        self.writes = []
    async def write_gatt_char(self, char, data, response):
        self.writes.append(char)
        if char == self.fail:
            raise RuntimeError('synthetic submission failure')
    async def disconnect(self):
        self.is_connected = False


class SubmissionTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, fail=None):
        transport = BlueZTransport('00:11:22:33:44:55')
        client = Client(fail)
        transport.client = client
        transport.ready = True
        return transport, client

    async def test_success_means_submission_not_acknowledgement(self):
        transport, client = self.transport()
        await transport.send(bytes(27))
        self.assertEqual(client.writes, [LOW, HIGH])
        self.assertEqual(dict(transport.counts), {
            'tx_low_attempted': 1, 'tx_low_submitted': 1,
            'tx_high_attempted': 1, 'tx_high_submitted': 1})

    async def test_low_failure_does_not_send_high_or_retry(self):
        transport, client = self.transport(LOW)
        with self.assertRaises(RuntimeError):
            await transport.send(bytes(27))
        self.assertEqual(client.writes, [LOW])
        self.assertEqual(dict(transport.counts), {'tx_low_attempted': 1, 'tx_low_failed': 1})
        self.assertFalse(transport.ready)

    async def test_high_failure_keeps_partial_submission_visible(self):
        transport, client = self.transport(HIGH)
        with self.assertRaises(RuntimeError):
            await transport.send(bytes(27))
        self.assertEqual(client.writes, [LOW, HIGH])
        self.assertEqual(dict(transport.counts), {
            'tx_low_attempted': 1, 'tx_low_submitted': 1,
            'tx_high_attempted': 1, 'tx_high_failed': 1})
        with self.assertRaises(RuntimeError):
            await transport.send(bytes(27))
        self.assertEqual(client.writes, [LOW, HIGH])

    async def test_single_fragment_failure_is_not_reported_submitted(self):
        transport, client = self.transport(HIGH)
        with self.assertRaises(RuntimeError):
            await transport.send(bytes(17))
        self.assertEqual(client.writes, [HIGH])
        self.assertEqual(dict(transport.counts), {'tx_high_attempted': 1, 'tx_high_failed': 1})

    async def test_cancellation_records_uncertainty_and_disconnects(self):
        transport, client = self.transport()
        started = asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        client.write_gatt_char = blocked
        task = asyncio.create_task(transport.send(bytes(17)))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(dict(transport.counts), {'tx_high_attempted': 1, 'tx_high_failed': 1})
        self.assertFalse(transport.ready)
        self.assertFalse(client.is_connected)
