"""Credential-private offline state snapshots. Deliberately no restore operation.

The SQLite file is copied without opening Store, which would mutate journals and
binding verification. Both the bridge flock and the full SQLite POSIX locking range
must be obtainable. A separate lock holder detects same-process writers too.
Rollback-journal format and absence of recovery sidecars are required.
"""
from contextlib import contextmanager
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile

MAGIC = b'AVIONMESH-PRIVATE-STATE\x00\x01\n'
MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 8192
MAX_JSON_BYTES = 1024 * 1024
MAX_ROWS = 100000
CHUNK = 65536
KIND = 'avionmesh_pi_private_state'
NEXT_STEP = ('Keep this snapshot private and the current state intact. There is no restore command. '
             'Before any recovery, obtain independent complete key/source TX history and all newer '
             'address reservations, retirements and operation receipts; have a recovery procedure '
             'reviewed without lowering any current floor or clearing uncertain operations. '
             'A backup or its recorded sequence alone is not that evidence.')


class BackupError(ValueError):
    """A bounded, credential-free diagnostic suitable for the offline CLI."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _fail(code, message):
    raise BackupError(code, message)


def _absolute(path):
    path = Path(path)
    if '..' in path.parts:
        _fail('unsafe_path', 'Parent traversal is not accepted; use a direct absolute path.')
    return path.absolute()


def _private(info, *, directory=False, single_link=False):
    if ((not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
            or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or (single_link and info.st_nlink != 1)):
        _fail('unsafe_permissions', 'State and output must be owner-private regular files and directories.')


def _no_acl(fd):
    """Mode bits do not bound Darwin ACL grants. Reject extended ACL paths.

    Never strip an ACL or change source security settings. Harmless deny ACLs
    are conservatively unsupported too; callers must choose another safe path.
    """
    try:
        if sys.platform == 'darwin':
            libc = ctypes.CDLL(None, use_errno=True)
            libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
            libc.acl_get_fd_np.restype = ctypes.c_void_p
            libc.acl_free.argtypes = [ctypes.c_void_p]
            libc.acl_free.restype = ctypes.c_int
            ctypes.set_errno(0)
            acl = libc.acl_get_fd_np(fd, 0x100)  # Darwin ACL_TYPE_EXTENDED
            if acl:
                libc.acl_free(acl)
                _fail('unsafe_acl', 'Extended ACL paths are not supported; choose an ACL-free private path. '
                      'Do not remove protective ACLs to bypass this check.')
            if ctypes.get_errno() not in (errno.ENOENT, errno.ENOTSUP, errno.EOPNOTSUPP):
                _fail('unsafe_acl', 'Extended access controls could not be verified.')
        elif sys.platform.startswith('linux'):
            acl_names = {'system.posix_acl_access', 'system.posix_acl_default',
                         'system.nfs4_acl', 'system.richacl'}
            if acl_names.intersection(os.listxattr(fd)):
                _fail('unsafe_acl', 'Extended ACL paths are not supported; choose an ACL-free private path. '
                      'Do not remove protective ACLs to bypass this check.')
        else:
            _fail('unsafe_acl', 'Extended access-control verification is unsupported on this platform.')
    except (AttributeError, OSError):
        _fail('unsafe_acl', 'Extended access controls could not be verified.')


@contextmanager
def _directory(path, *, forbidden_inode=None):
    """Pin a directory after rejecting symlinks in every path component."""
    path = _absolute(path)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        _no_acl(fd)
        for part in path.parts[1:]:
            try:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                _fail('unsafe_path', 'A directory is missing, inaccessible, or contains a symlink.')
            os.close(fd)
            fd = new
            info = os.fstat(fd)
            _no_acl(fd)
            if (info.st_dev, info.st_ino) == forbidden_inode:
                _fail('unsafe_destination', 'Store backups outside the operational state directory.')
            if (info.st_uid not in (0, os.geteuid())
                    or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
                _fail('unsafe_path', 'Directory ancestors must not permit untrusted replacement.')
        _private(os.fstat(fd), directory=True)
        yield path, fd
    finally:
        os.close(fd)


def _open_file(directory_fd, name, flags=os.O_RDONLY, *, single_link=False):
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except OSError:
        _fail('unsafe_path', 'A required private regular file is missing, inaccessible, or a symlink.')
    try:
        _private(os.fstat(fd), single_link=single_link)
        _no_acl(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _unchanged(directory_fd, name, before):
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        _fail('source_changed', 'Source changed during the snapshot; no backup was published.')
    if _signature(current) != _signature(before):
        _fail('source_changed', 'Source changed during the snapshot; no backup was published.')


def _no_sidecars(directory_fd):
    # Even a cold/empty leftover is refused. This tool must never attempt recovery.
    for name in ('bridge.sqlite3-journal', 'bridge.sqlite3-wal', 'bridge.sqlite3-shm'):
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        _fail('recovery_required', 'SQLite recovery files exist; inspect the stopped original state with '
              'a qualified recovery procedure before backing up. This tool will not alter them.')


def _json(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_JSON_BYTES:
        _fail('invalid_state', 'State contains an invalid or oversized JSON record.')
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError('Duplicate JSON key')
            obj[key] = value
        return obj
    def nonfinite(_):
        raise ValueError('Nonfinite JSON')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, RecursionError, UnicodeError):
        _fail('invalid_state', 'State contains invalid JSON.')


# POSIX record locks are process-scoped: taking this lock in the caller could
# silently succeed despite an already-open SQLite writer in that same process.
# A tiny isolated child owns ONLY the descriptor/lock, never SQL or source data.
# Parent death closes stdin, immediately releasing the lock. No network imports.
_LOCK_HOLDER = r"""
import fcntl,json,os,stat,sys
try:
    fd=os.open('bridge.sqlite3',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=int(sys.argv[1]))
except OSError:
    print('unsafe_path',flush=True)
    sys.exit(1)
info=os.fstat(fd)
if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
        or info.st_mode&0o077 or info.st_nlink!=1):
    print('unsafe_permissions',flush=True)
    sys.exit(1)
try:
    fcntl.lockf(fd,fcntl.LOCK_EX|fcntl.LOCK_NB,512,0x40000000,os.SEEK_SET)
except OSError:
    print('busy',flush=True)
    sys.exit(1)
info=os.fstat(fd)
print(json.dumps([info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns]),flush=True)
sys.stdin.buffer.read(1)
"""


@contextmanager
def _sqlite_lock(directory_fd):
    # Crucially, the parent must not even open/close the source file before the
    # child excludes writers: close(any fd to inode) would release locks of an
    # existing SQLite transaction in the parent's process, even on rejection.
    process = subprocess.Popen([sys.executable, '-I', '-S', '-c', _LOCK_HOLDER, str(directory_fd)],
                               pass_fds=(directory_fd,), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    try:
        if not select.select([process.stdout], [], [], 10)[0]:
            _fail('lock_unavailable', 'SQLite lock verification timed out; no backup was made.')
        response = process.stdout.readline(2048)
        if response == b'unsafe_path\n':
            _fail('unsafe_path', 'A required private regular file is missing, inaccessible, or a symlink.')
        if response == b'unsafe_permissions\n':
            _fail('unsafe_permissions', 'State and output must be owner-private regular files and directories.')
        try:
            signature = json.loads(response)
        except (ValueError, UnicodeError):
            signature = None
        if (not isinstance(signature, list) or len(signature) != 5
                or any(type(value) is not int for value in signature)):
            _fail('active_writer', 'SQLite is in use or cannot be exclusively locked; no backup was made.')
        yield process, tuple(signature)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def _lock_alive(process):
    if process.poll() is not None:
        _fail('lock_lost', 'SQLite lock ownership was lost; no backup was published.')


def _sqlite_header(fd):
    header = os.pread(fd, 100, 0)
    if len(header) != 100 or header[:16] != b'SQLite format 3\x00':
        _fail('corrupt_database', 'Not a supported SQLite database.')
    if header[18:20] != b'\x01\x01':
        _fail('unsupported_journal', 'Only stopped SQLite rollback-journal state without sidecars is supported.')


def _connect(path):
    # This helper only opens a private staged COPY, never operational state.
    db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1',
                         uri=True, isolation_level=None, timeout=0)
    try:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        steps = 0
        def budget():
            nonlocal steps
            steps += 1
            return steps > 5000  # At most about 50 million SQLite VM operations.
        db.set_progress_handler(budget, 10000)
        if hasattr(db, 'setlimit'):
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_JSON_BYTES + 4096)
        return db
    except BaseException:
        db.close()
        raise


def _summary(db):
    """Validate the supported schema and core identity, without interpreting receipts."""
    if db.execute('PRAGMA integrity_check').fetchmany(2) != [('ok',)]:
        _fail('corrupt_database', 'SQLite integrity verification failed.')
    objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    if set(objects) != {('table', 'metadata'), ('table', 'journals'), ('table', 'bindings')}:
        _fail('unsupported_schema', 'The snapshot has an unsupported state schema.')
    columns = {
        'metadata': [('key', 'TEXT'), ('value', 'TEXT')],
        'journals': [('id', 'TEXT'), ('kind', 'TEXT'), ('target', 'INTEGER'), ('status', 'TEXT'), ('detail', 'TEXT')],
        'bindings': [('target', 'INTEGER'), ('snapshot', 'TEXT'), ('verified', 'INTEGER')],
    }
    for table, expected in columns.items():
        actual = [(row[1], row[2]) for row in db.execute('PRAGMA table_info(' + table + ')')]
        if actual != expected:
            _fail('unsupported_schema', 'The snapshot has an unsupported state schema.')
        if db.execute('SELECT count(*) FROM ' + table).fetchone()[0] > MAX_ROWS:
            _fail('invalid_state', 'State row capacity exceeded.')
    metadata = {}
    for key, value in db.execute('SELECT key,value FROM metadata'):
        if not isinstance(key, str) or key in metadata:
            _fail('invalid_state', 'Invalid state metadata.')
        metadata[key] = _json(value)
    state_id, fingerprint, sequence = (metadata.get(k) for k in ('state_id', 'mesh_fingerprint', 'sequence'))
    if (not isinstance(state_id, str) or not re.fullmatch('[0-9a-f]{32}', state_id)
            or not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint)
            or type(sequence) is not int or not 0 <= sequence <= 0xffffff):
        _fail('invalid_state', 'Missing or invalid state identity, mesh binding, or sequence.')
    statuses = {s: 0 for s in ('prepared', 'attempted', 'uncertain', 'verified', 'cancelled')}
    # Claim retirement is a terminal receipt, not missing or pending work. Keep
    # the zero-retired shape unchanged so existing snapshot manifests still
    # compare exactly; older archives could not contain a retired receipt.
    allowed_statuses = set(statuses) | {'retired'}
    journals = 0
    for identifier, kind, target, status, detail in db.execute('SELECT id,kind,target,status,detail FROM journals'):
        if (not isinstance(identifier, str) or not identifier or not isinstance(kind, str)
                or type(target) is not int or not 0 <= target <= 65535 or status not in allowed_statuses
                or not isinstance(_json(detail), dict)):
            _fail('invalid_state', 'Invalid operation receipt.')
        statuses[status] = statuses.get(status, 0) + 1
        journals += 1
    bindings = 0
    for target, snapshot, verified in db.execute('SELECT target,snapshot,verified FROM bindings'):
        if (type(target) is not int or not 0 <= target <= 65535 or verified not in (0, 1)
                or not isinstance(_json(snapshot), dict)):
            _fail('invalid_state', 'Invalid retained binding.')
        bindings += 1
    # All metadata, including unknown keys and private journal content, remains in
    # the byte-for-byte payload. Counts below are not a recovery/high-water proof.
    return {'state_id': state_id, 'mesh_fingerprint': fingerprint, 'sequence': sequence,
            'metadata_records': len(metadata), 'journal_records': journals,
            'journal_statuses': statuses, 'binding_records': bindings}


def _copy(source, target, size):
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        data = source.read(min(CHUNK, remaining))
        if not data:
            _fail('truncated_input', 'Snapshot input was truncated.')
        target.write(data)
        digest.update(data)
        remaining -= len(data)
    if source.read(1):
        _fail('trailing_input', 'Snapshot input has unexpected trailing bytes.')
    return digest.hexdigest()


def _fsync_file(stream):
    stream.flush()
    os.fsync(stream.fileno())


def _manifest(snapshot, size, digest):
    return {'format': 1, 'kind': KIND, 'payload': {'bytes': size, 'sha256': digest},
            'snapshot': snapshot, 'restore_supported': False}


def _report(manifest):
    summary = {k: v for k, v in manifest['snapshot'].items() if k not in ('state_id', 'mesh_fingerprint')}
    return {'format': 1, 'kind': KIND, 'integrity_verified': True,
            'authenticity_verified': False, 'history_verified': False,
            'restore_supported': False, 'automatic_start': False, 'summary': summary,
            'next_step': NEXT_STEP}


@contextmanager
def _stage(parent):
    # A private child of the already validated output directory. Crash leftovers
    # remain private and are never treated as a completed destination artifact.
    with tempfile.TemporaryDirectory(prefix='.avionmesh-backup-', dir=parent) as folder:
        os.chmod(folder, 0o700)
        yield Path(folder)


def _create_backup(state_directory, destination):
    """Create a new opaque archive; never create/open Store or mutate source SQL.

    The output parent must already exist, be owner-private, and contain no path
    symlinks. The destination is published with link(2), which cannot overwrite an
    existing entry. A reported post-publication fsync failure is an uncertain
    durability outcome: inspect the destination, do not blindly retry/replace it.
    """
    destination = _absolute(destination)
    published = False
    try:
        with _directory(state_directory) as (source_path, source_fd), \
                _directory(destination.parent, forbidden_inode=(os.fstat(source_fd).st_dev,
                                                                os.fstat(source_fd).st_ino)) as (parent, parent_fd):
            if os.path.lexists(destination):
                _fail('destination_exists', 'Destination already exists; choose a new backup filename.')
            lock_fd = _open_file(source_fd, 'bridge.lock', os.O_RDWR, single_link=True)
            try:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    _fail('active_writer', 'A bridge owns this state. Stop and coordinate all writers first.')
                lock_info = os.fstat(lock_fd)
                _no_sidecars(source_fd)
                with _sqlite_lock(source_fd) as (locker, locked_signature):
                    _lock_alive(locker)
                    db_fd = _open_file(source_fd, 'bridge.sqlite3', single_link=True)
                    try:
                        info = os.fstat(db_fd)
                        if _signature(info) != locked_signature:
                            _fail('source_changed', 'Source changed while acquiring its exclusive lock.')
                        if not 512 <= info.st_size <= MAX_DATABASE_BYTES:
                            _fail('invalid_size', 'State database size is outside the supported bound.')
                        _no_sidecars(source_fd)
                        _unchanged(source_fd, 'bridge.sqlite3', info)
                        _unchanged(source_fd, 'bridge.lock', lock_info)
                        _sqlite_header(db_fd)
                        with _stage(parent) as staging:
                            snapshot = staging / 'snapshot.sqlite3'
                            with os.fdopen(os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
                                with os.fdopen(db_fd, 'rb', closefd=False) as input_stream:
                                    digest = _copy(input_stream, output, info.st_size)
                                _fsync_file(output)
                            _lock_alive(locker)
                            _unchanged(source_fd, 'bridge.sqlite3', info)
                            _unchanged(source_fd, 'bridge.lock', lock_info)
                            _no_sidecars(source_fd)
                            copied = _connect(snapshot)
                            try:
                                summary = _summary(copied)
                            finally:
                                copied.close()
                            manifest = _manifest(summary, info.st_size, digest)
                            raw = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode('utf-8')
                            if len(raw) > MAX_MANIFEST_BYTES:
                                _fail('invalid_size', 'Backup manifest exceeds the supported bound.')
                            artifact = staging / 'archive'
                            with os.fdopen(os.open(artifact, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
                                output.write(MAGIC + struct.pack('>I', len(raw)) + raw)
                                with snapshot.open('rb') as input_stream:
                                    if _copy(input_stream, output, info.st_size) != digest:
                                        _fail('source_changed', 'Staged snapshot changed before publication.')
                                _fsync_file(output)
                            # Verify the actual completed archive, not just its inputs.
                            _inspect_backup(artifact)
                            _unchanged(source_fd, 'bridge.sqlite3', info)
                            _unchanged(source_fd, 'bridge.lock', lock_info)
                            _lock_alive(locker)
                            # No replace race: link fails even if another creator or a
                            # symlink wins this name after the initial existence check.
                            try:
                                os.link(artifact, destination.name, dst_dir_fd=parent_fd, follow_symlinks=False)
                            except FileExistsError:
                                _fail('destination_exists', 'Destination already exists; choose a new backup filename.')
                            published = True
                            os.fsync(parent_fd)
                            return _report(manifest)
                    finally:
                        os.close(db_fd)  # Close while the CHILD still excludes writers.
            finally:
                os.close(lock_fd)
    except BackupError:
        raise
    except (OSError, sqlite3.Error, ValueError, OverflowError, RecursionError):
        if published:
            _fail('publication_uncertain', 'The backup may be published but final durability is uncertain. '
                  'Inspect that destination before retrying; never overwrite it.')
        _fail('backup_failed', 'Backup failed before publication; source state was not changed.')


def _inspect_backup(archive, *, recovery_evidence=False):
    """Verify a bounded private artifact, without using or importing live state."""
    archive = _absolute(archive)
    try:
        with _directory(archive.parent) as (parent, parent_fd):
            fd = _open_file(parent_fd, archive.name)
            with os.fdopen(fd, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not len(MAGIC) + 4 < before.st_size <= MAX_DATABASE_BYTES + MAX_MANIFEST_BYTES + len(MAGIC) + 4:
                    _fail('invalid_size', 'Backup size is outside the supported bound.')
                if stream.read(len(MAGIC)) != MAGIC:
                    _fail('invalid_backup', 'Not a supported private state backup.')
                header = stream.read(4)
                if len(header) != 4:
                    _fail('invalid_backup', 'Truncated backup header.')
                length = struct.unpack('>I', header)[0]
                if not 1 <= length <= MAX_MANIFEST_BYTES:
                    _fail('invalid_size', 'Backup manifest exceeds the supported bound.')
                raw = stream.read(length)
                try:
                    manifest = _json(raw.decode('utf-8'))
                except UnicodeError:
                    _fail('invalid_backup', 'Invalid backup manifest encoding.')
                if (not isinstance(manifest, dict)
                        or set(manifest) != {'format', 'kind', 'payload', 'snapshot', 'restore_supported'}
                        or type(manifest['format']) is not int or manifest['format'] != 1
                        or manifest['kind'] != KIND or manifest['restore_supported'] is not False
                        or not isinstance(manifest['snapshot'], dict)
                        or not isinstance(manifest['payload'], dict)
                        or set(manifest['payload']) != {'bytes', 'sha256'}):
                    _fail('invalid_backup', 'Invalid backup manifest schema.')
                size, digest = manifest['payload']['bytes'], manifest['payload']['sha256']
                if (type(size) is not int or not 512 <= size <= MAX_DATABASE_BYTES
                        or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
                        or before.st_size != len(MAGIC) + 4 + length + size):
                    _fail('invalid_backup', 'Backup payload bounds or length do not match.')
                with _stage(parent) as staging:
                    snapshot = staging / 'snapshot.sqlite3'
                    with os.fdopen(os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
                        if _copy(stream, output, size) != digest:
                            _fail('hash_mismatch', 'Backup payload hash verification failed.')
                        output.flush()
                    with snapshot.open('rb') as copied_header:
                        _sqlite_header(copied_header.fileno())
                    db = _connect(snapshot)
                    try:
                        if json.dumps(_summary(db), sort_keys=True) != json.dumps(manifest['snapshot'], sort_keys=True):
                            _fail('invalid_backup', 'Manifest does not match the preserved SQLite state.')
                        evidence = _recovery_evidence(db, manifest['snapshot']) if recovery_evidence else None
                    finally:
                        db.close()
                if _signature(os.fstat(stream.fileno())) != _signature(before):
                    _fail('source_changed', 'Backup changed during inspection.')
                _unchanged(parent_fd, archive.name, before)
                if recovery_evidence:
                    return evidence, _signature(before)
                return _report(manifest)
    except BackupError:
        raise
    except (OSError, sqlite3.Error, ValueError, OverflowError, RecursionError):
        _fail('inspection_failed', 'Backup could not be verified. Keep it isolated; do not use it as operational state.')


def _recovery_evidence(db, summary):
    """Private comparison material; never serialize it to stdout or a report."""
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def unique(rows):
        result = {}
        for key, value in rows:
            if key in result:
                _fail('invalid_state', 'Duplicate state records cannot be compared safely.')
            result[key] = digest(value)
        return result

    metadata = {key: _json(value) for key, value in db.execute('SELECT key,value FROM metadata')}
    return {
        'summary': summary,
        'metadata': unique(metadata.items()),
        'journals': unique((identifier, [kind, target, status, _json(detail)])
                          for identifier, kind, target, status, detail in
                          db.execute('SELECT id,kind,target,status,detail FROM journals')),
        'bindings': unique((target, [_json(snapshot), verified]) for target, snapshot, verified in
                          db.execute('SELECT target,snapshot,verified FROM bindings')),
        # Presence, including malformed false/null values, requires review. Never
        # treat a damaged retirement record as permission to transmit.
        'retirement_record_present': 'tx_retirement' in metadata,
        'admission_record_present': 'tx_admission' in metadata,
    }


def _recovery_source_unchanged(path, signature):
    path = _absolute(path)
    with _directory(path.parent) as (_, directory_fd):
        if _signature(os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)) != signature:
            _fail('source_changed', 'A snapshot changed during recovery comparison.')


def _recovery_plan(candidate, reference=None):
    """Inspect artifacts only. Compatibility is neither provenance nor TX admission."""
    try:
        proposed, proposed_signature = _inspect_backup(candidate, recovery_evidence=True)
        current = None
        if reference is not None:
            current, current_signature = _inspect_backup(reference, recovery_evidence=True)
            if proposed_signature[:2] == current_signature[:2]:
                _fail('invalid_reference', 'Candidate and reference must be distinct snapshot files.')
            _recovery_source_unchanged(reference, current_signature)
        _recovery_source_unchanged(candidate, proposed_signature)
        blockers = ['independent_complete_tx_history_required', 'all_previous_writers_must_be_stopped',
                    'reviewed_recovery_procedure_required']
        comparison = None
        if current is None:
            blockers.append('current_state_reference_unavailable')
            compatibility = 'not_compared'
        else:
            left, right = proposed['summary'], current['summary']
            same_mesh = left['mesh_fingerprint'] == right['mesh_fingerprint']
            same_lineage = left['state_id'] == right['state_id']
            differences = {}
            for table in ('metadata', 'journals', 'bindings'):
                a, b = proposed[table], current[table]
                differences[table] = {
                    'missing_from_candidate': len(b.keys() - a.keys()),
                    'only_in_candidate': len(a.keys() - b.keys()),
                    'changed': sum(a[key] != b[key] for key in a.keys() & b.keys()),
                }
            identical = not any(n for counts in differences.values() for n in counts.values())
            relation = ('lower' if left['sequence'] < right['sequence'] else
                        'higher' if left['sequence'] > right['sequence'] else 'equal')
            preservation_keys = {'inventory': 'inventory', 'address_reservations': 'known_targets',
                                 'retired_addresses': 'retired_addresses', 'retired_claims': 'retired_claims',
                                 'tx_admission': 'tx_admission', 'tx_retirement': 'tx_retirement'}
            preservation_changes = {label: proposed['metadata'].get(key) != current['metadata'].get(key)
                                    for label, key in preservation_keys.items()}
            comparison = {'same_mesh': same_mesh, 'same_state_lineage': same_lineage,
                          'sequence_relation': relation if same_mesh and same_lineage else 'not_comparable',
                          'records_equal': identical, 'differences': differences,
                          'preservation_changes': preservation_changes}
            compatibility = ('different_mesh' if not same_mesh else 'different_state_lineage'
                             if not same_lineage else 'matching_records' if identical else 'state_diverged')
            if not same_mesh:
                blockers.append('mesh_mismatch')
            if not same_lineage:
                blockers.append('state_lineage_mismatch')
            if not identical:
                blockers.append('state_records_differ_do_not_overwrite')
            if same_mesh and same_lineage and relation == 'lower':
                blockers.append('candidate_sequence_below_reference')
        inputs = {}
        for label, evidence in (('candidate', proposed), ('reference', current)):
            if evidence is None:
                continue
            summary = evidence['summary']
            unresolved = sum(summary['journal_statuses'][s] for s in ('prepared', 'attempted', 'uncertain'))
            inputs[label] = {'integrity_verified': True,
                             'unresolved_operation_records': unresolved,
                             'retirement_record_present': evidence['retirement_record_present'],
                             'admission_record_present': evidence['admission_record_present'],
                             'sequence_exhausted': summary['sequence'] == 0xffffff}
            if unresolved:
                blockers.append(label + '_unresolved_operations_require_review')
            if evidence['retirement_record_present']:
                blockers.append(label + '_transmitter_retirement_must_be_preserved')
            if not evidence['admission_record_present']:
                blockers.append(label + '_tx_admission_missing')
            if summary['sequence'] == 0xffffff:
                blockers.append(label + '_sequence_exhausted')
        return {'format': 1, 'kind': 'avionmesh_pi_recovery_plan', 'status': 'blocked',
                'compatibility': compatibility, 'comparison': comparison, 'inputs': inputs,
                'blockers': blockers, 'authenticity_verified': False, 'history_verified': False,
                'reference_currency_verified': False, 'receipt_semantics_verified': False,
                'restore_supported': False, 'automatic_start': False,
                'next_step': NEXT_STEP}
    except BackupError:
        raise
    except (OSError, sqlite3.Error, ValueError, OverflowError, RecursionError):
        _fail('inspection_failed', 'Recovery comparison failed; preserve all original state and snapshots.')


# Public calls are isolated even when embedded in an application's process.
# POSIX close(any fd to inode) drops that process's existing record locks. A
# helper dying during a copy can let another caller thread acquire such a lock;
# no source descriptor may subsequently be closed in the caller. Inspection of
# an accidentally supplied live SQLite file must obey the same boundary.
_WORKER_ENTRY = (
    "import runpy,sys; namespace=runpy.run_path(sys.argv[1]); "
    "namespace['_worker_main']()"
)


def _worker_main():
    operation = None
    try:
        request = json.loads(sys.stdin.buffer.read(16385))
        if not isinstance(request, dict) or set(request) != {'operation', 'paths'}:
            raise ValueError('Invalid worker request')
        operation, paths = request['operation'], request['paths']
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ValueError('Invalid worker paths')
        if operation == 'create' and len(paths) == 2:
            result = _create_backup(*paths)
        elif operation == 'inspect' and len(paths) == 1:
            result = _inspect_backup(*paths)
        elif operation == 'recovery-plan' and len(paths) in (1, 2):
            result = _recovery_plan(*paths)
        else:
            raise ValueError('Invalid worker operation')
        response = {'ok': True, 'result': result}
    except BackupError as exc:
        response = {'ok': False, 'code': exc.code, 'message': str(exc)}
    except Exception:
        # Never serialize arbitrary exception text, paths or state contents.
        if operation in ('inspect', 'recovery-plan'):
            response = {'ok': False, 'code': 'inspection_failed',
                        'message': 'Snapshot verification did not finish; backup integrity is unverified.'}
        else:
            response = {'ok': False, 'code': 'publication_uncertain',
                        'message': 'Snapshot verification did not finish. A destination may already exist; '
                                   'inspect it before retrying and never overwrite it.'}
    sys.stdout.write(json.dumps(response, sort_keys=True))


def _run_worker(operation, paths):
    request = json.dumps({'operation': operation, 'paths': [os.fspath(path) for path in paths]}).encode()
    if len(request) > 16384:
        _fail('unsafe_path', 'Snapshot path request exceeds the supported bound.')
    try:
        # -I -S excludes caller PYTHONPATH and all site initialization. run_path loads only
        # this trusted, installed stdlib-only module, not operational modules.
        result = subprocess.run([sys.executable, '-I', '-S', '-c', _WORKER_ENTRY,
                                 os.path.abspath(__file__)], input=request,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=180, check=False)
        if result.returncode != 0 or len(result.stdout) > 16384:
            raise ValueError('Worker did not return a bounded result')
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or type(response.get('ok')) is not bool:
            raise ValueError('Invalid worker response')
        if response['ok'] is True and set(response) == {'ok', 'result'} and isinstance(response['result'], dict):
            return response['result']
        if (response['ok'] is False and set(response) == {'ok', 'code', 'message'}
                and isinstance(response['code'], str) and isinstance(response['message'], str)):
            raise BackupError(response['code'], response['message'])
        raise ValueError('Invalid worker response')
    except BackupError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        if operation == 'create':
            _fail('publication_uncertain', 'The isolated snapshot worker did not finish. A destination '
                  'may already exist; inspect it before retrying and never overwrite it.')
        _fail('inspection_failed', 'The isolated snapshot worker did not finish; backup integrity is unverified.')


def create_backup(state_directory, destination):
    """Create a new private snapshot in an isolated worker, never opening source in caller."""
    return _run_worker('create', (state_directory, destination))


def inspect_backup(archive):
    """Verify a private artifact in isolation; never open the supplied file in caller."""
    return _run_worker('inspect', (archive,))


def recovery_plan(candidate, reference=None):
    """Return a secret-free, read-only readiness report from private snapshots."""
    paths = (candidate,) if reference is None else (candidate, reference)
    return _run_worker('recovery-plan', paths)
