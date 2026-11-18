"""Synthetic examples only: candidate framing is not hardware qualification."""
import unittest
from avionmesh_pi import scene_protocol as scene
from avionmesh_pi.protocol import Packet


class SceneProtocolTests(unittest.TestCase):
    def test_full_colour_and_brightness_rows_have_distinct_candidate_selectors(self):
        colour = scene.candidate_colour_insert(40002, 7, 4000, 91)
        brightness = scene.candidate_brightness_insert(40002, 7, 128, 92)
        self.assertEqual(colour.hex(), '429c73022200005b07020fa000')
        self.assertEqual(brightness.hex(), '429c73022200005c2780000000')
        self.assertEqual(scene.candidate_delete(40002, 7, 93).hex(), '429c73052200005d0700000000')
        self.assertEqual(scene.candidate_recall(7).hex(), '00007300220007000701000000')

    def test_commands_do_not_mutate_transport_or_allocate_sequences(self):
        self.assertFalse(scene.PROGRAMMING_SUPPORTED)
        for function, args in [(scene.candidate_colour_insert, (40002,7,4000,91)),
                               (scene.candidate_brightness_insert, (40002,7,128,92)),
                               (scene.candidate_delete, (40002,7,93))]:
            self.assertEqual(function(*args), function(*args))
            self.assertEqual(len(function(*args)), 13)

    def test_reject_invalid_ranges_booleans_and_broadcast_writes(self):
        for target in (0, 291, 32768, 32895, 65408, True, '40002'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                scene.candidate_delete(target, 7, 1)
        for reference in (0, -1, 21, 32, 255, True, '7'):
            for function, args in [(scene.candidate_delete, (40002, reference, 1)),
                                   (scene.candidate_recall, (reference,))]:
                with self.subTest(reference=reference), self.assertRaises(ValueError): function(*args)
        for token in (0, 256, True, '1'):
            with self.assertRaises(ValueError): scene.candidate_delete(40002, 7, token)
        for brightness in (-1,256,True,'128'):
            with self.assertRaises(ValueError): scene.candidate_brightness_insert(40002,7,brightness,1)
        for kelvin in (2699,5001,True,'4000'):
            with self.assertRaises(ValueError): scene.candidate_colour_insert(40002,7,kelvin,1)

    def describe(self, payload, address=40002, source=32768, sequence=7):
        return scene.describe_scene_message(Packet(sequence,source,address,115,bytes.fromhex(payload)))

    def test_colour_and_brightness_classification_retains_candidate_status(self):
        colour = self.describe('022200005b07020fa000')
        brightness = self.describe('022200005c2780000000')
        self.assertEqual(colour['kelvin_candidate'], 4000)
        self.assertEqual(brightness['brightness_candidate'], 128)
        self.assertEqual(brightness['selector_raw'],0x27)
        self.assertFalse(brightness['selector_bank_verified'])
        self.assertFalse(colour['semantics_verified'])
        self.assertNotIn('scene_id',colour)

    def test_unknown_ack_codes_never_indicate_success(self):
        for verb in (2,5):
            for result in (0,1,5,255):
                row=self.describe(bytes([verb,34,91,result]).hex(),32768,40002)
                self.assertEqual(row['kind'],'scene_mutation_reply_candidate')
                self.assertEqual(row['result_raw'],result)
                self.assertEqual(row['result_meaning'],'unknown')
                self.assertFalse(row['semantics_verified'])
                self.assertNotIn('success',row)

    def test_delete_scope_and_recall_flags_remain_raw(self):
        row=self.describe('052200005d0700000000')
        self.assertEqual(row['deletion_extent'],'unknown')
        for flag in (1,3):
            row=self.describe(f'002200070007{flag:02x}000000',0)
            self.assertEqual(row['kind'],'scene_trigger_candidate')
            self.assertEqual(row['flags_raw'],flag)
            self.assertNotIn('activation',row)

    def test_changed_or_unbounded_layouts_are_not_misdecoded(self):
        for payload in ('022200005b07030fa000','022200005b07020fa001',
                        '022200005c2780000001','022200005c5580000000',
                        '052200005d0701000000','00220007000704000000'):
            with self.subTest(payload=payload):
                self.assertEqual(self.describe(payload)['kind'],'unclassified_scene_message')
        self.assertEqual(self.describe('022200005c2780000000',0)['kind'],'unclassified_scene_message')

    def test_local_tx_unknown_sequence_is_not_forged(self):
        row=self.describe('022200005c2780000000',sequence=None)
        self.assertEqual(row['kind'],'scene_brightness_insert_candidate')
        self.assertFalse(row['semantics_verified'])
        self.assertNotIn('sequence',row)

    def test_unrelated_invalid_payloads_and_identity(self):
        self.assertIsNone(self.describe('000a0000008000000000'))
        for payload in ('0022'+'00'*24,):
            with self.assertRaises(ValueError):self.describe(payload)
        with self.assertRaises(ValueError):self.describe('05225d01',source=True)
        with self.assertRaises(ValueError):scene.describe_scene_message(Packet(None,40002,0,115,'0022'))


    def test_explicit_keypad_profile_matches_captured_recall_and_keeps_default(self):
        self.assertEqual(scene.candidate_recall(2, profile='keypad_capture').hex(),
                         '00007300220002000203000000')
        self.assertEqual(scene.candidate_recall(3, profile='keypad_capture').hex(),
                         '00007300220003000303000000')
        self.assertEqual(scene.candidate_recall(2).hex(), '00007300220002000201000000')
        for reference in range(1, 21):
            normal = scene.candidate_recall(reference)
            alternative = scene.candidate_recall(reference, profile='keypad_capture')
            self.assertEqual([i for i,(a,b) in enumerate(zip(normal,alternative)) if a!=b], [9])
            self.assertEqual(len(alternative), 13)

    def test_unknown_recall_profile_or_reference_cannot_encode(self):
        for profile in (None, True, 3, [], {}, 'fast', 'raw', '03'):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                scene.candidate_recall(2, profile=profile)
        for reference in (0, 21, True, '2'):
            with self.assertRaises(ValueError):
                scene.candidate_recall(reference, profile='keypad_capture')

if __name__ == '__main__': unittest.main()
