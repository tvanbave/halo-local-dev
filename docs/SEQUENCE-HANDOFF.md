# Offline transmit-history admission

Version 0.4 refuses live startup, encrypted MCP sends and raw commissioning sends
unless this private state database has an admission receipt. Existing version 0.3
state is not automatically admitted. There is no default, bypass configuration,
random starting counter or automatic reactivation. Dev44 adds the explicitly
confirmed account-import mode below for ESP-compatible existing-network control.

The fixed gateway source is 32768 and the sequence is 24 bits. An inclusive
high-water mark N means the next allocated value is N+1. Allocation is committed
before dispatch, including failed dispatch. Values never wrap. Advancing a floor
is atomic; reapplying the same admission never rewinds allocations. All earlier
address reservations, operation attempts, tokens and protected groups remain.

## What constitutes evidence

The inspected public recsrmesh candidate calls random_seq for each ordinary send
and disassociation; random_seq uses random 24-bit values. It is not a persisted
monotonic history. These candidates do not establish which library was installed
at every point in the real installation. A last ESP counter, latest packet, maximum
observed capture, stopped ESP, random large starting number or old Pi backup is
**not** a complete historical high-water mark or proof that the proposed future
allocation range above that floor is unused for that key/source.

Supported admission declarations are:

- `fresh_mesh` / `new_mesh_key`: a genuinely new key with no previous use, empty
  unused Pi state, floor zero, and no other writer. Merely changing the passphrase
  file while existing devices still use an old key does not create such a mesh.
- `verified_history` / `exclusive_monotonic_writer_retirement`: an externally
  reviewed, complete chain of exclusive monotonic writers and their committed
  inclusive floors. The final writer must be stopped and retired.
- `verified_history` / `exhaustive_key_source_audit`: externally reviewed evidence
  covering **all** historical use of this key/source, including any random writers.
  The available ESP sources/captures do not supply such an audit.

Declarations require `history_complete: true`, `previous_writers_stopped: true`
and an evidence SHA256. The bridge validates shape, identity and monotonicity; it
cannot verify these real-world assertions or the contents represented by a hash.
`history_verified_by_bridge` remains false. Do not fabricate a declaration to get
past the guard. These verified-history modes remain blocked without this evidence. Account import
uses a separate, explicitly recorded compatibility policy; it does not fabricate
a verified-history declaration.

## ESP-compatible HALO account import (dev44)

The owner requested the same existing-app-setup import and local control as the
previously tested ESP bridge. First-run cloud setup therefore offers an explicit
**Import and enable local control** action after account/location/inventory review.
It saves the existing key and imports inventory only into a new empty installation.
The recorded admission mode is `esp_compatible_import`, with `fresh_mesh:false`,
`history_complete:false` and `historical_high_water_mark:null`. This is an explicit
compatibility policy, not proof that a previously used key/source has unused nonces.

The Pi retains its durable monotonic allocator: each local sequence is committed
before transmission, continues across restarts, and cannot wrap. Its high-water
mark describes this Pi's allocations only; `historical_floor_known:false` exposes
that boundary. It does not import an ESP last counter or label the old key fresh.
Historical overlap with legacy random senders or the vendor app is not excluded,
so this mode does not provide the stronger all-writer no-reuse assurance of the
verified-history policies. It preserves the existing network's interoperability.

This mode is created by reviewed first-install cloud adoption, not the generic
`sequence-admit` command. Admission refuses any prior inventory, operation journal,
sequence use, admission or retirement. Setup preserves an interrupted staging
folder and refuses to overwrite an existing installation. Existing state locks,
permanent retirement and all uncertain-operation recovery rules remain enforced.
Stop the old ESP bridge before using the Pi for active control; never run copies
of the Pi data volume or restore an older copy as current transmit history.

## Full installation backup restore (dev68)

The encrypted UI backup preserves the original admission and node identity, which
also own scene and controller records. Explicitly confirmed restoration adds a
separate `tx_restore_admission_v1` receipt and retained restore history instead of
rewriting the original admission or claiming the mesh is fresh. Effective status
reports `backup_compatible_restore` and `historical_floor_known:false`.

A replacement Pi can use the saved network without re-pairing, after the owner
confirms the previous bridge is stopped and will stay off. The archive cannot
establish transmissions, allocations or programming after it was created. In-place
restore checks current history and preserves its nondecreasing counter and all
reservations; divergent physical inventory/scene/programming receipts are refused.
Pending operations remain pending or uncertain and are not replayed. Retired,
superseded or exhausted snapshots cannot be activated. This is a separate explicit
compatibility policy, not an override of the verified-history admission command.

Restore activates a complete staged generation after the old HTTP, workers, MQTT,
Bluetooth and Store close. A durable pending record allows interrupted filesystem
commit to finish; it never authorizes device setters. Previous generations are
fenced against this version's transmitter startup and retained for recovery.
Do not run a pre-dev68 image after restoring; it does not understand installation
pointers or superseded-generation markers.

See [Backup and restore](../publication/BACKUP.md) for the supported UI and limits.

## Domain alternatives under investigation

The monotonic policy prevents historical OFB IV reuse; it is not proof of a vendor
receiver high-water requirement. Inspected legacy generators use odd values only.
An even-only allocation domain or a new source could theoretically avoid that
history if all-writer domain exclusivity and device acceptance were established.
Neither is currently proven or supported by this admission format. See the
retained development investigation (not part of this public source snapshot).
No alternative-source, parity-stride or risk-acceptance bypass has been added.

## Offline commands and schema

These commands acquire the exclusive private-state lock and perform no network
operations. Opening state initializes its schema and invalidates volatile bindings;
`sequence-status` may create a new empty state. Stop the service first. Never point
them at original ESP/Pi operational directories during this development.

```
python -m avionmesh_pi sequence-status --state-directory NEW_PRIVATE_STATE --mesh-passphrase-file PRIVATE_KEY_FILE
python -m avionmesh_pi sequence-admit --state-directory NEW_PRIVATE_STATE --mesh-passphrase-file PRIVATE_KEY_FILE REVIEWED_DOCUMENT.json
python -m avionmesh_pi sequence-retire --state-directory PRIVATE_STATE --mesh-passphrase-file PRIVATE_KEY_FILE
```

The admission document has exactly these fields (placeholders are not runnable):

```json
{
  "format": 1,
  "state_id": "COPY_FROM_THIS_DATABASE_STATUS",
  "mesh_fingerprint": "COPY_FROM_THIS_DATABASE_STATUS",
  "source": 32768,
  "mode": "verified_history",
  "high_water_mark": 123456,
  "previous_writers_stopped": true,
  "history_complete": true,
  "evidence_kind": "exclusive_monotonic_writer_retirement",
  "evidence_sha256": "SHA256_OF_REVIEWED_COMPLETE_EVIDENCE",
  "fresh_mesh": false
}
```

No example endorses 123456 as an actual safe value. Evidence documents are bounded
to 8192 bytes. Mesh keys/passphrases are never written into returned receipts.
Retirement commits before returning its receipt and permanently blocks that DB;
re-running retirement returns the same receipt, allowing export after output loss.
A retirement receipt with `admitted:false` is not proof of historical safety.

## Limits and rollback

The OS lock excludes writers sharing this exact DB; it cannot detect copied DBs,
restored snapshots or independent transmitters. A copied state_id is still copied.
An old admitted database must not be treated as authority after restoration. A
fresh complete history review and nondecreasing proven floor are required. Do not
restore journals or discard reservation history to bypass uncertainty. Code can
be rolled back independently, but an old binary lacking admission/retirement guards
must never be launched against retired or unreviewed state. The legacy offline CLI does not restore operational state or convert private ESP
journals. The separate dev68 UI compatibility restore is described above.
