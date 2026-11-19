# Full bridge backup and restore

Development build **0.5.0.dev69** adds password-protected full backups in
**Settings → Backup & restore**, and restore on the first-run page of an empty
replacement Pi. The workflow is implemented and covered by offline tests.
Physical backup/restore acceptance on the deployed bridges is still pending.

The intended result is the same paired lighting network on the restored bridge,
with its Home Assistant identifiers retained. Restore does not reset or re-pair
lights, and it does not send old group, scene or button programming requests.

## What a full backup contains

| Included | Kept elsewhere |
| --- | --- |
| Mesh credentials and paired-device identities | Raspberry Pi OS, installed packages and Docker images |
| Names, room labels, group memberships and scene records | The Pi's IP address, Ethernet/Wi-Fi settings and Bluetooth adapter selection |
| Controller assignment records and eligible saved HA event mappings | BlueZ adapter cache and operating-system Bluetooth state |
| MQTT settings and credentials, bridge identity, sharing choices and discovery identifiers | Home Assistant automations, dashboards and its own registry backup |
| Database records, radio counter, allocated/retired addresses and all operation receipts, including unfinished operations | Changes made after the backup was created |

The bridge checks each archive before download. If you select **Protect with a password**, save that password separately. You will need it to open the encrypted file. Backup and restore run locally.

The format supports a database up to **8 MiB** and portable settings up to
**128 KiB**. This limit keeps backup processing bounded on smaller Pis. Larger
installations are refused rather than exported incompletely.

## Trusted-LAN backups

On a fresh installation of dev78 or later, choose **Restore a backup** on the first screen. Select your file, review the inventory and confirm the restore. No `.env` change or bridge owner password is required. The page asks for a file password only when you select an encrypted backup.

In **Settings → Backup & restore**, **Download backup** saves a file that opens without a password. Select **Protect with a password** for an encrypted copy. Anyone who can reach a passwordless bridge on your LAN can download or restore a full backup. Anyone holding an unencrypted file can read its mesh and MQTT credentials.

Upgrades keep the installation’s previous access settings. To require owner authorization and encrypted downloads, set `HALO_TRUSTED_LAN_BACKUPS=false` in the installation’s `.env` and run `docker compose up -d` using your usual Compose file. To enable trusted-LAN backups on an older installation running dev77 or later, set it to `true` and recreate the container. A configured browser login still applies. The destination Pi controls access after a restore.

## Confirm backup access

If trusted-LAN backups are switched off and the bridge opens without a browser login, enter **this Pi’s bridge owner password** when the page asks. Get it in a terminal on that Pi, from the directory
where you start HALO Local:

```sh
docker compose exec bridge python -m avionmesh_pi login-info
```

Use the printed password in **This Pi’s bridge owner password**. With the
bundled Bluetooth stack, use your usual Compose file. If browser login is
enabled, signing in to the bridge already proves ownership. For a custom
token-authenticated service, use the destination service’s configured credential.

The separate **backup password** encrypts the file and is needed when you
restore it. Use a trusted connection when entering either password.

## Download a backup

1. Open **Settings → Backup & restore** on the running bridge.
2. Enter this Pi’s owner password if prompted. For the backup file, if password protection is selected or required, enter and confirm a separate password of **12–256 characters**.
3. Choose **Download backup**. Wait for the backup file to finish downloading.
4. Keep the `.halobackup` file on another computer or storage device, together with
   a separately stored record of its password.

Wait for active pairing or programming to finish. If the bridge reports that it
is busy, let that work complete before starting another download. Downloading a
backup does not change the lights or their native assignments.

A backup left only on the same SD card will not help if that card fails. Browser
downloads are manual in this build: scheduled backups, remote backup destinations
and a final-transfer **Move to another Pi** action are not implemented yet.

## Restore onto a replacement Pi

1. Install HALO Local on the replacement using the [installation guide](INSTALL.md).
   Use dev69 or a compatible newer build. Leave its first-run setup unconfigured:
   do not create a new mesh or import a cloud network first.
2. Stop the original bridge and leave it off. Any other bridge using a copy of its
   saved installation must also remain off. Restore does not remotely stop them.
3. Open the replacement Pi’s page and choose **Restore a backup**. Enter this Pi’s
   owner password if asked, select the `.halobackup` file, enter the file’s
   password if prompted and choose **Review backup**.
4. Check the bridge identity, backup date/version, device, group and scene counts,
   and any warnings or operations needing review.
5. Confirm the old bridge is stopped, that this is the backup to restore, and that
   changes made after the backup are not included. Choose **Restore reviewed backup**
   once.
6. Wait for the bridge to start, then choose **Reload bridge**. If the response is
   lost, reload and inspect the setup; do not submit the restore again blindly.

The destination keeps its current IP/port, adapter and access mode. An explicit
`HALO_REMOVAL` setting applies to the running installation after restore and on
subsequent restarts.
Mesh identity, node identity and MQTT settings come from the backup. This preserves
Home Assistant discovery identifiers rather than intentionally creating a second
set of entities. The broker must still be reachable from the replacement Pi.

### What an older backup can establish

A backup records the installation at one moment. If the old Pi kept running,
the file cannot tell the replacement about later devices, programming operations
or radio transmissions. Devices added after the backup will not appear in it.
An old database counter alone does not prove what happened later.

Replacement restore records an explicitly confirmed **backup-compatible restore**.
It preserves the saved history and never labels the existing network as new, but
it does not claim to verify all later radio history or that every previous writer
is stopped. This limitation is shown before restore. Use the newest available
backup and keep the original bridge and all copied instances off.

If the original Pi still works, download a current backup immediately before
stopping it. There is not yet an automatic final-transfer operation that freezes
the source and hands ownership to the replacement.

## Restore on the current Pi

Use **Settings → Backup & restore → Review backup**. The page checks the backup
against the current installation before offering **Restore reviewed backup**.
The bridge stops its own workers and restarts through the restore workflow; there
is no need to reset the lights.

This path preserves the latest known radio counter, allocated and retired
addresses, and operational history. It can restore earlier names, room labels,
sharing choices and compatible settings, but it refuses a backup if known physical
device, group-membership, scene-preset or programming/recovery history has diverged.
Use a current backup if the review reports newer physical changes. An older file
must not erase knowledge of devices or uncertain operations.

Before switching installations, the bridge retains its private previous
installation and creates an encrypted **`before-restore.halobackup`** recovery copy
in its data volume. When restoring an encrypted backup, this file uses the same password. When restoring an unencrypted backup, the Pi generates a password for the recovery copy and saves it beside that copy in the private file `before-restore.password`. These
copies are preserved for recovery review; they are not automatically started or
used for rollback. Do not manually restart a retained installation alongside the
restored bridge.

## Checks after restore

- Wait for Bluetooth and MQTT, if configured, to reconnect. Light readings start
  afresh rather than displaying old observations as current.
- Check that the expected lights, rooms, groups, scenes and controllers are listed.
- Try one light and one group, then check the existing Home Assistant entities.
- In **Scenes → Edit / details**, use **Verify saved settings** before activating
  restored scenes. Fresh scene verification does not survive restore.
- Controller editing may require a new button read; battery controllers may need
  their wake prompts. Eligible saved HA event mappings remain subject to their
  existing dependency checks.
- Review retained unfinished operations in **Settings → Operations**. Restore
  never automatically repeats a possibly sent programming request.

If verification fails, preserve the original files and the restore report. Do
not delete the data volume, reset counters or clear operation records to force a
restart. A failed application update should normally roll back compatible software
while preserving current state, rather than replace the database with an older copy.

## Other exports are different

**Device list backup** in Settings exports names and inventory metadata without
mesh keys or credentials. It cannot replace a full `.halobackup` file or migrate
a working lighting network by itself.

The older `state-backup`, `state-inspect` and `state-recovery-plan` command-line
tools work with private database-only snapshots. Those files do not include the
separately stored installation credentials/settings and are not accepted as full
UI backups. Their conservative offline comparison behaviour is unchanged.

Raw Docker-volume archives remain useful for preserving files during an incident,
but they are not interchangeable with the reviewed UI restore format. Never start
two bridge instances from copied volumes.
