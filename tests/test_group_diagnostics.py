"""Bounded group diagnostics are passive and contain documented MCP data only."""
import unittest

from avionmesh_pi.diagnostics import Diagnostics
from avionmesh_pi.protocol import Packet, decode_light, model_command, plan_light_command


class GroupDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.d = Diagnostics(lambda: self.now)
        self.started = self.d.start_group(258, [40001,40002], seconds=10)

    def records(self):
        return self.d.export(self.started['id'])['records']

    def native(self, *, group=258, source=40100, brightness=255, kelvin=None, address=0):
        command = plan_light_command(group, **({'kelvin':kelvin} if kelvin is not None else {'brightness':brightness}))
        return Packet(123,source,address,115,command[3:])

    def tx(self, command, **kwargs):
        self.d.transmitted(command,124,1000,kwargs.pop('accepted',True),
                           session=self.d.session,owner=self.started['id'],**kwargs)

    def test_scope_members_are_copied_and_device_capture_remains_compatible(self):
        self.assertEqual(self.started['scope'],'group')
        self.assertEqual(self.started['target'],258)
        self.assertEqual(self.started['group'],258)
        self.started['members'].append(40003)
        self.assertEqual(self.d.export(self.started['id'])['members'],[40001,40002])
        self.assertEqual(self.d.export(self.started['id'])['format'],3)
        self.d.release(self.started['id'])
        d = self.d.start(40100,seconds=1)
        self.assertNotIn('scope',d)
        self.d.observe(self.native(group=259),1000)
        result = self.d.export(d['id'])
        self.assertEqual(result['format'],2)
        self.assertEqual(len(result['records']),1)

    def test_invalid_group_duration_and_member_lists_are_rejected(self):
        for group,members,seconds in (
                (0,[40001],10),(255,[40001],10),(24576,[40001],10),(24566,[40001],10),
                (True,[40001],10),(258,[],10),(258,[40001]*2,10),(258,[True],10),
                (258,[32768],10),(258,[65408],10),(258,range(40001,40003),10),
                (258,list(range(40001,40514)),10),(258,[40001],0),(258,[40001],61),
                (258,[40001],True)):
            with self.subTest(group=group,members=str(members)[:30],seconds=seconds):
                with self.assertRaises(ValueError):
                    Diagnostics(lambda:1000).start_group(group,members,seconds)

    def test_native_group_commands_from_any_authenticated_sender_and_duplicates_are_kept(self):
        for source in (40100,40101,32768):
            for packet in (self.native(source=source),self.native(source=source,brightness=0),
                           self.native(source=source,kelvin=4000)):
                self.d.observe(packet,1000,wire_length=27,trailer=54)
                self.d.observe(packet,1000,wire_length=27,trailer=53)
        rows = self.records()
        self.assertEqual(len(rows),18)
        self.assertEqual({r['source'] for r in rows},{40100,40101,32768})
        self.assertTrue(all(r['authenticated_rx'] and not r['local_tx'] for r in rows))
        self.assertTrue(all(r['wire_length']==27 and not r['trailer_authenticated']
                            and r['trailer_may_be_forwarded'] for r in rows))
        self.assertEqual([r['transport_trailer'] for r in rows[:2]],[54,53])
        self.assertEqual([r['capture_index'] for r in rows],list(range(1,19)))

    def test_known_relative_native_group_controls_are_allowed_but_unknown_steps_are_not(self):
        for delta in (13,243,1):
            payload = bytes([17,10,1,2,0,delta,0,0,0,0])
            self.d.observe(Packet(1,40100,0,115,payload),1000)
        self.assertEqual(len(self.records()),2)

    def test_wrong_group_address_opcode_noun_or_command_layout_is_excluded(self):
        packets = [self.native(group=259),self.native(address=40001)]
        good = self.native().payload
        packets += [Packet(1,40100,0,114,good),Packet(1,40100,0,115,b'\x00\x1b'+good[2:])]
        for payload in (good[:-1],good+b'\0',b'\x01'+good[1:],
                        good[:4]+b'\x01'+good[5:],good[:6]+b'\x01'+good[7:],
                        good[:9]+b'\x01',bytes.fromhex('001d0102000010000000'),
                        bytes.fromhex('001d0102000100010000')):
            packets.append(Packet(1,40100,0,115,payload))
        for packet in packets:
            self.d.observe(packet,1000)
        self.assertEqual(self.records(),[])

    def test_local_group_setters_and_member_gets_are_included_only_for_selected_scope(self):
        self.tx(plan_light_command(258,brightness=0))
        self.tx(plan_light_command(258,kelvin=3500))
        self.tx(model_command(40001,1,10))
        self.tx(model_command(40002,1,29))
        for command in (plan_light_command(259,brightness=255),plan_light_command(40001,brightness=255),
                        model_command(40003,1,10),model_command(40001,2,3),model_command(258,1,10),
                        model_command(40001,1,10,b'\x01\0\0')):
            self.tx(command)
        rows = self.records()
        self.assertEqual(len(rows),4)
        self.assertEqual([r['address'] for r in rows],[0,0,40001,40002])
        self.assertTrue(all(r['local_tx'] and not r['authenticated_rx'] for r in rows))
        self.assertTrue(all(r['dispatch_started'] is None for r in rows))

    def test_exact_member_reports_allow_reads_and_unsolicited_reports(self):
        for source in (40001,40002):
            for verb in (1,11):
                for payload in (bytes([verb,10,0,128]),bytes([verb,29,0,1,0x0f,0xa0])):
                    self.d.observe(Packet(1,source,32768,115,payload),1000,
                                   wire_length=len(payload)+17,trailer=255)
        self.assertEqual(len(self.records()),8)

    def test_foreign_or_malformed_member_reports_and_assignment_data_are_excluded(self):
        good = b'\x01\x0a\0\xff'
        for packet in (Packet(1,40003,32768,115,good),Packet(1,40001,40002,115,good),
                       Packet(1,40001,32768,115,good+b'\0'),Packet(1,40001,32768,115,good[:-1]),
                       Packet(1,40001,32768,115,b'\x01\x1d\0\2\x0f\xa0'),
                       Packet(1,40001,32768,115,b'\x01\x1d\0\1\0\1'),
                       Packet(1,40001,32768,115,b'\x00\x1b\1\1'),
                       Packet(1,40001,32768,4,good)):
            self.d.observe(packet,1000)
        self.assertEqual(self.records(),[])

    def test_ct_reports_in_both_supported_modes_match_read_light_decoding(self):
        # Existing local read fixtures use mode0; other report fixtures use mode1.
        for payload in ('011d00000dac','011d00000fa0','011d00010dac','011d00010fa0'):
            packet = Packet(1,40001,32768,115,bytes.fromhex(payload))
            self.assertIn(decode_light(packet)['color_temp'],(3500,4000))
            self.d.observe(packet,1000,wire_length=23,trailer=54)
        self.assertEqual([r['payload'] for r in self.records()],
                         ['011d00000dac','011d00000fa0','011d00010dac','011d00010fa0'])

    def test_tx_timing_dispatch_rejection_and_transport_uncertainty_are_distinct(self):
        command = plan_light_command(258,brightness=255)
        self.now = 1010
        self.tx(command,accepted=False,dispatch_started=False,ended=1005,wire_length=27,trailer=255)
        self.tx(command,accepted=False,dispatch_started=True,ended=1008,wire_length=27,trailer=255)
        self.tx(command,accepted=True,dispatch_started=True,ended=1009,wire_length=27,trailer=255)
        rows = self.records()
        self.assertEqual([r['dispatch_started'] for r in rows],[False,True,True])
        self.assertEqual([r['transport_accepted'] for r in rows],[False,False,True])
        self.assertEqual([r['duration_ms'] for r in rows],[5,8,9])
        self.assertTrue(all(r['begun_ms']==1000 and not r['remote_acknowledged'] for r in rows))
        self.assertTrue(all(not r['trailer_authenticated'] and not r['trailer_may_be_forwarded'] for r in rows))
        self.assertEqual(self.d.summary()['tx_failures'],2)
        self.assertEqual(self.d.summary()['tx_samples'][-1]['ended_ms'],1009)

    def test_optional_metadata_is_validated_without_raising_or_logging_objects(self):
        self.d.observe(self.native(),1000,wire_length={'secret':'never logged'},trailer=b'raw bytes')
        self.tx(plan_light_command(258,brightness=255),wire_length=40,trailer=True,
                dispatch_started='unknown string',ended={'secret':'never logged'})
        rows = self.records()
        self.assertEqual(len(rows),2)
        self.assertTrue(all('wire_length' not in r and 'transport_trailer' not in r for r in rows))
        self.assertIsNone(rows[1]['dispatch_started'])
        self.assertNotIn('secret',str(self.d.export(self.started['id'])))
        self.assertNotIn('raw bytes',str(self.d.export(self.started['id'])))

    def test_capture_owner_session_and_time_boundaries_preserve_scope(self):
        with self.assertRaises(RuntimeError):
            self.d.start_group(259,[40001],1)
        with self.assertRaises(ValueError):
            self.d.export('wrong')
        command = plan_light_command(258,brightness=255)
        for owner,session in ((None,self.d.session),('wrong',self.d.session),(self.started['id'],99)):
            self.d.transmitted(command,1,1000,True,owner=owner,session=session)
        self.d.observe(self.native(),999)
        self.assertEqual(self.records(),[])
        self.d.boundary()
        self.d.observe(self.native(),1000)
        self.tx(command)
        self.assertEqual(self.records(),[])
        self.assertEqual(self.d.export(self.started['id'])['stopped'],'session_changed')

    def test_expiry_prevents_late_rx_or_tx_and_requires_export_before_replacement(self):
        self.now = 11000
        self.d.observe(self.native(),1000)
        self.tx(plan_light_command(258,brightness=255))
        with self.assertRaises(RuntimeError):
            self.d.start_group(259,[40001],1)
        self.assertEqual(self.records(),[])
        self.d.start_group(259,[40001],1)

    def test_size_limit_counts_drops_and_stopped_capture_remains_immutable_by_copy(self):
        for _ in range(260):
            self.d.observe(self.native(),1000)
        result = self.d.export(self.started['id'],stop=True)
        self.assertEqual((len(result['records']),result['received'],result['dropped']),(256,260,4))
        self.assertEqual(result['truncated'],0)
        self.assertFalse(result['mesh_key_included'])
        self.assertFalse(result['masp_included'])
        result['records'].clear()
        result['members'].append(40003)
        self.d.observe(self.native(),1000)
        self.assertEqual(len(self.records()),256)
        self.assertEqual(self.d.export(self.started['id'])['members'],[40001,40002])
        self.d.release(self.started['id'])
        self.assertIsNone(self.d.capture)


if __name__ == '__main__':
    unittest.main()
