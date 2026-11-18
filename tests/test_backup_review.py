"""Independent backup safety regressions using only disposable synthetic state."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from avionmesh_pi import backup
from support import synthetic_store


class BackupSafetyReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        store = synthetic_store(self.state, bytes(range(16)))
        store.close()
        self.database = self.state / 'bridge.sqlite3'
        self.original = self.database.read_bytes()
        self.destination = self.root / 'review.backup'

    def tearDown(self):
        if sys.platform == 'darwin':
            subprocess.run(['chmod', '-RN', str(self.root)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.temp.cleanup()

    def test_rejection_preserves_callers_posix_record_locks(self):
        probe = (
            "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR); "
            "fcntl.lockf(fd,fcntl.LOCK_EX|fcntl.LOCK_NB,512,0x40000000,os.SEEK_SET)"
        )
        fd = os.open(self.database, os.O_RDWR)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 512, 0x40000000, os.SEEK_SET)
            before = subprocess.run([sys.executable, '-I', '-c', probe, str(self.database)],
                                    capture_output=True)
            self.assertNotEqual(before.returncode, 0)
            with self.assertRaises(backup.BackupError) as raised:
                backup.create_backup(self.state, self.destination)
            self.assertEqual(raised.exception.code, 'active_writer')
            after = subprocess.run([sys.executable, '-I', '-c', probe, str(self.database)],
                                   capture_output=True)
            self.assertNotEqual(after.returncode, 0,
                                'Rejected backup released caller-owned POSIX source locks')
        finally:
            os.close(fd)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.database.read_bytes(), self.original)

    def test_parent_never_opens_source_database_descriptor(self):
        # Even opening only after helper lock acquisition is unsafe: if that
        # helper dies and a caller transaction starts, closing any source fd in
        # this process would drop the caller's new POSIX locks. All raw source
        # reads must therefore remain in the isolated child process.
        original_open = os.open

        def guard(path, *args, **kwargs):
            if os.fspath(path) in ('bridge.sqlite3', str(self.database)):
                raise AssertionError('Parent must not open/close source database')
            return original_open(path, *args, **kwargs)

        with patch.object(backup.os, 'open', side_effect=guard):
            report = backup.create_backup(self.state, self.destination)
        self.assertTrue(report['integrity_verified'])
        self.assertEqual(self.database.read_bytes(), self.original)

    def test_inspection_of_invalid_sqlite_input_preserves_caller_locks(self):
        probe = (
            "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR); "
            "fcntl.lockf(fd,fcntl.LOCK_EX|fcntl.LOCK_NB,512,0x40000000,os.SEEK_SET)"
        )
        fd = os.open(self.database, os.O_RDWR)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 512, 0x40000000, os.SEEK_SET)
            with self.assertRaises(backup.BackupError):
                backup.inspect_backup(self.database)
            after = subprocess.run([sys.executable, '-I', '-c', probe, str(self.database)],
                                   capture_output=True)
            self.assertNotEqual(after.returncode, 0,
                                'Rejected inspection released caller-owned POSIX source locks')
        finally:
            os.close(fd)
        self.assertEqual(self.database.read_bytes(), self.original)

    def test_case_alias_cannot_publish_inside_source(self):
        alias = self.root / 'STATE'
        if not alias.exists() or not alias.samefile(self.state):
            self.skipTest('Filesystem does not resolve differently cased directory aliases')
        for parent in (alias, alias / 'nested'):
            if parent.name == 'nested':
                parent.mkdir(mode=0o700)
            destination = parent / 'archive'
            with self.assertRaises(backup.BackupError) as raised:
                backup.create_backup(self.state, destination)
            self.assertEqual(raised.exception.code, 'unsafe_destination')
            self.assertFalse(destination.exists())
        self.assertEqual(self.database.read_bytes(), self.original)

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin ACLs are independent of mode bits')
    def test_inherited_public_read_acl_is_rejected_before_backup(self):
        output = self.root / 'acl-output'
        output.mkdir(mode=0o700)
        subprocess.run(['chmod', '+a',
                        'everyone allow read,readattr,readextattr,readsecurity,file_inherit,directory_inherit',
                        str(output)], check=True)
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        destination = output / 'synthetic.backup'
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.state, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(self.database.read_bytes(), self.original)

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin ACLs are independent of mode bits')
    def test_public_read_acl_on_source_is_rejected(self):
        subprocess.run(['chmod', '+a', 'everyone allow read', str(self.database)], check=True)
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.state, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.database.read_bytes(), self.original)

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin ACLs are independent of mode bits')
    def test_public_read_acl_on_archive_is_rejected(self):
        backup.create_backup(self.state, self.destination)
        subprocess.run(['chmod', '+a', 'everyone allow read', str(self.destination)], check=True)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(backup.BackupError):
            backup.inspect_backup(self.destination)

    def test_backup_cli_never_imports_operational_modules(self):
        script = r'''
import importlib.abc, sys
class RejectLive(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('avionmesh_pi.storage', 'avionmesh_pi.runtime',
                        'avionmesh_pi.service', 'avionmesh_pi.server'):
            raise AssertionError('Operational import: ' + fullname)
sys.meta_path.insert(0, RejectLive())
from avionmesh_pi.__main__ import main
raise SystemExit(main(sys.argv[1:]))
'''
        for arguments in (['state-backup', '--state-directory', str(self.state), str(self.destination)],
                          ['state-inspect', str(self.destination)]):
            result = subprocess.run([sys.executable, '-c', script, *arguments],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.database.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
