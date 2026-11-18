"""Offline recovery comparison; all state and credentials are synthetic."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi import backup
from avionmesh_pi.__main__ import main
from support import synthetic_store


class RecoveryPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        store = synthetic_store(self.state, bytes(range(16)))
        self.addCleanup(store.close)
        store.allocate_sequence()
        store.set('future_metadata', {'secret': 'synthetic-recovery-secret'})
        store.save_binding(40001, {'slots': [{'target': 300}], 'secret': 'synthetic-binding-secret'})
        receipt = store.begin('synthetic', 40001, {'secret': 'synthetic-journal-secret'})
        store.transition(receipt, 'prepared', 'attempted')
        store.transition(receipt, 'attempted', 'verified')
        self.receipt = receipt
        self.identity = store.get('state_id')
        self.mesh = store.get('mesh_fingerprint')
        store.close()
        self.database = self.state / 'bridge.sqlite3'
        self.candidate = self.snapshot('candidate')

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, name):
        path = self.root / (name + '.avionbackup')
        backup._create_backup(self.state, path)
        return path

    def sql(self, statement, parameters=()):
        with sqlite3.connect(self.database) as db:
            db.execute(statement, parameters)

    def metadata(self, key, value):
        self.sql('INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)', (key, json.dumps(value)))

    def plan(self, reference=None):
        return backup._recovery_plan(self.candidate, reference)

    def error(self, code, operation):
        with self.assertRaises(backup.BackupError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)

    def test_equal_distinct_snapshots_are_comparable_but_never_authorize_recovery(self):
        reference = self.snapshot('reference')
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.database, self.candidate, reference)}
        result = self.plan(reference)
        self.assertEqual(result['compatibility'], 'matching_records')
        self.assertTrue(result['comparison']['records_equal'])
        for field in ('authenticity_verified', 'history_verified', 'reference_currency_verified',
                      'receipt_semantics_verified', 'restore_supported', 'automatic_start'):
            self.assertIs(result[field], False)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('independent_complete_tx_history_required', result['blockers'])
        for path, expected in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), expected)

    def test_missing_reference_still_produces_actionable_blocked_plan(self):
        result = self.plan()
        self.assertEqual(result['compatibility'], 'not_compared')
        self.assertIsNone(result['comparison'])
        self.assertIn('current_state_reference_unavailable', result['blockers'])

    def test_current_sequence_does_not_make_old_candidate_safe(self):
        self.metadata('sequence', 25)
        result = self.plan(self.snapshot('reference'))
        self.assertEqual(result['comparison']['sequence_relation'], 'lower')
        self.assertIn('candidate_sequence_below_reference', result['blockers'])
        self.assertEqual(result['comparison']['differences']['metadata']['changed'], 1)
        self.assertNotIn('recommended_sequence', result)

    def test_higher_candidate_sequence_does_not_prove_reference_currency(self):
        self.metadata('sequence', 0)
        result = self.plan(self.snapshot('reference'))
        self.assertEqual(result['comparison']['sequence_relation'], 'higher')
        self.assertFalse(result['reference_currency_verified'])
        self.assertIn('state_records_differ_do_not_overwrite', result['blockers'])

    def test_new_reservations_retirements_inventory_and_unknown_metadata_are_detected(self):
        for key, value in [('known_targets', [301]), ('retired_addresses', [40004]),
                           ('inventory', {'devices': [], 'groups': []}),
                           ('new_private_schema', {'credential': 'another-private-secret'})]:
            self.metadata(key, value)
        result = self.plan(self.snapshot('reference'))
        self.assertEqual(result['compatibility'], 'state_diverged')
        self.assertGreaterEqual(result['comparison']['differences']['metadata']['missing_from_candidate'], 3)
        self.assertTrue(result['comparison']['preservation_changes']['address_reservations'])
        self.assertTrue(result['comparison']['preservation_changes']['retired_addresses'])
        self.assertNotIn('another-private-secret', json.dumps(result))

    def test_missing_changed_and_candidate_only_receipts_are_not_merged(self):
        self.sql('UPDATE journals SET status=? WHERE id=?', ('uncertain', self.receipt))
        self.sql('INSERT INTO journals VALUES (?,?,?,?,?)', ('new-private-id', 'claim', 40004, 'attempted', '{}'))
        self.sql('DELETE FROM bindings')
        result = self.plan(self.snapshot('reference'))
        differences = result['comparison']['differences']
        self.assertEqual(differences['journals'], {'changed': 1, 'missing_from_candidate': 1, 'only_in_candidate': 0})
        self.assertEqual(differences['bindings']['only_in_candidate'], 1)
        self.assertEqual(result['inputs']['reference']['unresolved_operation_records'], 2)
        self.assertIn('reference_unresolved_operations_require_review', result['blockers'])

    def test_candidate_unresolved_operations_are_blockers_even_if_reference_matches(self):
        self.sql('UPDATE journals SET status=?', ('prepared',))
        self.candidate = self.snapshot('pending')
        result = self.plan(self.snapshot('reference'))
        self.assertEqual(result['compatibility'], 'matching_records')
        self.assertIn('candidate_unresolved_operations_require_review', result['blockers'])

    def test_retirement_presence_cannot_be_overridden_even_by_null(self):
        for value in ({'retired': True}, None, False):
            with self.subTest(value=value):
                self.metadata('tx_retirement', value)
                reference = self.snapshot('retirement-' + str(value))
                result = self.plan(reference)
                self.assertIn('reference_transmitter_retirement_must_be_preserved', result['blockers'])

    def test_admission_missing_and_sequence_exhaustion(self):
        self.sql('DELETE FROM metadata WHERE key=?', ('tx_admission',))
        self.metadata('sequence', 0xffffff)
        result = self.plan(self.snapshot('reference'))
        self.assertIn('reference_tx_admission_missing', result['blockers'])
        self.assertIn('reference_sequence_exhausted', result['blockers'])

    def test_different_mesh_and_lineage_are_explicitly_blocked(self):
        self.metadata('mesh_fingerprint', '1' * 64)
        self.metadata('state_id', '2' * 32)
        result = self.plan(self.snapshot('reference'))
        self.assertEqual(result['compatibility'], 'different_mesh')
        self.assertFalse(result['comparison']['same_mesh'])
        self.assertEqual(result['comparison']['sequence_relation'], 'not_comparable')
        self.assertFalse(result['comparison']['same_state_lineage'])
        self.assertIn('mesh_mismatch', result['blockers'])
        self.assertIn('state_lineage_mismatch', result['blockers'])

    def test_same_mesh_new_lineage_does_not_claim_continuity(self):
        self.metadata('state_id', '2' * 32)
        result = self.plan(self.snapshot('reference'))
        self.assertEqual(result['compatibility'], 'different_state_lineage')

    def test_same_file_or_hardlink_is_not_a_current_state_reference(self):
        self.error('invalid_reference', lambda: self.plan(self.candidate))
        alias = self.root / 'alias.avionbackup'
        os.link(self.candidate, alias)
        self.error('invalid_reference', lambda: self.plan(alias))

    def test_private_values_and_identity_do_not_escape_public_worker(self):
        result = backup.recovery_plan(self.candidate, self.snapshot('reference'))
        rendered = json.dumps(result)
        for value in (self.identity, self.mesh, self.receipt, 'synthetic-recovery-secret',
                      'synthetic-journal-secret', 'synthetic-binding-secret', str(self.root)):
            self.assertNotIn(value, rendered)

    def test_cli_reports_blocked_plan_with_successful_inspection_exit(self):
        output = io.StringIO()
        with patch('sys.stdout', output):
            code = main(['state-recovery-plan', str(self.candidate), '--reference', str(self.snapshot('reference'))])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'blocked')

    def test_cli_error_never_prints_path_or_payload(self):
        bad = self.root / 'synthetic-private-filename'
        bad.write_bytes(b'private-payload')
        bad.chmod(0o600)
        output = io.StringIO()
        with patch('sys.stderr', output):
            code = main(['state-recovery-plan', str(bad)])
        self.assertEqual(code, 2)
        self.assertNotIn(str(bad), output.getvalue())
        self.assertNotIn('private-payload', output.getvalue())

    def test_symlink_public_permissions_and_corruption_refused(self):
        alias = self.root / 'symlink'
        alias.symlink_to(self.candidate)
        with self.assertRaises(backup.BackupError):
            self.plan(alias)
        reference = self.snapshot('reference')
        reference.chmod(0o644)
        with self.assertRaises(backup.BackupError):
            self.plan(reference)
        reference.chmod(0o600)
        data = bytearray(reference.read_bytes())
        data[-1] ^= 1
        reference.write_bytes(data)
        self.error('hash_mismatch', lambda: self.plan(reference))

    def test_candidate_change_during_reference_inspection_is_detected(self):
        reference = self.snapshot('reference')
        inspect = backup._inspect_backup
        def changed(path, **kwargs):
            result = inspect(path, **kwargs)
            if path == reference:
                self.candidate.write_bytes(self.candidate.read_bytes() + b'changed')
            return result
        with patch.object(backup, '_inspect_backup', side_effect=changed):
            self.error('source_changed', lambda: self.plan(reference))

    def test_reference_replacement_is_detected(self):
        reference = self.snapshot('reference')
        inspect = backup._inspect_backup
        def changed(path, **kwargs):
            result = inspect(path, **kwargs)
            if path == reference:
                replacement = self.root / 'replacement'
                shutil.copyfile(reference, replacement)
                replacement.chmod(0o600)
                os.replace(replacement, reference)
            return result
        with patch.object(backup, '_inspect_backup', side_effect=changed):
            self.error('source_changed', lambda: self.plan(reference))

    def test_duplicate_journal_identifier_cannot_be_hidden_by_comparison_map(self):
        with sqlite3.connect(self.database) as db:
            db.execute('ALTER TABLE journals RENAME TO previous_journals')
            db.execute('CREATE TABLE journals (id TEXT, kind TEXT, target INTEGER, status TEXT, detail TEXT)')
            db.execute('INSERT INTO journals SELECT * FROM previous_journals')
            db.execute('INSERT INTO journals SELECT * FROM previous_journals')
            db.execute('DROP TABLE previous_journals')
        reference = self.snapshot('reference')
        self.error('invalid_state', lambda: self.plan(reference))

    def test_worker_timeout_fails_without_publication_or_restore_claim(self):
        with patch.object(backup.subprocess, 'run', side_effect=subprocess.TimeoutExpired('worker', 180)):
            self.error('inspection_failed', lambda: backup.recovery_plan(self.candidate))

    def test_operational_database_is_rejected_without_modifying_it(self):
        before = hashlib.sha256(self.database.read_bytes()).digest()
        with self.assertRaises(backup.BackupError):
            backup.recovery_plan(self.database)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).digest(), before)

    def test_comparison_opens_only_immutable_staged_sqlite(self):
        reference = self.snapshot('reference')
        connect = backup.sqlite3.connect
        opened = []
        def checked(path, **kwargs):
            opened.append(path)
            self.assertIn('.avionmesh-backup-', path)
            self.assertTrue(path.endswith('?mode=ro&immutable=1'))
            self.assertNotIn(str(self.database), path)
            return connect(path, **kwargs)
        with patch.object(backup.sqlite3, 'connect', side_effect=checked):
            self.plan(reference)
        self.assertEqual(len(opened), 2)


if __name__ == '__main__':
    unittest.main()
