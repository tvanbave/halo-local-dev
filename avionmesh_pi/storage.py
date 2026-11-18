"""Private durable state, monotonic transmit sequences and non-retrying journals."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
import fcntl

from .bridge import load_devices
from .protocol import integer


class Store:
    def __init__(self, directory, key):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise ValueError("State directory must be private (0700)")
        path = directory / "bridge.sqlite3"
        if path.is_symlink():
            raise ValueError("State file must not be a symlink")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        if path.stat().st_mode & 0o077:
            raise ValueError("State file must be private (0600)")
        self.lock_fd = os.open(directory / "bridge.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.db = None
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            self.lock_fd = None
            raise RuntimeError("Another bridge owns this state directory") from None
        try:
            self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
            self._initialize(key)
        except BaseException:
            self.close()
            raise

    def _initialize(self, key):
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS journals(id TEXT PRIMARY KEY, kind TEXT NOT NULL,
            target INTEGER NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS bindings(target INTEGER PRIMARY KEY, snapshot TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 0);
        ''')
        fingerprint = hashlib.sha256(key).hexdigest()
        with self.transaction():
            old = self.get("mesh_fingerprint")
            if old is not None and old != fingerprint:
                raise ValueError("State belongs to a different mesh")
            self.set("mesh_fingerprint", fingerprint)
            if self.get("state_id") is None:
                self.set("state_id",uuid.uuid4().hex)
            if self.get("sequence") is None:
                self.set("sequence", 0)
            # Attempted operations are never retried when the process restarts.
            self.db.execute("UPDATE journals SET status='uncertain' WHERE status='attempted'")
            self.db.execute("UPDATE bindings SET verified=0")
            self.reserve_targets(g['group_id'] for g in self.inventory()['groups'])
            for (raw,) in self.db.execute('SELECT snapshot FROM bindings'):
                self.reserve_targets(s['target'] for s in json.loads(raw)['slots'])

    def transaction(self):
        store = self
        class Transaction:
            def __enter__(self):
                store.db.execute("BEGIN IMMEDIATE")
            def __exit__(self, typ, val, tb):
                store.db.execute("ROLLBACK" if typ else "COMMIT")
        return Transaction()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def set(self, key, value):
        if key=='inventory':
            load_devices(value['devices'])
            if not isinstance(value['groups'],list) or len(value['groups'])>128:
                raise ValueError('Inventory group capacity exceeded')
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))

    def require_tx_ready(self):
        if self.get('installation_superseded_v1'):
            raise RuntimeError('This installation was superseded by a restore')
        if not self.get('tx_admission') or self.get('tx_retirement'):
            raise RuntimeError('TX history admission is required and retirement is permanent')
        if self.get('sequence') >= 0xffffff:
            raise RuntimeError('MCP sequence exhausted')

    def allocate_sequence(self):
        with self.transaction():
            self.require_tx_ready()
            sequence = self.get("sequence") + 1
            if sequence > 0xffffff:
                raise RuntimeError("MCP sequence exhausted; cannot wrap or reset")
            self.set("sequence", sequence)
        return sequence

    def inventory(self):
        return self.get("inventory", {"devices": [], "groups": []})

    def import_inventory(self, data):
        if not isinstance(data, dict) or set(data) != {"devices", "groups"}:
            raise ValueError("Expected public devices and groups only")
        load_devices(data["devices"])
        ids = {d["device_id"] for d in data["devices"]}
        seen = set()
        if not isinstance(data["groups"], list) or len(data["groups"])>128:
            raise ValueError("Expected groups list")
        for group in data["groups"]:
            if (not isinstance(group, dict) or set(group) != {"group_id", "name", "members", "exposed", "protected"}
                    or type(group["exposed"]) is not bool or type(group["protected"]) is not bool
                    or not isinstance(group["name"], str) or not 1 <= len(group["name"]) <= 128
                    or not isinstance(group["members"], list)):
                raise ValueError("Invalid group")
            integer(group["group_id"], 256, 24575, "group ID")
            if group["group_id"] in seen or len(set(group["members"])) != len(group["members"]):
                raise ValueError("Duplicate group/member")
            seen.add(group["group_id"])
            if any(type(i) is not int or i not in ids for i in group["members"]):
                raise ValueError("Unknown group member")
            if group["protected"] and (group["members"] or group["exposed"]):
                raise ValueError("Protected event targets must remain empty and unexposed")
        with self.transaction():
            previous = self.inventory()
            if ids & set(self.get('retired_addresses', [])):
                raise ValueError("Retired mesh addresses cannot be imported or reused")
            proposed = {d['device_id']:d for d in data['devices']}
            from .keypad_ownership import partial_setup_slots
            for receipt in self.journals():
                if ((receipt['kind'] == 'keypad_setup' or
                        receipt['kind'] == 'keypad_edit' and receipt['detail'].get('managed'))
                        and (receipt['status'] == 'verified' or partial_setup_slots(receipt))
                        and receipt['target'] not in self.get('retired_addresses', [])):
                    device = proposed.get(receipt['target'])
                    if device is None or device['product_id'] != receipt['detail'].get('product_id'):
                        raise ValueError('Managed keypad identity requires its removal lifecycle')
                if (receipt['kind'] == 'claim' and receipt['status'] == 'verified'
                        and receipt['id'] not in self.get('retired_claims', [])):
                    device = proposed.get(receipt['target'])
                    if device is None or device['product_id'] != receipt['detail'].get('product_id'):
                        raise ValueError('Retained claimed identities require their removal lifecycle')
            if self.journals(pending=True):
                raise ValueError("Resolve pending transactions before import")
            # Import is metadata-only. Existing protected reservations cannot vanish.
            for old in previous["groups"]:
                if old["protected"] and old not in data["groups"]:
                    raise ValueError("Import cannot alter protected reservations")
            self.reserve_targets(g["group_id"] for g in previous["groups"]+data["groups"])
            self.set("inventory", data)
            self.db.execute("UPDATE bindings SET verified=0")

    def begin(self, kind, target, detail, *, inventory=None, reserved_targets=()):
        with self.transaction():
            if any(j["target"] == target for j in self.journals(pending=True)):
                raise ValueError("Target has an unresolved transaction")
            identifier = uuid.uuid4().hex
            self.db.execute("INSERT INTO journals VALUES (?,?,?,?,?)",
                            (identifier, kind, target, "prepared", json.dumps(detail)))
            if inventory is not None:
                self.set('inventory', inventory)
            self.reserve_targets(reserved_targets)
        return identifier

    def transition(self, identifier, expected, status):
        allowed = {("prepared", "attempted"), ("prepared", "cancelled"),
                   ("attempted", "verified"), ("attempted", "uncertain"),
                   ("uncertain", "verified")}
        if (expected, status) not in allowed:
            raise ValueError("Invalid transaction transition")
        with self.transaction():
            changed = self.db.execute("UPDATE journals SET status=? WHERE id=? AND status=?",
                                      (status, identifier, expected)).rowcount
            if changed != 1:
                raise ValueError("Transaction state changed; refusing dispatch")

    def journals(self, pending=False):
        rows = self.db.execute("SELECT id,kind,target,status,detail FROM journals ORDER BY rowid").fetchall()
        return [dict(id=i, kind=k, target=t, status=s, detail=json.loads(d))
                for i, k, t, s, d in rows if not pending or s in ("prepared", "attempted", "uncertain")]

    def save_binding(self, target, snapshot):
        self.db.execute("INSERT OR REPLACE INTO bindings VALUES (?,?,1)", (target, json.dumps(snapshot)))
        self.reserve_targets(s['target'] for s in snapshot['slots'])

    def reserve_targets(self, targets):
        known = set(self.get('known_targets', []))
        updated = known | {i for i in targets if type(i) is int and 256 <= i <= 24575}
        if updated != known:
            self.set('known_targets', sorted(updated))

    def observe_target(self, target, source):
        if type(target) is not int or not 256 <= target <= 24575:
            return
        sources = self.get('target_sources', {})
        owners = sources.setdefault(str(target), [])
        if source not in owners:
            owners.append(source)
            self.set('target_sources', sources)

    def owned_target_safe(self, target, owner):
        return all(source == owner for source in self.get('target_sources', {}).get(str(target), []))

    def bindings(self):
        return {target: json.loads(value) for target, value in
                self.db.execute("SELECT target,snapshot FROM bindings WHERE verified=1")}

    def invalidate_bindings(self):
        self.db.execute("UPDATE bindings SET verified=0")

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
