"""Synthetic process/owner shutdown tests; no Bluetooth, broker or network."""
import asyncio
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from avionmesh_pi.service import _protected_shutdown, _shutdown_service


class Owner:
    def __init__(self, events, name, *, blocked=False, failed=False):
        self.events, self.name = events, name
        self.blocked, self.failed = blocked, failed

    async def cleanup(self):
        self.events.append(self.name)
        if self.failed:
            raise RuntimeError('synthetic secret must not enter logs')
        try:
            if self.blocked:
                await asyncio.Event().wait()
        finally:
            self.events.append(self.name + '_finished')

    close = cleanup
    disconnect = cleanup


class Store:
    def __init__(self, events):
        self.events = events

    def close(self):
        self.events.append('store_closed')


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_http_and_failed_mqtt_do_not_skip_disconnect_or_close(self):
        events = []
        with patch('avionmesh_pi.service.SHUTDOWN_TIMEOUTS',
                   {stage: .02 for stage in ('http', 'workers', 'mqtt', 'bluetooth')}):
            with self.assertLogs('avionmesh_pi.service', level='WARNING') as logs:
                await _shutdown_service(Owner(events, 'http', blocked=True), [],
                    Owner(events, 'mqtt', failed=True), Owner(events, 'bluetooth'), Store(events))
        self.assertEqual(events, ['http', 'http_finished', 'mqtt', 'bluetooth',
                                  'bluetooth_finished', 'store_closed'])
        self.assertIn('service_shutdown_http_timeout', str(logs.output))
        self.assertIn('service_shutdown_mqtt_failed', str(logs.output))
        self.assertNotIn('secret', str(logs.output))

    async def test_worker_uncertainty_cleanup_precedes_store_close(self):
        events = []
        entered = asyncio.Event()
        async def writer():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append('write_uncertain')
        worker = asyncio.create_task(writer())
        await entered.wait()
        await _shutdown_service(None, [worker], None, Owner(events, 'bluetooth'), Store(events))
        self.assertEqual(events, ['write_uncertain', 'bluetooth', 'bluetooth_finished', 'store_closed'])
        self.assertTrue(worker.cancelled())

    async def test_worker_hanging_during_first_cancellation_is_cancelled_again(self):
        events, entered = [], asyncio.Event()
        async def writer():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append('write_uncertain')
                try:
                    await asyncio.Event().wait()
                finally:
                    events.append('worker_finished')
        worker = asyncio.create_task(writer())
        await entered.wait()
        with patch('avionmesh_pi.service.SHUTDOWN_TIMEOUTS',
                   {stage: .02 for stage in ('http', 'workers', 'mqtt', 'bluetooth')}):
            with self.assertLogs('avionmesh_pi.service', level='WARNING') as logs:
                await _shutdown_service(None, [worker], None, Owner(events, 'bluetooth'), Store(events))
        self.assertEqual(events, ['write_uncertain', 'worker_finished', 'bluetooth',
                                  'bluetooth_finished', 'store_closed'])
        self.assertTrue(worker.cancelled())
        self.assertIn('service_shutdown_workers_timeout', str(logs.output))

    async def test_repeated_cancellation_does_not_interrupt_disconnect(self):
        events, entered, release = [], asyncio.Event(), asyncio.Event()
        class Transport:
            async def disconnect(self):
                entered.set()
                await release.wait()
                events.append('disconnected')
        task = asyncio.create_task(_protected_shutdown(None, [], None, Transport(), Store(events)))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertEqual(events, [])
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(events, ['disconnected', 'store_closed'])

    async def test_primary_error_is_not_replaced_by_cleanup_failure(self):
        events = []
        async def failing():
            try:
                raise ValueError('primary startup failure')
            finally:
                await _protected_shutdown(Owner(events, 'http', failed=True), [], None,
                                          Owner(events, 'bluetooth', failed=True), Store(events))
        with self.assertLogs('avionmesh_pi.service', level='WARNING'):
            with self.assertRaisesRegex(ValueError, 'primary startup failure'):
                await failing()
        self.assertEqual(events[-1], 'store_closed')

    async def test_store_close_failure_does_not_replace_primary_error(self):
        class FailedStore:
            def close(self):
                raise RuntimeError('synthetic private close error')
        async def failing():
            try:
                raise ValueError('primary startup failure')
            finally:
                await _protected_shutdown(None, [], None, None, FailedStore())
        with self.assertLogs('avionmesh_pi.service', level='WARNING') as logs:
            with self.assertRaisesRegex(ValueError, 'primary startup failure'):
                await failing()
        self.assertNotIn('private', str(logs.output))
        with self.assertRaisesRegex(RuntimeError, 'close error'):
            await _protected_shutdown(None, [], None, None, FailedStore())


@unittest.skipUnless(os.name == 'posix', 'POSIX service signal handling')
class ProcessSignalTests(unittest.TestCase):
    def test_real_serve_entrypoint_handles_repeated_sigterm_and_cleans_up(self):
        # Patch only the service body/config loader. Execute the real main()
        # command dispatch and installed signal wrapper in a separate process.
        script = '''
import asyncio
from unittest.mock import patch
from avionmesh_pi.__main__ import main
async def fake_service(config):
    print('READY', flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        print('CLEANING', flush=True)
        await asyncio.sleep(.15)
        print('CLOSED', flush=True)
with patch('avionmesh_pi.service.load_config', return_value={}), patch('avionmesh_pi.service.serve', fake_service):
    raise SystemExit(main(['serve', '/synthetic/config.json', '--allow-live']))
'''
        process = subprocess.Popen([sys.executable, '-u', '-c', script],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(5), 'service did not start')
                self.assertEqual(process.stdout.readline().strip(), 'READY')
                process.send_signal(signal.SIGTERM)
                self.assertTrue(selector.select(5), 'service did not begin cleanup')
                self.assertEqual(process.stdout.readline().strip(), 'CLEANING')
                process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), 'CLOSED')
            self.assertEqual(stderr, '')
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
