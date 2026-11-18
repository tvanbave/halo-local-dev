"""Bleak/BlueZ GATT adapter. Importing or constructing it performs no I/O."""
import asyncio
import time
from collections import Counter

from .transport import FragmentAssembler

SERVICE = "0000fef1-0000-1000-8000-00805f9b34fb"
LOW = "c4edc000-9daf-11e3-8003-00025b000b00"
HIGH = "c4edc000-9daf-11e3-8004-00025b000b00"
NOTIFY_TIMEOUT = 5
DISCONNECT_TIMEOUT = 5


class BlueZTransport:
    def __init__(self, address, adapter="hci0", *, scanner=None, client_factory=None, clock=None):
        self.address = address.upper()
        self.adapter = adapter
        self.scanner = scanner
        self.client_factory = client_factory
        self.clock = clock or (lambda: int(time.monotonic() * 1000))
        self.client = None
        self.ready = False
        self.queue = asyncio.Queue(maxsize=64)
        self.closed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.assembler = FragmentAssembler()
        self.dropped = 0
        self.counts = Counter()
        self._low_timer = None

    async def connect(self):
        if self.client is not None:
            if self.client.is_connected:
                raise RuntimeError("Previous Bluetooth connection has not closed")
            self.client = None
        if self.scanner is None:
            from bleak import BleakScanner, BleakClient
            self.scanner, self.client_factory = BleakScanner, BleakClient
        self.closed.clear()
        self._cancel_low_timer()
        self.assembler.reset()
        while not self.queue.empty():
            self.queue.get_nowait()
        device = await self.scanner.find_device_by_address(self.address, timeout=10, bluez={"adapter": self.adapter})
        if device is None:
            raise RuntimeError("Configured relay was not found")
        self.client = self.client_factory(device, disconnected_callback=self._disconnected, bluez={"adapter": self.adapter})
        try:
            await asyncio.wait_for(self.client.connect(), 20)
            for characteristic, name in ((LOW, "low"), (HIGH, "high")):
                if self.client.services.get_characteristic(characteristic) is None:
                    raise RuntimeError("Relay is missing CSRMesh characteristics")
                try:
                    await asyncio.wait_for(self.client.start_notify(
                        characteristic, self._callback(name, self.client)), NOTIFY_TIMEOUT)
                except BaseException:
                    self.counts['notification_setup_failed'] += 1
                    raise
            if not self.client.is_connected:
                raise RuntimeError("Relay disconnected during subscription")
            self.ready = True
        except BaseException:
            await self.disconnect()
            raise

    def _disconnected(self, client):
        if client is not self.client:
            return
        self.ready = False
        self.closed.set()
        self._cancel_low_timer()
        self.assembler.reset()

    def _cancel_low_timer(self):
        if self._low_timer is not None:
            self._low_timer.cancel()
            self._low_timer = None

    def _receive_failure(self, error):
        self.counts['invalid_notification' if isinstance(error, ValueError) else 'queue_drop'] += 1
        self.dropped += 1
        self.ready = False
        self.closed.set()
        self._cancel_low_timer()
        self.assembler.reset()

    def _deliver(self, frame, observed_at):
        self.queue.put_nowait((frame, observed_at))
        self.counts['completed_frames'] += 1

    def _flush_low(self, now):
        pending = self.assembler.pending
        frame = self.assembler.poll(now)
        if frame is not None:
            self.counts['expired_low'] += 1
            self.counts['standalone_low'] += 1
            # Waiting for HIGH must not make old evidence appear freshly heard.
            self._deliver(frame, pending[0])

    def _poll_low(self, owner, pending):
        if (owner is not self.client or not owner.is_connected or self.closed.is_set()
                or self.assembler.pending is not pending):
            return
        self._low_timer = None
        try:
            self._flush_low(self.clock())
            if self.assembler.pending is pending:
                # Timer callbacks may run slightly early relative to the ms clock.
                self._schedule_low(owner)
        except (ValueError, asyncio.QueueFull) as error:
            self._receive_failure(error)

    def _schedule_low(self, owner):
        pending = self.assembler.pending
        if pending is not None:
            delay = max(1, 200 - (self.clock() - pending[0])) / 1000
            self._low_timer = asyncio.get_running_loop().call_later(delay, self._poll_low, owner, pending)

    def _callback(self, name, owner):
        def notify(characteristic, data):
            if owner is not self.client or not owner.is_connected or self.closed.is_set():
                return
            now = self.clock()
            self.counts[name] += 1
            self._cancel_low_timer()
            try:
                # A delayed event-loop tick cannot splice expired LOW into HIGH.
                self._flush_low(now)
                pending = self.assembler.pending
                if name == 'low' and pending:
                    self.counts['replaced_low'] += 1
                if name == 'high':
                    self.counts['paired_high' if pending else 'standalone_high'] += 1
                    if pending and now > pending[0]:
                        self.counts['max_fragment_gap_ms'] = max(
                            self.counts['max_fragment_gap_ms'], now - pending[0])
                frame = self.assembler.feed(name, bytes(data), now)
                if frame is not None:
                    self._deliver(frame, now)
                self._schedule_low(owner)
            except (ValueError, asyncio.QueueFull) as error:
                self._receive_failure(error)
        return notify

    async def send(self, frame):
        if not 1 <= len(frame) <= 40:
            raise ValueError("Invalid mesh frame size")
        async with self.lock:
            if not self.ready or not self.client or not self.client.is_connected:
                raise RuntimeError("Bluetooth path is offline")
            client = self.client
            try:
                if len(frame) > 20:
                    await self._write_fragment(client, LOW, frame[:20], "low")
                    if self.client is not client or not self.ready or not client.is_connected:
                        raise RuntimeError("Bluetooth session changed during fragmented write")
                    await self._write_fragment(client, HIGH, frame[20:], "high")
                else:
                    await self._write_fragment(client, HIGH, frame, "high")
            except BaseException:
                # A failed second write is uncertain. Never resend either fragment.
                if self.client is client:
                    await self.disconnect()
                raise

    async def _write_fragment(self, client, characteristic, payload, name):
        # Submission success is not a device acknowledgement. Keep payloads and
        # keys out of diagnostics; an exception leaves delivery uncertain.
        self.counts[f"tx_{name}_attempted"] += 1
        try:
            await asyncio.wait_for(client.write_gatt_char(characteristic, payload, response=False), 5)
        except BaseException:
            self.counts[f"tx_{name}_failed"] += 1
            raise
        self.counts[f"tx_{name}_submitted"] += 1

    async def disconnect(self):
        self.ready = False
        self.closed.set()
        self._cancel_low_timer()
        self.assembler.reset()
        client = self.client
        if client:
            try:
                await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT)
            except Exception:
                self.counts['disconnect_failed'] += 1
            # A disconnect request/timeout is not confirmation. Retain ownership
            # while connected so connect() cannot create an overlapping session.
            if not client.is_connected and self.client is client:
                self.client = None
