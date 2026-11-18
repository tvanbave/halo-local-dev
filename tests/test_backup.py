"""Private, temporary synthetic state only. No network, hardware or actual state."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import select
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi import backup
from avionmesh_pi.__main__ import main
from avionmesh_pi.migration import retire
from avionmesh_pi.storage import Store
from support import synthetic_store

KEY = bytes(range(16))

CHILD = r'''
import os,sqlite3,sys,time
from pathlib import Path
from avionmesh_pi import backup
from avionmesh_pi.storage import Store
state,destination,mode=sys.argv[1:]
if mode=='bridge':
    store=Store(state,bytes(range(16)))
elif mode=='sqlite':
    db=sqlite3.connect(str(Path(state)/'bridge.sqlite3'))
    db.execute('BEGIN IMMEDIATE')
elif mode in ('before_publish','after_publish'):
    original=backup.os.link
    def boundary(*args,**kwargs):
        if mode=='after_publish': original(*args,**kwargs)
        print('checkpoint',flush=True)
        time.sleep(60)
    backup.os.link=boundary
    backup._create_backup(state,destination)
print('checkpoint',flush=True)
time.sleep(60)
'''


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS /var and /tmp are symlinks. The API intentionally requires the
        # actual no-symlink path; do not relax the check for test convenience.
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        self.output = self.root / 'private.avionbackup'
        self.store = synthetic_store(self.state, KEY)
        self.store.allocate_sequence()
        self.store.set('known_targets', [256, 300, 777])
        self.store.set('retired_addresses', [40007])
        self.store.set('retired_claims', ['retired-synthetic-claim'])
        self.store.set('future_private_metadata', {'credential': 'synthetic-credential-only', 'nested': [1, 2]})
        self.store.set('discovery_tombstones', ['synthetic/tombstone'])
        self.store.save_binding(40001, {'slots': [{'target': 300}], 'key': 'synthetic-binding-key'})
        self.receipt = self.store.begin('claim', 40003, {'token': 'synthetic-receipt-secret'})
        self.store.transition(self.receipt, 'prepared', 'attempted')
        self.identity = self.store.get('state_id')
        self.fingerprint = self.store.get('mesh_fingerprint')
        self.store.close()
        self.database = self.state / 'bridge.sqlite3'
        self.original = self.database.read_bytes()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def create(self):
        # Private worker core enables deterministic syscall fault injection.
        # Public API isolation is independently covered in test_backup_review.
        return backup._create_backup(self.state, self.output)

    def assert_error(self, code, operation):
        with self.assertRaises(backup.BackupError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)
        return str(raised.exception)

    def unpack(self):
        raw = self.output.read_bytes()
        size = struct.unpack('>I', raw[len(backup.MAGIC):len(backup.MAGIC) + 4])[0]
        start = len(backup.MAGIC) + 4
        return json.loads(raw[start:start + size]), raw[start + size:]

    def replace(self, manifest, payload):
        raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
        self.output.write_bytes(backup.MAGIC + struct.pack('>I', len(raw)) + raw + payload)

    def source_unchanged(self):
        self.assertEqual(self.database.read_bytes(), self.original)
        self.assertEqual({p.name for p in self.state.iterdir()}, {'bridge.sqlite3', 'bridge.lock'})

    @contextlib.contextmanager
    def child(self, mode):
        process = subprocess.Popen([sys.executable, '-c', CHILD, str(self.state), str(self.output), mode],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 10)[0], 'Synthetic child did not reach checkpoint')
            self.assertEqual(process.stdout.readline().strip(), 'checkpoint')
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
            process.stdout.close()
            process.stderr.close()

    def test_full_database_preserved_without_restart_mutations(self):
        before = self.database.stat()
        report = self.create()
        manifest, payload = self.unpack()
        self.assertEqual(payload, self.original)
        self.assertEqual(manifest['snapshot']['state_id'], self.identity)
        self.assertEqual(manifest['snapshot']['mesh_fingerprint'], self.fingerprint)
        self.assertEqual(manifest['payload']['sha256'], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(report['summary']['sequence'], 1)
        self.assertEqual(report['summary']['journal_statuses']['attempted'], 1)
        self.assertEqual(report['summary']['journal_statuses']['uncertain'], 0)
        self.assertEqual(report['summary']['binding_records'], 1)
        self.assertFalse(report['restore_supported'])
        self.assertFalse(report['automatic_start'])
        self.assertFalse(report['history_verified'])
        self.assertFalse(report['authenticity_verified'])
        self.assertEqual(report, backup.inspect_backup(self.output))
        self.assertEqual(backup._signature(before), backup._signature(self.database.stat()))
        self.source_unchanged()
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertFalse(list(self.root.glob('.avionmesh-backup-*')))

    def test_retirement_and_all_reservations_remain_in_snapshot(self):
        store = synthetic_store(self.state, KEY)
        retire(store)
        store.close()
        self.original = self.database.read_bytes()
        self.create()
        _, payload = self.unpack()
        self.assertEqual(payload, self.original)
        with tempfile.NamedTemporaryFile(dir=self.root) as private:
            private.write(payload)
            private.flush()
            db = sqlite3.connect(private.name)
            try:
                metadata = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM metadata')}
                self.assertTrue(metadata['tx_retirement']['retired'])
                self.assertEqual(metadata['known_targets'], [256, 300, 777])
                self.assertEqual(metadata['retired_addresses'], [40007])
                self.assertEqual(metadata['retired_claims'], ['retired-synthetic-claim'])
                self.assertIn('future_private_metadata', metadata)
            finally:
                db.close()

    def test_inspection_never_infers_newer_sequence_or_reads_source(self):
        report = self.create()
        store = synthetic_store(self.state, KEY)
        self.assertEqual(store.allocate_sequence(), 2)
        store.set('known_targets', [256, 300, 777, 999])
        store.close()
        newer = self.database.read_bytes()
        with patch('avionmesh_pi.storage.Store', side_effect=AssertionError('No state initialization')):
            self.assertEqual(backup.inspect_backup(self.output), report)
        self.assertEqual(self.database.read_bytes(), newer)
        self.assertEqual(report['summary']['sequence'], 1)
        self.assertIn('independent complete key/source TX history', report['next_step'])

    def test_public_report_and_cli_do_not_print_private_identity_or_credentials(self):
        for arguments in (['state-backup', '--state-directory', str(self.state), str(self.output)],
                          ['state-inspect', str(self.output)]):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('avionmesh_pi.storage.Store', side_effect=AssertionError('No Store')), \
                    patch('socket.socket', side_effect=AssertionError('No networking')), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(arguments), 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result['integrity_verified'])
            for secret in (self.identity, self.fingerprint, KEY.hex(), 'synthetic-credential-only',
                           'synthetic-binding-key', 'synthetic-receipt-secret'):
                self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())

    def test_public_worker_failure_reports_uncertain_creation_and_unverified_inspection(self):
        with patch.object(backup.subprocess, 'run', side_effect=subprocess.TimeoutExpired('synthetic', 180)):
            self.assert_error('publication_uncertain', lambda: backup.create_backup(self.state, self.output))
            self.assert_error('inspection_failed', lambda: backup.inspect_backup(self.output))
        self.source_unchanged()

    def test_cli_failure_is_specific_bounded_and_no_restore_command(self):
        store = Store(self.state, KEY)
        try:
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(['state-backup', '--state-directory', str(self.state), str(self.output)]), 2)
            self.assertEqual(stdout.getvalue(), '')
            self.assertIn('[active_writer]', stderr.getvalue())
            self.assertNotIn(str(self.state), stderr.getvalue())
        finally:
            store.close()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(['state-restore', str(self.output)])
        self.assertEqual(raised.exception.code, 2)

    def test_bridge_owner_in_same_and_other_process_rejected(self):
        store = Store(self.state, KEY)
        try:
            self.assert_error('active_writer', self.create)
        finally:
            store.close()
        with self.child('bridge'):
            self.assert_error('active_writer', self.create)
        self.assertFalse(self.output.exists())

    def test_uncooperative_sqlite_writer_is_rejected(self):
        with self.child('sqlite'):
            self.assert_error('active_writer', self.create)
        self.assertFalse(self.output.exists())
        self.source_unchanged()

    def test_sqlite_writer_in_callers_process_is_rejected(self):
        db = sqlite3.connect(self.database)
        try:
            db.execute('BEGIN IMMEDIATE')
            self.assert_error('active_writer', self.create)
        finally:
            db.close()
        self.source_unchanged()

    def test_rejection_preserves_existing_callers_sqlite_locks(self):
        # POSIX close(any fd to inode) silently drops this process's SQLite
        # locks. A failed backup must not open/close that inode in this process.
        probe = "import sqlite3,sys; d=sqlite3.connect(sys.argv[1],timeout=0); d.execute('BEGIN IMMEDIATE')"
        db = sqlite3.connect(self.database)
        try:
            db.execute('BEGIN IMMEDIATE')
            self.assert_error('active_writer', self.create)
            result = subprocess.run([sys.executable, '-c', probe, str(self.database)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('database is locked', result.stderr)
        finally:
            db.close()
        self.source_unchanged()

    def test_active_sqlite_reader_also_blocks_exclusive_snapshot(self):
        db = sqlite3.connect(self.database)
        try:
            db.execute('BEGIN')
            db.execute('SELECT * FROM metadata').fetchall()
            self.assert_error('active_writer', self.create)
        finally:
            db.close()
        self.source_unchanged()

    def test_sqlite_is_never_opened_against_source(self):
        connect = sqlite3.connect
        def copied_only(path, *args, **kwargs):
            self.assertNotIn(str(self.database), str(path))
            self.assertIn('mode=ro&immutable=1', str(path))
            return connect(path, *args, **kwargs)
        with patch.object(backup.sqlite3, 'connect', side_effect=copied_only):
            self.create()
        self.source_unchanged()

    def test_lock_holder_death_fails_before_publication(self):
        lock = backup._sqlite_lock
        @contextlib.contextmanager
        def lost(fd):
            with lock(fd) as (process, signature):
                process.kill()
                process.wait(timeout=5)
                yield process, signature
        with patch.object(backup, '_sqlite_lock', side_effect=lost):
            self.assert_error('lock_lost', self.create)
        self.assertFalse(self.output.exists())
        self.source_unchanged()

    def test_lock_helper_launch_failure_fails_closed(self):
        with patch.object(backup.subprocess, 'Popen', side_effect=OSError('synthetic unavailable runtime')):
            self.assert_error('backup_failed', self.create)
        self.assertFalse(self.output.exists())
        self.source_unchanged()

    def test_sqlite_lock_remains_held_through_publication(self):
        original = os.link
        script = "import sqlite3,sys; d=sqlite3.connect(sys.argv[1],timeout=0); d.execute('BEGIN IMMEDIATE')"
        def check(*args, **kwargs):
            result = subprocess.run([sys.executable, '-c', script, str(self.database)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('database is locked', result.stderr)
            return original(*args, **kwargs)
        with patch.object(backup.os, 'link', side_effect=check):
            self.create()
        self.source_unchanged()

    def test_source_missing_lock_does_not_create_one(self):
        (self.state / 'bridge.lock').unlink()
        self.assert_error('unsafe_path', self.create)
        self.assertFalse((self.state / 'bridge.lock').exists())
        self.assertFalse(self.output.exists())

    def test_source_symlink_file_lock_directory_and_ancestor_rejected(self):
        for name in ('bridge.sqlite3', 'bridge.lock'):
            path = self.state / name
            original = self.root / ('original-' + name)
            path.rename(original)
            path.symlink_to(original)
            self.assert_error('unsafe_path', self.create)
            path.unlink()
            original.rename(path)
        alias = self.root / 'alias'
        alias.symlink_to(self.state, target_is_directory=True)
        self.assert_error('unsafe_path', lambda: backup.create_backup(alias, self.output))
        parent_alias = self.root / 'parent-alias'
        parent_alias.symlink_to(self.root, target_is_directory=True)
        self.assert_error('unsafe_path', lambda: backup.create_backup(parent_alias / 'state', self.output))
        self.source_unchanged()

    def test_hardlinked_source_and_nonregular_files_rejected_without_blocking(self):
        link = self.root / 'second-database-link'
        os.link(self.database, link)
        self.assert_error('unsafe_permissions', self.create)
        link.unlink()
        path = self.state / 'bridge.lock'
        path.unlink()
        os.mkfifo(path, 0o600)
        self.assert_error('unsafe_permissions', self.create)
        path.unlink()
        path.mkdir(mode=0o700)
        self.assert_error('unsafe_path', self.create)

    def test_state_file_directory_and_lock_must_be_private(self):
        for path, bad, good in ((self.state, 0o750, 0o700), (self.database, 0o640, 0o600),
                                (self.state / 'bridge.lock', 0o644, 0o600)):
            path.chmod(bad)
            self.assert_error('unsafe_permissions', self.create)
            path.chmod(good)
        self.source_unchanged()

    def test_owner_mismatch_rejected(self):
        with patch.object(backup.os, 'geteuid', return_value=os.geteuid() + 1):
            self.assert_error('unsafe_path', self.create)

    def test_destination_parent_must_exist_and_be_private_without_symlinks(self):
        self.assert_error('unsafe_path', lambda: backup.create_backup(self.state, self.root / 'missing' / 'backup'))
        public = self.root / 'public'
        public.mkdir(mode=0o755)
        self.assert_error('unsafe_permissions', lambda: backup.create_backup(self.state, public / 'backup'))
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.assert_error('unsafe_path', lambda: backup.create_backup(self.state, alias / 'backup'))
        self.assert_error('unsafe_path', lambda: backup.create_backup(self.state, self.root / 'x' / '..' / 'backup'))

    def test_destination_cannot_be_inside_operational_state(self):
        self.assert_error('unsafe_destination', lambda: backup.create_backup(self.state, self.state / 'archive'))
        child = self.state / 'nested'
        child.mkdir(mode=0o700)
        self.assert_error('unsafe_destination', lambda: backup.create_backup(self.state, child / 'archive'))

    def test_existing_destination_file_directory_and_dangling_symlink_not_replaced(self):
        self.output.write_bytes(b'do not replace')
        self.assert_error('destination_exists', self.create)
        self.assertEqual(self.output.read_bytes(), b'do not replace')
        self.output.unlink()
        self.output.mkdir()
        self.assert_error('destination_exists', self.create)
        self.output.rmdir()
        self.output.symlink_to(self.root / 'missing-target')
        self.assert_error('destination_exists', self.create)
        self.assertTrue(self.output.is_symlink())

    def test_destination_race_cannot_overwrite_another_creator(self):
        link = os.link
        def race(*args, **kwargs):
            self.output.write_bytes(b'winner')
            return link(*args, **kwargs)
        with patch.object(backup.os, 'link', side_effect=race):
            self.assert_error('destination_exists', self.create)
        self.assertEqual(self.output.read_bytes(), b'winner')
        self.assertFalse(list(self.root.glob('.avionmesh-backup-*')))
        self.source_unchanged()

    def test_any_recovery_sidecar_even_symlink_or_empty_is_rejected(self):
        for suffix in ('-journal', '-wal', '-shm'):
            sidecar = Path(str(self.database) + suffix)
            sidecar.write_bytes(b'')
            self.assert_error('recovery_required', self.create)
            sidecar.unlink()
            sidecar.symlink_to(self.root / 'missing')
            self.assert_error('recovery_required', self.create)
            sidecar.unlink()
        self.source_unchanged()

    def test_untrusted_writable_ancestor_rejected(self):
        ancestor = self.root / 'unsafe-ancestor'
        ancestor.mkdir(mode=0o777)
        ancestor.chmod(0o777)
        private = ancestor / 'private'
        private.mkdir(mode=0o700)
        self.assert_error('unsafe_path', lambda: backup.create_backup(self.state, private / 'backup'))

    def test_malformed_and_oversized_json_state_rejected(self):
        for value in ('{"duplicate":1,"duplicate":2}', '{broken',
                      '"' + 'x' * (backup.MAX_JSON_BYTES + 1) + '"'):
            db = sqlite3.connect(self.database)
            db.execute('UPDATE metadata SET value=? WHERE key=?', (value, 'future_private_metadata'))
            db.commit()
            db.close()
            self.assert_error('invalid_state', self.create)
            self.database.write_bytes(self.original)

    def test_wal_mode_without_sidecars_is_rejected(self):
        db = sqlite3.connect(self.database)
        self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone(), ('wal',))
        db.close()
        self.assert_error('unsupported_journal', self.create)
        self.assertFalse(self.output.exists())

    def test_empty_oversized_and_non_sqlite_sources_rejected(self):
        self.database.write_bytes(b'')
        self.assert_error('invalid_size', self.create)
        with self.database.open('wb') as stream:
            stream.truncate(backup.MAX_DATABASE_BYTES + 1)
        self.assert_error('invalid_size', self.create)
        self.database.write_bytes(b'not sqlite' * 1024)
        self.assert_error('corrupt_database', self.create)
        self.assertFalse(self.output.exists())

    def test_unknown_schema_and_invalid_required_metadata_rejected(self):
        db = sqlite3.connect(self.database)
        db.execute('CREATE VIEW arbitrary AS SELECT * FROM metadata')
        db.close()
        self.assert_error('unsupported_schema', self.create)
        self.database.write_bytes(self.original)
        for key, value in (('state_id', 'bad'), ('mesh_fingerprint', 'bad'), ('sequence', True),
                           ('sequence', -1), ('sequence', 0x1000000)):
            db = sqlite3.connect(self.database)
            db.execute('UPDATE metadata SET value=? WHERE key=?', (json.dumps(value), key))
            db.commit()
            db.close()
            self.assert_error('invalid_state', self.create)
            self.database.write_bytes(self.original)

    def test_failed_fsync_before_publication_never_leaves_destination(self):
        fsync = os.fsync
        for fail_at in (1, 2):
            calls = 0
            def failure(fd):
                nonlocal calls
                calls += 1
                if calls == fail_at:
                    raise OSError('synthetic storage failure')
                return fsync(fd)
            with patch.object(backup.os, 'fsync', side_effect=failure):
                self.assert_error('backup_failed', self.create)
            self.assertFalse(self.output.exists())
            self.assertFalse(list(self.root.glob('.avionmesh-backup-*')))
            self.source_unchanged()

    def test_parent_fsync_failure_reports_uncertain_publication_not_false_absence(self):
        fsync = os.fsync
        def failure(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('synthetic directory sync failure')
            return fsync(fd)
        with patch.object(backup.os, 'fsync', side_effect=failure):
            self.assert_error('publication_uncertain', self.create)
        self.assertTrue(backup.inspect_backup(self.output)['integrity_verified'])
        self.assert_error('destination_exists', self.create)
        self.source_unchanged()

    def test_completed_archive_is_verified_before_publication(self):
        sync = backup._fsync_file
        calls = 0
        def corrupt(stream):
            nonlocal calls
            calls += 1
            sync(stream)
            if calls == 2:
                stream.seek(-1, os.SEEK_END)
                stream.write(b'!')
                stream.flush()
        with patch.object(backup, '_fsync_file', side_effect=corrupt):
            self.assert_error('hash_mismatch', self.create)
        self.assertFalse(self.output.exists())
        self.source_unchanged()

    def test_raw_copy_failure_cleans_partial_private_staging(self):
        def fail(source, output, size):
            output.write(b'partial synthetic secret')
            raise OSError('synthetic write failure')
        with patch.object(backup, '_copy', side_effect=fail):
            self.assert_error('backup_failed', self.create)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.avionmesh-backup-*')))
        self.source_unchanged()

    def test_source_replacement_during_copy_is_detected(self):
        original = backup._copy
        def replace(source, target, size):
            result = original(source, target, size)
            displaced = self.root / 'displaced.sqlite3'
            self.database.rename(displaced)
            self.database.write_bytes(self.original)
            self.database.chmod(0o600)
            return result
        with patch.object(backup, '_copy', side_effect=replace):
            self.assert_error('source_changed', self.create)
        self.assertFalse(self.output.exists())

    def test_inspection_rejects_symlinks_public_artifacts_and_fifo(self):
        self.create()
        alias = self.root / 'alias'
        alias.symlink_to(self.output)
        self.assert_error('unsafe_path', lambda: backup.inspect_backup(alias))
        self.output.chmod(0o640)
        self.assert_error('unsafe_permissions', lambda: backup.inspect_backup(self.output))
        self.output.chmod(0o600)
        alias.unlink()
        os.mkfifo(alias, 0o600)
        self.assert_error('unsafe_permissions', lambda: backup.inspect_backup(alias))

    def test_corruption_truncation_trailing_and_hash_changes_rejected(self):
        self.create()
        original = self.output.read_bytes()
        for changed in (original[:-1], original + b'x', original[:-1000]):
            self.output.write_bytes(changed)
            self.assert_error('invalid_backup', lambda: backup.inspect_backup(self.output))
        self.output.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        self.assert_error('hash_mismatch', lambda: backup.inspect_backup(self.output))
        self.output.write_bytes(b'X' + original[1:])
        self.assert_error('invalid_backup', lambda: backup.inspect_backup(self.output))

    def test_manifest_bounds_schema_duplicate_fields_and_boolean_numbers_rejected(self):
        self.create()
        manifest, payload = self.unpack()
        raw = self.output.read_bytes()
        self.output.write_bytes(backup.MAGIC + struct.pack('>I', backup.MAX_MANIFEST_BYTES + 1) + raw)
        self.assert_error('invalid_size', lambda: backup.inspect_backup(self.output))
        for key, value in (('format', True), ('restore_supported', True), ('kind', 'other'),
                           ('payload', {'bytes': True, 'sha256': '0' * 64}), ('extra', 1)):
            changed = dict(manifest)
            changed[key] = value
            self.replace(changed, payload)
            self.assert_error('invalid_backup', lambda: backup.inspect_backup(self.output))
        changed = dict(manifest)
        changed['snapshot'] = dict(manifest['snapshot'], sequence=True)
        self.replace(changed, payload)
        self.assert_error('invalid_backup', lambda: backup.inspect_backup(self.output))
        duplicate = b'{"format":1,"format":1}'
        self.output.write_bytes(backup.MAGIC + struct.pack('>I', len(duplicate)) + duplicate + payload)
        self.assert_error('invalid_state', lambda: backup.inspect_backup(self.output))

    def test_recomputed_hash_does_not_hide_invalid_sqlite_or_manifest_mismatch(self):
        self.create()
        manifest, payload = self.unpack()
        corrupt = b'broken database!' + payload[16:]
        manifest['payload']['sha256'] = hashlib.sha256(corrupt).hexdigest()
        self.replace(manifest, corrupt)
        self.assert_error('corrupt_database', lambda: backup.inspect_backup(self.output))
        manifest['payload']['sha256'] = hashlib.sha256(payload).hexdigest()
        manifest['snapshot']['sequence'] += 1
        self.replace(manifest, payload)
        self.assert_error('invalid_backup', lambda: backup.inspect_backup(self.output))

    def test_interrupted_backup_before_publication_only_leaves_private_staging(self):
        with self.child('before_publish'):
            self.assertFalse(self.output.exists())
            leftovers = list(self.root.glob('.avionmesh-backup-*'))
            self.assertTrue(leftovers)
            for directory in leftovers:
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                for path in directory.iterdir():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.source_unchanged()
        self.assertTrue(self.create()['integrity_verified'])

    def test_interrupted_backup_after_link_has_complete_verifiable_artifact(self):
        with self.child('after_publish'):
            self.assertTrue(self.output.exists())
        self.assertTrue(backup.inspect_backup(self.output)['integrity_verified'])
        self.assert_error('destination_exists', self.create)
        self.source_unchanged()


if __name__ == '__main__':
    unittest.main()
