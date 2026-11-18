import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi.__main__ import main, replay
from avionmesh_pi.bridge import Bridge, Device, load_devices

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
VECTORS = json.loads((FIXTURES / "vectors.json").read_text())
KEY = bytes(range(16))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge([Device(40001, 127, "Keypad", True),
                              Device(40002, 134, "A19", True),
                              Device(40003, 91, "Accessory", False)], KEY)
        self.bridge.connection(True, 0)

    def receive(self, name, now, received=None):
        return self.bridge.receive(bytes.fromhex(VECTORS[name]), now if received is None else received, now)

    def test_discovery_matches_event_topics_and_is_opt_in(self):
        discovery = self.bridge.discovery()
        self.assertEqual(len(discovery), 2)
        event = self.receive("on", 1)[0]
        self.assertEqual(discovery[0]["payload"]["state_topic"], event["topic"])
        self.assertEqual(discovery[0]["payload"]["availability_mode"], "all")
        self.assertFalse(event["retain"])
        self.assertEqual(event["qos"], 0)

    def test_duplicate_does_not_swallow_new_sequence(self):
        self.assertEqual(len(self.receive("raise", 1)), 1)
        self.assertEqual(self.receive("raise", 2), [])
        self.assertEqual(len(self.receive("raise_repeat", 3)), 1)
        self.assertEqual(self.bridge.counts["duplicate"], 1)

    def test_echo_unknown_and_unexposed_sources(self):
        for now, name in enumerate(("echo", "unknown_source", "accessory"), 1):
            self.assertEqual(self.receive(name, now), [])
        self.assertEqual(self.bridge.counts["unknown_or_unexposed_source"], 3)

    def test_invalid_authentication(self):
        corrupted = bytearray.fromhex(VECTORS["on"])
        corrupted[7] ^= 1
        self.assertEqual(self.bridge.receive(bytes(corrupted), 0, 0), [])
        self.assertEqual(self.bridge.counts["invalid_frame"], 1)

    def test_read_and_assignment_not_events(self):
        self.assertEqual(self.receive("read", 1), [])
        self.assertEqual(self.receive("assignment", 2), [])

    def test_session_freshness_and_no_reconnect_replay(self):
        self.assertEqual(self.receive("on", 2000, received=0), [])
        self.assertEqual(self.receive("on", 2000, received=2001), [])
        self.bridge.connection(False, 2100)
        self.assertEqual(self.receive("on", 2101), [])
        messages = self.bridge.connection(True, 2200)
        self.assertFalse(any(m["topic"].endswith("/event") for m in messages))
        self.assertEqual(self.receive("on", 2200, received=2199), [])
        self.assertTrue(self.receive("on", 2201))

    def test_light_reports_drive_state_and_availability(self):
        self.assertEqual(self.bridge.availability(0)[0]["payload"], "offline")
        reports = self.receive("light", 10)
        self.assertEqual(reports[0]["payload"], {"state": "ON", "brightness": 128})
        self.assertEqual(reports[1]["payload"], "online")
        self.assertEqual(self.receive("color", 20)[0]["payload"]["color_temp"], 3000)
        self.assertEqual(self.bridge.availability(120009)[0]["payload"], "online")
        self.assertEqual(self.bridge.availability(120010)[0]["payload"], "offline")
        self.assertEqual(self.receive("color_late", 120011), [])
        self.assertEqual(self.bridge.availability(120012)[0]["payload"], "offline")

    def test_disconnect_clears_state(self):
        self.receive("light", 1)
        self.bridge.connection(False, 2)
        self.bridge.connection(True, 3)
        self.assertEqual(self.bridge.availability(3)[0]["payload"], "offline")
        self.assertEqual(self.receive("color", 4), [])

    def test_no_optimistic_state_from_command_plan(self):
        planned = self.bridge.plan_command(40002, brightness=255)
        self.assertIsInstance(planned, bytes)
        self.assertEqual(self.bridge.states, {})
        for target in (40001, 40003, 40004):
            with self.assertRaises(ValueError):
                self.bridge.plan_command(target, brightness=0)
        with self.assertRaises(ValueError):
            self.bridge.plan_command(40002, brightness=0, retained=True)

    def test_no_color_temperature_for_load_dimmer(self):
        bridge = Bridge([Device(40002, 97, "Dimmer", True)], KEY)
        bridge.connection(True, 0)
        with self.assertRaises(ValueError):
            bridge.plan_command(40002, kelvin=3000)
        self.assertEqual(bridge.receive(bytes.fromhex(VECTORS["color"]), 1, 1), [])

    def test_model_identity_is_not_button_qualification(self):
        bridge = Bridge([Device(40003, 91, "Accessory", True), Device(40004, 126, "Battery", True),
                         Device(40005, 81, "Anyplace", True), Device(40009, 255, "Unknown", True)], KEY)
        self.assertEqual(len(bridge.discovery()), 3)
        bridge.connection(True, 0)
        for now, name in enumerate(("accessory", "battery", "anyplace"), 1):
            event = bridge.receive(bytes.fromhex(VECTORS[name]), now, now)[0]["payload"]
            self.assertNotIn("button", event)
            self.assertNotIn("gesture", event)

    def test_dedup_is_time_bounded_not_permanent_antireplay(self):
        self.receive("on", 0)
        self.assertEqual(self.receive("on", 4999), [])
        self.assertTrue(self.receive("on", 5000))

    def test_invalid_inventory_and_namespace(self):
        for record in ({"passphrase": "DO_NOT_ECHO"},
                       {"device_id": True, "product_id": 127, "name": "Invalid"},
                       {"device_id": 32768, "product_id": 127, "name": "Host"},
                       {"device_id": 40001, "product_id": 127, "name": "Bad", "exposed": 1}):
            with self.assertRaises(ValueError):
                load_devices([record])
        item = {"device_id": 40001, "product_id": 127, "name": "Duplicate"}
        with self.assertRaises(ValueError):
            load_devices([item, item])
        with self.assertRaises(ValueError):
            Bridge([], KEY, node="bad/#")

    def test_monotonic_clock(self):
        self.bridge.availability(10)
        with self.assertRaises(ValueError):
            self.bridge.availability(9)


class ReplayTests(unittest.TestCase):
    def test_service_refuses_without_explicit_live_flag(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["serve", "/nonexistent-config"]), 2)

    def test_end_to_end_without_sockets(self):
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            rows = list(replay(FIXTURES / "session.jsonl"))
        events = [r for r in rows if r.get("topic", "").endswith("/event")]
        self.assertEqual([r["payload"]["event_type"] for r in events],
                         ["on", "raise", "raise", "lower", "off", "mesh_command"])
        self.assertTrue(all(not r["retain"] for r in events))
        self.assertEqual(rows[-1]["counters"], {
            "controller_events": 6, "duplicate": 1, "ignored_packet": 2,
            "invalid_frame": 1, "light_reports": 2, "not_ready_or_stale": 3,
            "unknown_or_unexposed_source": 3})
        self.assertNotIn(KEY.hex(), json.dumps(rows))

    def test_cli_bad_input_excludes_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "invalid.jsonl"
            fixture.write_text('{"passphrase":"DO_NOT_ECHO"}\n')
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(main(["replay", str(fixture)]), 2)
            self.assertNotIn("DO_NOT_ECHO", out.getvalue() + err.getvalue())

    def test_empty_and_nonobject_fixtures_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "invalid.jsonl"
            for text in ("", "[]\n", "null\n"):
                fixture.write_text(text)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["replay", str(fixture)]), 2)


if __name__ == "__main__":
    unittest.main()
