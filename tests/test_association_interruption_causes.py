"""Synthetic cause isolation, not a reconstruction of an installation capture.

Several distinct faults have the same three-submission public-key receipt.
These tests deliberately do not infer a historical cause from that receipt.
"""
import asyncio
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.association import envelope, open_envelope
from avionmesh_pi.commissioning import ClaimValidationError, Commissioning
from avionmesh_pi.runtime import Runtime
from avionmesh_pi.transport import FragmentAssembler
from support import synthetic_store as Store
from test_association import ClaimTransport, IDENTITY, KEY


class InterruptedTransport(ClaimTransport):
    def __init__(self, fault):
        super().__init__()
        self.fault = fault
        self.claim_requests = []

    async def send(self, frame):
        try:
            payload = open_envelope(frame)
        except ValueError:
            return await super().send(frame)
        opcode = payload[0]
        if opcode != 0:
            self.claim_requests.append((opcode, payload[5] if opcode == 4 else None))
        if opcode != 4 or payload[5] != 1:
            return await super().send(frame)

        self.sent += 1
        reply = self.peer.respond(frame)
        if self.fault == "silent_peer":
            return
        if self.fault == "invalid_authentication":
            reply = bytes([reply[0] ^ 1]) + reply[1:]
        elif self.fault == "foreign_target":
            decoded = open_envelope(reply)
            reply = envelope(decoded[:1] + bytes([decoded[1] ^ 1]) + decoded[2:])
        elif self.fault == "duplicate_previous_chunk":
            decoded = open_envelope(reply)
            reply = envelope(decoded[:5] + b"\0" + decoded[6:])
        elif self.fault == "transport_offline":
            self.ready = False
            return
        elif self.fault == "transport_offline_during_wait":
            asyncio.get_running_loop().call_soon(setattr, self, "ready", False)
            return
        elif self.fault == "session_change_during_wait":
            asyncio.get_running_loop().call_soon(self.runtime.session)
            return
        elif self.fault in ("unrelated_burst", "foreign_target_burst", "invalid_authentication_burst"):
            # Model background notifications draining while send is in flight.
            # The valid expected reply follows 64 unrelated discovery packets.
            for counter in range(64):
                announcement = envelope(b"\x01" + bytes([counter + 32]) * 16)
                if self.fault == "foreign_target_burst":
                    decoded = open_envelope(reply)
                    announcement = envelope(decoded[:1] + bytes([decoded[1] ^ 1]) + decoded[2:])
                elif self.fault == "invalid_authentication_burst":
                    announcement = bytes([reply[0] ^ 1]) + reply[1:]
                self.runtime.observe(announcement, 10)
        elif self.fault == "conflicting_target_burst":
            # Genuine ambiguity: 64 DISTINCT authenticated chunk-1 replies for
            # this exact target precede the valid one. Fail closed.
            decoded = open_envelope(reply)
            for counter in range(64):
                self.runtime.observe(envelope(decoded[:6] + bytes([counter, 0xA5]) * 4 + decoded[14:]), 10)
        elif self.fault == "duplicate_reply_burst":
            # Mesh echo: the same valid reply delivered many times.
            for _ in range(64):
                self.runtime.observe(reply, 10)
        elif self.fault == "stale_chunk_burst":
            # Late repeats of the already-processed chunk-0 reply.
            decoded = open_envelope(reply)
            stale = envelope(decoded[:5] + b"\0" + decoded[6:])
            for _ in range(64):
                self.runtime.observe(stale, 10)
        elif self.fault == "expired_fragment":
            assembler = FragmentAssembler()
            self.assertion_low_result = assembler.feed("low", reply[:20], 10)
            reply = assembler.feed("high", reply[20:], 210)
            self.fragment_dropped = assembler.dropped
        elif self.fault != "none":
            raise AssertionError("Unknown synthetic fault")
        self.runtime.observe(reply, 10)


class AssociationInterruptionCauseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(self.temporary.name, KEY)

    async def asyncTearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def setup_claim(self, fault):
        self.transport = InterruptedTransport(fault)
        self.runtime = Runtime(self.store, KEY, self.transport, clock=lambda: 10)
        self.transport.runtime = self.runtime
        self.runtime.mesh_ready = True
        self.claims = Commissioning(self.runtime)

    async def assert_same_public_key_receipt(self, fault):
        self.setup_claim(fault)
        await self.claims.discover(seconds=0)
        real_wait_for = asyncio.wait_for
        deadlines = []

        async def accelerated_deadline(awaitable, timeout):
            deadlines.append(timeout)
            # Exercise the real queue wait/cancellation path without spending
            # five wall-clock seconds per deliberately unanswered request.
            return await real_wait_for(awaitable, .01 if timeout == 5 else timeout)

        with patch("avionmesh_pi.commissioning.asyncio.wait_for", accelerated_deadline):
            with self.assertRaises((asyncio.TimeoutError, RuntimeError)):
                await self.claims.claim(IDENTITY.hex(), "Synthetic interrupted light", 134,
                                        inventory_complete=True)
        receipt = self.store.journals()[0]
        detail = receipt["detail"]
        self.assertEqual(receipt["status"], "uncertain")
        self.assertEqual(detail["association_phase"], "public_key")
        self.assertEqual(detail["submissions_attempted"], 3)
        self.assertEqual(detail["submissions_accepted"], 3)
        timing=detail['association_timing']
        self.assertEqual(len(timing['steps']),3)
        self.assertEqual(timing['steps'][-1]['state'],'public_key')
        self.assertEqual(timing['steps'][-1]['chunk'],1)
        if fault != 'conflicting_target_burst':
            self.assertNotIn('reply_observed_ms',timing['steps'][-1])
        for step in timing['steps']:
            self.assertLessEqual(set(step),{'state','chunk','started_ms','write_started_ms','write_finished_ms','reply_observed_ms','reply_processed_ms'})
        self.assertTrue(all(type(v) is int and v>=0 for v in timing['transport_delta'].values()))
        self.assertFalse(detail["association_complete"])
        self.assertTrue(detail["inventory_complete"])
        self.assertEqual(self.transport.claim_requests, [(2, None), (4, 0), (4, 1)])
        self.assertFalse(self.store.inventory()["devices"])
        self.assertFalse(self.transport.ready)
        self.assertIn(5, deadlines)
        self.assertIn(60, deadlines)
        expected_code = {
            "transport_offline": "association_transport_unavailable",
            "transport_offline_during_wait": "association_transport_unavailable",
            "session_change_during_wait": "association_session_changed",
            "conflicting_target_burst": "association_receive_overflow",
        }.get(fault, "association_response_timeout")
        self.assertEqual(detail.get("failure_code"), expected_code)
        observation = detail["failure_observation"]
        self.assertEqual(set(observation), {
            "state", "expected_chunk", "same_session", "transport_ready",
            "queue_overflow", "ignored_frames", "out_of_step_frames",
            "duplicate_frames", "queued_target_frames",
        })
        self.assertEqual(observation["state"], "public_key")
        self.assertEqual(observation["expected_chunk"], 1)
        self.assertIs(type(observation["expected_chunk"]), int)
        for field in ("same_session", "transport_ready", "queue_overflow"):
            self.assertIs(type(observation[field]), bool)
        for field in ("ignored_frames", "out_of_step_frames", "duplicate_frames", "queued_target_frames"):
            self.assertIs(type(observation[field]), int)
            self.assertGreaterEqual(observation[field], 0)
            self.assertLessEqual(observation[field], 65535)
        self.assertEqual(observation["same_session"], fault != "session_change_during_wait")
        self.assertEqual(observation["queue_overflow"], fault == "conflicting_target_burst")
        if fault == "conflicting_target_burst":
            self.assertGreaterEqual(observation["queued_target_frames"], 64)
        if fault == "duplicate_previous_chunk":
            # Stale chunk replies are discarded before the queue; still a timeout.
            self.assertEqual(observation["out_of_step_frames"], 1)
            # Only the association and chunk-0 replies were queued.
            self.assertEqual(observation["queued_target_frames"], 2)

    async def test_missing_reply_reproduces_three_submission_public_key_interruption(self):
        await self.assert_same_public_key_receipt("silent_peer")

    async def test_invalid_authentication_has_same_historical_receipt_signature(self):
        await self.assert_same_public_key_receipt("invalid_authentication")

    async def test_foreign_target_has_same_historical_receipt_signature(self):
        await self.assert_same_public_key_receipt("foreign_target")

    async def test_duplicate_chunk_has_same_historical_receipt_signature(self):
        await self.assert_same_public_key_receipt("duplicate_previous_chunk")

    async def test_transport_loss_has_same_historical_receipt_signature(self):
        await self.assert_same_public_key_receipt("transport_offline")

    async def test_transport_loss_while_waiting_is_distinguished_from_response_timeout(self):
        await self.assert_same_public_key_receipt("transport_offline_during_wait")

    async def test_session_change_while_waiting_is_distinguished_from_response_timeout(self):
        await self.assert_same_public_key_receipt("session_change_during_wait")

    async def test_genuine_target_receive_overflow_still_fails_closed(self):
        await self.assert_same_public_key_receipt("conflicting_target_burst")

    async def test_expired_low_fragment_has_same_historical_receipt_signature(self):
        await self.assert_same_public_key_receipt("expired_fragment")
        self.assertIsNone(self.transport.assertion_low_result)
        self.assertEqual(self.transport.fragment_dropped, 1)

    async def test_expected_chunk_response_completes_without_fault(self):
        await self.assert_successful_claim("none")

    async def test_unrelated_receive_burst_cannot_overflow_claim_queue(self):
        await self.assert_successful_claim("unrelated_burst")

    async def test_foreign_target_receive_burst_cannot_overflow_claim_queue(self):
        await self.assert_successful_claim("foreign_target_burst")

    async def test_invalid_authentication_burst_cannot_overflow_claim_queue(self):
        await self.assert_successful_claim("invalid_authentication_burst")

    async def test_repeated_valid_reply_burst_cannot_overflow_claim_queue(self):
        await self.assert_successful_claim("duplicate_reply_burst")

    async def test_stale_previous_chunk_burst_cannot_overflow_claim_queue(self):
        await self.assert_successful_claim("stale_chunk_burst")

    async def assert_successful_claim(self, fault):
        self.setup_claim(fault)
        await self.claims.discover(seconds=0)
        result = await self.claims.claim(IDENTITY.hex(), "Synthetic healthy light", 134,
                                         inventory_complete=True)
        self.assertTrue(result["associated"])
        self.assertEqual(self.store.journals()[0]["status"], "verified")
        self.assertEqual(len(self.transport.claim_requests), 12)
        timing=self.store.journals()[0]['detail']['association_timing']
        self.assertEqual(len(timing['steps']),12)
        self.assertTrue(all('reply_observed_ms' in step for step in timing['steps']))

    async def test_missing_manual_scan_without_refresh_fails_before_receipt_or_submission(self):
        self.setup_claim("none")
        await self.claims.discover(seconds=0)
        self.runtime.session()  # A previous successful claim invalidates discovery.
        with self.assertRaises(ClaimValidationError):
            await self.claims.claim(IDENTITY.hex(), "Synthetic stale candidate", 134,
                                    inventory_complete=True)
        self.assertFalse(self.store.journals())
        self.assertFalse(self.transport.claim_requests)

    async def test_server_refresh_path_does_not_need_a_second_manual_scan(self):
        self.setup_claim("none")
        await self.claims.discover(seconds=0)
        self.runtime.session()
        real_discover = self.claims.discover

        async def instant_synthetic_discovery():
            return await real_discover(seconds=0)

        with patch.object(self.claims, "discover", instant_synthetic_discovery):
            result = await self.claims.claim(IDENTITY.hex(), "Synthetic auto refresh", 134,
                                             inventory_complete=True, refresh_identity=True)
        self.assertTrue(result["associated"])
        self.assertEqual(self.store.journals()[0]["status"], "verified")
