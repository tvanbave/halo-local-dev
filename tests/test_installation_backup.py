"""Portable encrypted backups use only synthetic local state and credentials."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID

from avionmesh_pi import installation_backup as b
from avionmesh_pi.migration import retire
from avionmesh_pi.protocol import derive_mcp_key
from support import synthetic_store

PASSPHRASE = 'synthetic-mesh-only-never-live'
PASSWORD = 'synthetic backup password 2026'
CREATED = '2026-11-17T12:30:00Z'


def certificate():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Synthetic offline CA')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class InstallationBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pem = certificate()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name).resolve() / 'state'
        self.store = synthetic_store(self.path, derive_mcp_key(PASSPHRASE))
        self.store.import_inventory({'devices': [
            {'device_id': 40001, 'product_id': 134, 'name': 'Synthetic light', 'exposed': True},
            {'device_id': 40002, 'product_id': 127, 'name': 'Synthetic keypad', 'exposed': True}],
            'groups': [
                {'group_id': 291, 'name': 'Synthetic group', 'members': [40001], 'exposed': True, 'protected': False},
                {'group_id': 292, 'name': 'Synthetic HA target', 'members': [], 'exposed': False, 'protected': True}]})
        self.store.allocate_sequence()
        self.store.set('device_rooms', {'40001': 'Synthetic room', '40002': 'Synthetic room'})
        self.store.set('native_scenes_v1', [{'id': 'synthetic-scene', 'reference': 1, 'name': 'Synthetic scene', 'members': []}])
        self.store.set('future_private_metadata', {'value': 'synthetic-private-history'})
        self.store.set('retired_addresses', [40009])
        self.store.set('retired_claims', ['synthetic-retired-claim'])
        self.store.save_binding(40002, {'slots': [{'target': 292}], 'private': 'synthetic-binding'})
        self.journal = self.store.begin('group_membership', 40001, {'group': 291, 'credential': 'synthetic-journal-secret'})
        self.store.transition(self.journal, 'prepared', 'attempted')
        self.store.close()
        self.database = (self.path / 'bridge.sqlite3').read_bytes()
        self.settings = {'node': 'synthetic_bridge', 'mesh_passphrase': PASSPHRASE,
            'api_token': 'synthetic-api-token-not-real', 'browser_login': False,
            'capabilities': {'group_membership': True, 'removal': False},
            'mqtt': {'format': 1, 'revision': '1' * 32, 'enabled': True,
                'broker': {'host': 'broker.invalid', 'port': 8883, 'username': 'synthetic-user',
                           'password': 'synthetic-broker-password', 'tls': True, 'ca_pem': self.pem}}}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def encode(self, settings=None, password=PASSWORD, database=None):
        return b.encode_backup(self.database if database is None else database,
            self.settings if settings is None else settings, password, created_at=CREATED)

    def error(self, code, operation):
        with self.assertRaises(b.InstallationBackupError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return str(caught.exception)

    def raw_metadata(self):
        return {'format': 1, 'created_at': CREATED, 'version': '0.5.0.dev67',
            'settings': copy.deepcopy(self.settings),
            'database': {'bytes': len(self.database), 'sha256': hashlib.sha256(self.database).hexdigest()}}

    def seal(self, metadata=None, database=None, raw=None):
        metadata = self.raw_metadata() if metadata is None else metadata
        data = self.database if database is None else database
        raw = json.dumps(metadata).encode() if raw is None else raw
        header = b.MAGIC + bytes(16) + bytes(12)
        plaintext = struct.pack('>I', len(raw)) + raw + data
        return header + AESGCM(b._key(b._password(PASSWORD), bytes(16))).encrypt(bytes(12), plaintext, header)

    def test_full_roundtrip_preserves_private_state_and_portable_settings(self):
        original = copy.deepcopy(self.settings)
        before = self.database
        with patch('avionmesh_pi.storage.Store', side_effect=AssertionError('Never start state')):
            archive = self.encode()
            decoded = b.decode_backup(archive, PASSWORD)
        self.assertEqual(decoded['database'], before)
        self.assertEqual(decoded['settings'], original)
        self.assertEqual(self.settings, original)
        self.assertEqual(decoded['created_at'], CREATED)
        self.assertEqual((self.path / 'bridge.sqlite3').read_bytes(), before)
        self.assertEqual(decoded['summary'], {
            'devices': 2, 'lights': 1, 'controllers': 1, 'groups': 1, 'rooms': 1, 'scenes': 1,
            'pending_operations': 1, 'retirement_record_present': False, 'sequence_exhausted': False,
            'node': 'synthetic_bridge', 'created_at': CREATED, 'version': b.__version__,
            'mqtt_enabled': True, 'encrypted': True, 'integrity_verified': True})
        for secret in (PASSPHRASE, original['api_token'], 'synthetic-broker-password',
                       'synthetic-journal-secret', 'synthetic-private-history', 'synthetic-binding'):
            self.assertNotIn(secret.encode(), archive)
            self.assertNotIn(secret, json.dumps(decoded['summary']))
        info = b.inspect_database(decoded['database'], PASSPHRASE)
        self.assertEqual(info['sequence'], 1)
        self.assertTrue(info['admitted'])
        self.assertEqual(info['admission_mode'], 'fresh_mesh')
        self.assertEqual(info['journal_statuses']['attempted'], 1)
        self.assertEqual(info['journal_statuses']['uncertain'], 0)

    def test_random_salt_and_nonce_produce_distinct_archives(self):
        first, second = self.encode(), self.encode()
        self.assertNotEqual(first, second)
        self.assertEqual(b.decode_backup(first, PASSWORD), b.decode_backup(second, PASSWORD))

    def test_wrong_password_and_modified_header_or_ciphertext_never_decrypt(self):
        archive = self.encode()
        self.error('invalid_password_or_backup', lambda: b.decode_backup(archive, 'another synthetic password'))
        for offset in (len(b.MAGIC), len(b.MAGIC) + 16, b._HEADER_BYTES + 5, len(archive) - 1):
            changed = bytearray(archive); changed[offset] ^= 1
            self.error('invalid_password_or_backup', lambda: b.decode_backup(bytes(changed), PASSWORD))
        changed = bytearray(archive); changed[0] ^= 1
        self.error('invalid_backup', lambda: b.decode_backup(bytes(changed), PASSWORD))

    def test_truncated_appended_oversized_and_wrong_format_rejected(self):
        archive = self.encode()
        for value in (archive[:2], archive[:b._HEADER_BYTES], b'PK' + archive, archive.decode('latin1')):
            self.error('invalid_backup', lambda: b.decode_backup(value, PASSWORD))
        for value in (archive[:-1], archive + b'extra'):
            self.error('invalid_password_or_backup', lambda: b.decode_backup(value, PASSWORD))
        with patch.object(b, 'MAX_ARCHIVE_BYTES', len(archive) - 1), patch.object(b, '_key', side_effect=AssertionError('No KDF for oversized input')):
            self.error('invalid_backup', lambda: b.decode_backup(archive, PASSWORD))

    def test_password_bounds_and_unicode_are_consistent(self):
        for value in ('', 'short', 'x' * 257, 'x' * 12 + '\x00', None, b'not-a-text-password', '\ud800' * 12):
            self.error('invalid_backup_password', lambda: self.encode(password=value))
        password = 'üñîçøde 🔑 password'
        archive = self.encode(password=password)
        self.assertEqual(b.decode_backup(archive, password)['database'], self.database)

    def test_portable_settings_reject_host_paths_unknown_keys_and_bad_flags(self):
        cases = []
        for field in ('http_host', 'adapter', 'state_directory', '../service.json', 'files'):
            cases.append(dict(self.settings, **{field: 'do-not-restore'}))
        cases.extend([dict(self.settings, node='../wrong'), dict(self.settings, browser_login=1),
            dict(self.settings, capabilities={'arbitrary_executable': True}),
            dict(self.settings, capabilities={'removal': 1}), dict(self.settings, api_token=''),
            dict(self.settings, mesh_passphrase='x' * 253)])
        bad = copy.deepcopy(self.settings); bad['mqtt']['broker']['ca_file'] = '/synthetic/path.pem'; cases.append(bad)
        bad = copy.deepcopy(self.settings); bad['mqtt']['broker']['ca_pem'] = 'not a PEM certificate'; cases.append(bad)
        bad = copy.deepcopy(self.settings); bad['mqtt']['broker']['ca_pem'] = '\ud800'; cases.append(bad)
        bad = copy.deepcopy(self.settings); bad['mqtt']['broker']['ca_pem'] = '-' * 5 + 'BEGIN PRIVATE KEY' + '-' * 5; cases.append(bad)
        bad = copy.deepcopy(self.settings); bad['mqtt']['broker']['host'] = 'https://broker.invalid'; cases.append(bad)
        bad = copy.deepcopy(self.settings); bad['mqtt']['enabled'] = 1; cases.append(bad)
        for value in cases:
            with self.subTest(value=list(value)):
                self.error('invalid_backup_settings', lambda: self.encode(settings=value))

    def test_mesh_credential_must_match_database(self):
        settings = dict(self.settings, mesh_passphrase='different-synthetic-mesh')
        self.error('backup_mesh_mismatch', lambda: self.encode(settings=settings))
        metadata = self.raw_metadata(); metadata['settings'] = settings
        self.error('backup_mesh_mismatch', lambda: b.decode_backup(self.seal(metadata), PASSWORD))

    def test_authenticated_inner_schema_and_digest_are_still_checked(self):
        changes = [lambda m: m.update(format=2), lambda m: m.update(format=True),
                   lambda m: m.update(extra='not allowed'), lambda m: m.update(created_at='invalid'),
                   lambda m: m.update(created_at='2026-02-31T00:00:00Z'),
                   lambda m: m.update(version='../../run'), lambda m: m['database'].update(bytes=True),
                   lambda m: m['database'].update(bytes=len(self.database) + 1),
                   lambda m: m['database'].update(sha256='0' * 64),
                   lambda m: m['database'].update(path='../../state')]
        for change in changes:
            metadata = self.raw_metadata(); change(metadata)
            with self.subTest(metadata=list(metadata)):
                with self.assertRaises(b.InstallationBackupError):
                    b.decode_backup(self.seal(metadata), PASSWORD)

    def test_duplicate_json_nonfinite_and_nested_metadata_rejected(self):
        original = json.dumps(self.raw_metadata()).encode()
        duplicate = b'{"format":1,' + original[1:]
        for raw in (duplicate, b'{"format":NaN}', b'\xff'):
            self.error('invalid_backup', lambda: b.decode_backup(self.seal(raw=raw), PASSWORD))
        # CPython JSON depth limits differ: either parser rejection or the
        # subsequent exact root-schema rejection is a complete refusal.
        with self.assertRaises(b.InstallationBackupError) as caught:
            b.decode_backup(self.seal(raw=b'[' * 1500 + b']' * 1500), PASSWORD)
        self.assertIn(caught.exception.code, ('invalid_backup', 'unsupported_backup_format'))
        with patch.object(b, 'MAX_METADATA_BYTES', 2):
            self.error('invalid_backup_settings', lambda: self.encode())
            self.error('invalid_backup', lambda: b.decode_backup(self.seal(), PASSWORD))

    def test_database_integrity_schema_and_inventory_validated_read_only(self):
        for value in (b'', self.database[:100], b'x' * len(self.database)):
            self.error('invalid_backup_database', lambda: self.encode(database=value))
        wal = bytearray(self.database); wal[18] = 2
        self.error('invalid_backup_database', lambda: self.encode(database=bytes(wal)))
        changes = ["CREATE TABLE unexpected(value TEXT)",
                   "UPDATE metadata SET value='not json' WHERE key='inventory'",
                   "UPDATE metadata SET value='[]' WHERE key='inventory'",
                   "UPDATE metadata SET value='false' WHERE key='sequence'",
                   "UPDATE metadata SET value='false' WHERE key='tx_admission'"]
        for statement in changes:
            path = Path(self.temp.name) / 'changed.sqlite3'; path.write_bytes(self.database)
            with sqlite3.connect(path) as db: db.execute(statement)
            self.error('invalid_backup_database', lambda: self.encode(database=path.read_bytes()))
            path.unlink()

    def test_retirement_and_unresolved_receipts_are_preserved_not_cleared(self):
        store = synthetic_store(self.path, derive_mcp_key(PASSPHRASE)); retire(store); store.close()
        raw = (self.path / 'bridge.sqlite3').read_bytes()
        decoded = b.decode_backup(self.encode(database=raw), PASSWORD)
        self.assertEqual(decoded['database'], raw)
        self.assertTrue(decoded['summary']['retirement_record_present'])
        self.assertEqual(decoded['summary']['pending_operations'], 1)
        self.assertEqual((self.path / 'bridge.sqlite3').read_bytes(), raw)

    def retired_claim_database(self, *, pending=True):
        # Synthetic completed retirement keeps its receipt and spent address.
        # The production claim lifecycle owns this terminal transition.
        path = self.path / 'bridge.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('INSERT INTO journals VALUES (?,?,?,?,?)',
                ('synthetic-retired-claim', 'claim', 40009, 'retired',
                 json.dumps({'uuid': '0100000000021400000a000000000000',
                             'product_id': 134, 'retired_before_network_key': True})))
            if not pending:
                db.execute("UPDATE journals SET status='verified' WHERE id=?", (self.journal,))
        return path.read_bytes()

    def test_retired_claim_roundtrip_preserves_receipt_and_excludes_it_from_pending(self):
        for pending in (True, False):
            with self.subTest(pending=pending):
                (self.path / 'bridge.sqlite3').write_bytes(self.database)
                raw = self.retired_claim_database(pending=pending)
                decoded = b.decode_backup(self.encode(database=raw), PASSWORD)
                self.assertEqual(decoded['database'], raw)
                self.assertEqual(decoded['summary']['pending_operations'], 1 if pending else 0)
                self.assertFalse(decoded['summary']['retirement_record_present'])
                info = b.inspect_database(decoded['database'], PASSPHRASE)
                self.assertEqual(info['journal_statuses']['retired'], 1)
                self.assertEqual(info['journal_records'], 2)
                self.assertEqual((self.path / 'bridge.sqlite3').read_bytes(), raw)
                # The encrypted form must not expose the retired identity.
                self.assertNotIn(b'synthetic-retired-claim', self.encode(database=raw))

    def test_legacy_snapshot_accepts_retired_claim_and_retains_zero_retired_manifest_shape(self):
        before = b.inspect_database(self.database, PASSPHRASE)
        self.assertEqual(set(before['journal_statuses']),
                         {'prepared', 'attempted', 'uncertain', 'verified', 'cancelled'})
        legacy = Path(self.temp.name).resolve() / 'before.avionbackup'
        report = b.state_backup._create_backup(self.path, legacy)
        self.assertEqual(b.state_backup.inspect_backup(legacy), report)
        raw = self.retired_claim_database(pending=False)
        retired = Path(self.temp.name).resolve() / 'retired.avionbackup'
        report = b.state_backup._create_backup(self.path, retired)
        self.assertEqual(report['summary']['journal_statuses']['retired'], 1)
        self.assertEqual(report['summary']['journal_statuses']['attempted'], 0)
        self.assertEqual(b.state_backup.inspect_backup(retired), report)
        self.assertEqual((self.path / 'bridge.sqlite3').read_bytes(), raw)

    def test_disabled_mqtt_roundtrip_and_api_token_not_replaced(self):
        settings = copy.deepcopy(self.settings)
        settings['browser_login'] = True
        settings['mqtt'] = {'format': 1, 'revision': '2' * 32, 'enabled': False, 'broker': {}}
        decoded = b.decode_backup(self.encode(settings), PASSWORD)
        self.assertFalse(decoded['summary']['mqtt_enabled'])
        self.assertTrue(decoded['settings']['browser_login'])
        self.assertEqual(decoded['settings']['api_token'], settings['api_token'])
        self.assertEqual(decoded['settings']['mqtt']['broker'], {'host': '', 'username': '', 'tls': True, 'port': 8883})

    def test_errors_and_success_do_not_print_private_values(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            blob = self.encode(); b.decode_backup(blob, PASSWORD)
            message = self.error('invalid_password_or_backup', lambda: b.decode_backup(blob, 'incorrect synthetic password'))
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), '')
        self.assertNotIn(PASSWORD, message)
        self.assertNotIn(PASSPHRASE, message)

    def test_input_database_size_checked_before_sqlite_inspection(self):
        with patch.object(b, 'MAX_DATABASE_BYTES', len(self.database) - 1), patch.object(b.state_backup, '_connect', side_effect=AssertionError('No SQLite for oversized input')):
            self.error('invalid_backup_database', lambda: b.inspect_database(self.database, PASSPHRASE))
