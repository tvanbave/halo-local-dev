import hashlib
import json
from pathlib import Path
import unittest

from avionmesh_pi.protocol import (Packet, decode_controller, decrypt_mcp,
                                  derive_mcp_key, plan_light_command)
from avionmesh_pi.transport import FragmentAssembler

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
VECTORS = json.loads((FIXTURES / "vectors.json").read_text())
KEY = bytes(range(16))


class CryptoTests(unittest.TestCase):
    def test_openssl_known_answer(self):
        packet = decrypt_mcp(bytes.fromhex(VECTORS["on"]), KEY)
        self.assertEqual(packet, Packet(1, 40001, 32768, 115,
                                       bytes.fromhex("000a012300ff00000000")))

    def test_all_authenticated_single_byte_tampering_rejected(self):
        original = bytes.fromhex(VECTORS["on"])
        for offset in range(len(original)-1):
            with self.subTest(offset=offset):
                altered = bytearray(original)
                altered[offset] ^= 1
                with self.assertRaises(ValueError):
                    decrypt_mcp(bytes(altered), KEY)

    def test_mutable_trailer_preserves_authenticated_packet(self):
        frame = bytes.fromhex(VECTORS['on'])
        expected = decrypt_mcp(frame, KEY)
        for trailer in (0,1,54,55,254,255):
            self.assertEqual(decrypt_mcp(frame[:-1]+bytes([trailer]),KEY),expected)

    def test_wrong_key_and_malformed_envelopes(self):
        frame = bytes.fromhex(VECTORS["on"])
        for bad in (b"", frame[:16], frame + bytes(20), frame[:-1]):
            with self.assertRaises(ValueError):
                decrypt_mcp(bad, KEY)
        for key in (bytes(16), bytes(15), bytes(17)):
            with self.assertRaises(ValueError):
                decrypt_mcp(frame, key)

    def test_key_derivation_contract(self):
        expected = hashlib.sha256(b"synthetic passphrase\0MCP").digest()[-16:][::-1]
        self.assertEqual(derive_mcp_key("synthetic passphrase"), expected)
        for value in ("", "a\0b", "x" * 253, None):
            with self.assertRaises(ValueError):
                derive_mcp_key(value)

    def test_controller_vectors(self):
        for name, event in (("on", "on"), ("raise", "raise"), ("lower", "lower"),
                            ("off", "off"), ("unknown", "mesh_command")):
            with self.subTest(name=name):
                decoded = decode_controller(decrypt_mcp(bytes.fromhex(VECTORS[name]), KEY))
                self.assertEqual(decoded["event_type"], event)
                self.assertEqual(decoded["target"], 291)
                self.assertNotIn("button", decoded)

    def test_queries_and_assignments_are_not_events(self):
        for name in ("read", "assignment"):
            self.assertIsNone(decode_controller(decrypt_mcp(bytes.fromhex(VECTORS[name]), KEY)))

    def test_native_accessory_layout_is_product_scoped(self):
        packet = Packet(1, 40003, 0, 115, bytes.fromhex("000a0123010080000000"))
        self.assertEqual(decode_controller(packet, 91)["brightness"], 128)
        for product in (None, 81, 126, 127):
            self.assertEqual(decode_controller(packet, product)["event_type"], "mesh_command")

    def test_unknown_layout_and_short_packets(self):
        for payload in (b"", b"\0", bytes.fromhex("01300000")):
            self.assertIsNone(decode_controller(Packet(1, 40001, 0, 115, payload)))
        for payload in (bytes.fromhex("000a"), bytes.fromhex("110a0123006300000000")):
            self.assertEqual(decode_controller(Packet(1, 40001, 0, 115, payload))["event_type"], "mesh_command")

    def test_plaintext_command_vectors(self):
        self.assertEqual(plan_light_command(40002, brightness=128).hex(), "429c73000a0000008000000000")
        self.assertEqual(plan_light_command(291, brightness=255).hex(), "000073000a012300ff00000000")
        self.assertEqual(plan_light_command(40002, kelvin=3000).hex(), "429c73001d000000010bb80000")
        # First device address is 0x8080, not the host's 0x8000.
        self.assertEqual(plan_light_command(32896, brightness=0)[:2], b"\x80\x80")

    def test_invalid_commands(self):
        for target in (0, -1, 65536, True, 32768, 32895):
            with self.assertRaises(ValueError):
                plan_light_command(target, brightness=0)
        for args in ({}, {"brightness": -1}, {"brightness": True}, {"brightness": 256},
                     {"kelvin": 2699}, {"kelvin": 5001}, {"brightness": 0, "kelvin": 3000}):
            with self.assertRaises(ValueError):
                plan_light_command(40002, **args)


class FragmentTests(unittest.TestCase):
    def test_low_high_and_standalone(self):
        frame = bytes.fromhex(VECTORS["on"])
        assembler = FragmentAssembler()
        self.assertIsNone(assembler.feed("low", frame[:20], 0))
        assembled = assembler.feed("high", frame[20:], 199)
        self.assertEqual(decrypt_mcp(assembled, KEY).crypto_source, 40001)
        self.assertEqual(assembler.feed("high", b"one", 200), b"one")

    def test_timeout_and_reconnect_do_not_splice(self):
        for reset in (False, True):
            assembler = FragmentAssembler()
            assembler.feed("low", b"first", 0)
            if reset:
                assembler.reset()
            self.assertEqual(assembler.feed("high", b"last", 200), b"last")
            self.assertEqual(assembler.dropped, 1)

    def test_replaced_low_is_counted(self):
        assembler = FragmentAssembler()
        assembler.feed("low", b"old", 0)
        assembler.feed("low", b"new", 1)
        self.assertEqual(assembler.feed("high", b"end", 2), b"newend")
        self.assertEqual(assembler.dropped, 1)

    def test_invalid_notifications(self):
        for ch, value in (("invalid", b"a"), ("high", b""), ("low", bytes(21))):
            with self.assertRaises(ValueError):
                FragmentAssembler().feed(ch, value, 0)
        assembler = FragmentAssembler()
        assembler.feed("low", b"a", 2)
        with self.assertRaises(ValueError):
            assembler.feed("high", b"b", 1)

if __name__ == "__main__":
    unittest.main()
