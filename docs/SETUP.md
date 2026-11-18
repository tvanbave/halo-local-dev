# Raspberry Pi setup

For a step-by-step first installation, start with [Install on a fresh Raspberry Pi](INSTALL.md).
This page is the reference for Bluetooth options, storage, settings and updates.

**Use wired Ethernet with the Pi's Wi-Fi disabled for the recommended setup.**
Operation over Wi-Fi has not been tested. Confirm Ethernet access before disabling
Wi-Fi, and leave Bluetooth enabled for communication with the lights.

## Choose how Bluetooth runs

Use 64-bit Raspberry Pi OS or another Linux host with a working Bluetooth adapter
and Docker Engine with Compose **v2.17 or newer**. If needed, follow the
[official Docker Engine instructions](https://docs.docker.com/engine/install/).
Python and all application dependencies are included in the image.

| Installation | Command | Bluetooth provider |
| --- | --- | --- |
| Dedicated Pi, bundled Bluetooth | `docker compose -f compose.bluetooth.yaml up -d --build` | BlueZ and private D-Bus in a companion container |
| Host Bluetooth already configured or shared | `docker compose up -d --build` | Host BlueZ over the host D-Bus socket |

These are **alternative standalone files**, not overlays. Use one consistently.
Both use the same `halo-local` project and `halo-local-data` volume. The existing
`compose.yaml` keeps host Bluetooth so an upgrade cannot silently take over it.
Docker Desktop and rootless Docker are not supported for live Bluetooth; Docker
Desktop remains suitable for offline tests.

## Option A: bundled Bluetooth on a dedicated Pi

The image installs BlueZ and D-Bus automatically. You do **not** need to install
BlueZ on the host. The host still provides Linux Bluetooth drivers, adapter firmware
and its normal system D-Bus service at `/run/dbus`. A private bus inside the stack
carries bridge traffic; the host bus is checked only for a competing BlueZ owner.
If the host bus is unavailable or access is denied, startup refuses to proceed.

The bundled daemon reserves this host's Bluetooth adapters for HALO Local. Use
Option B if Home Assistant, desktop peripherals, audio or another application
needs host Bluetooth. A second adapter does not make two independent daemons on
the same host a supported configuration.

On a fresh **dedicated** Pi whose host Bluetooth service exists, stop and mask it
once, preventing D-Bus from starting it again:

```sh
sudo systemctl mask --now bluetooth.service
```

If that service is absent because BlueZ was never installed, skip that command.
Do not disable D-Bus or Pi services that initialize Bluetooth hardware/UART/firmware.
Also stop any manually launched `bluetoothd` or other Bluetooth container. The
ownership guard detects BlueZ on the host bus and duplicate instances using this
stack's socket volume; it cannot discover unrelated daemons using private buses.

From the repository, start the whole stack:

```sh
docker compose -f compose.bluetooth.yaml up -d --build
docker compose -f compose.bluetooth.yaml logs bridge
```

The companion starts private D-Bus and BlueZ, enables `HALO_ADAPTER` (default
`hci0`), and checks that it is powered before the bridge starts. It never scans,
pairs devices, resets hardware or changes host services during this startup step.
The bridge's normal device workflows perform radio operations after setup.

You can avoid repeating `-f compose.bluetooth.yaml` by adding this to a local
`.env` file (excluded from Git):

```dotenv
COMPOSE_FILE=compose.bluetooth.yaml
```

Then `docker compose up -d --build`, `docker compose logs bridge` and the other
commands below select the bundled stack. Do not set `COMPOSE_FILE` for Option B.

**Validation status:** container builds, private-bus access and supervisor behavior
are tested offline. Real Pi adapter startup, daemon restart and lighting control
with bundled BlueZ still require physical acceptance. Existing bridge hardware
results in README used host BlueZ and do not qualify this new mode.

## Option B: keep host BlueZ

Raspberry Pi OS commonly includes BlueZ. On Debian/Raspberry Pi OS it can be
installed and enabled with:

```sh
sudo apt-get update
sudo apt-get install -y bluez rfkill
sudo systemctl enable --now bluetooth
sudo rfkill unblock bluetooth
sudo bluetoothctl power on
```

Then, from the repository, with no `COMPOSE_FILE` override:

```sh
docker compose up -d --build
docker compose logs bridge
```

The bridge reads the host adapter inventory before serving. It does not change
host adapter power or run a Bluetooth daemon in this mode. The host D-Bus policy
must permit the bridge UID **65532** to use BlueZ, and that UID must exist on the
host: the host bus disconnects a user ID it cannot look up. Create it once with
the two commands in [Install, step 5](INSTALL.md#5-create-the-bridges-user).
Bundled mode does not need this; its private bus has the user built in.

## Finish first-run setup

The first build downloads runtime dependencies. Later starts use the cached build.
Open the LAN URL printed in the bridge logs (port **18089** by default). New
installations open directly without a username or password. Setup still requires
an explicit choice to create a new mesh or import an existing HALO Home setup.

### Optional browser login

Browser login is **off by default for new installations**. Anyone who can reach
the bridge on your LAN can manage it and control its devices. Keep it on a trusted
network and do not forward the HTTP port to the internet. Host and browser-origin
checks remain enabled, including during initial setup. Full backup downloads and
restores also require this Pi’s owner password. Retrieve it with the command below.
[Backup and restore](BACKUP.md#confirm-backup-access).

To require a login, add this to the repository's local `.env` file, preserving
any other settings:

```dotenv
HALO_BROWSER_LOGIN=true
```

Run `docker compose up -d --build` using the same Compose file as before. Sign in
as **halo**. A first-run login-enabled install prints its generated password once;
to retrieve the saved password later, run:

```sh
docker compose exec bridge python -m avionmesh_pi login-info
```

For bundled Bluetooth, include `-f compose.bluetooth.yaml` unless using the `.env`
shortcut. To turn an existing installation's login off, set
`HALO_BROWSER_LOGIN=false` and recreate the bridge with the same command. An empty
or unset value preserves the saved access mode on upgrades. Switching either way
preserves the saved password, mesh key, devices, settings and transmit history.
MQTT works in either mode; its broker credentials are separate.

When enabled, the password protects setup, UI, assets, event stream and API.
HTTP does not encrypt credentials; use a secure network path for remote access.

### Use an existing HALO Home / Avi-on setup

1. Choose **Import existing lights** and sign in with the account used in
   the app. Your account password is not saved. The short-lived login and preview
   expire after ten minutes or when cancelled.
2. Select the correct account location and choose **Preview this location**.
   Review its device names and group memberships.
3. Stop the old ESP bridge for this network. Confirm the selected setup and choose
   **Import and enable local control**. The original mesh key and addressed devices
   are imported into this new Pi installation; devices are not reset or re-paired.
4. The bridge selects a nearby CSRMesh Bluetooth relay and starts local control.
   Cloud login is no longer needed for ordinary operation. Connect MQTT and share
   selected devices/groups with Home Assistant when ready.

The imported mesh key is kept only in the private persistent data volume. Account
passwords and login tokens are not written there or returned in the preview.
Interrupted imports preserve their partial installation for review rather than
replacing keys or silently repeating the import.

Later, **Settings → Import from Avi-on / HALO Home** previews compatible metadata
updates for the same mesh. Existing local names, sharing choices and memberships
are preserved. Neither import nor sync reprograms membership, imports scenes or
schedules, or changes keypad assignments. Cloud records can differ from the lights'
physical tables; imported memberships are not claimed to be fresh radio readback.

This ports the ESP account-import behavior, with explicit location choice and
review. It records an ESP-compatible import origin, not a fictional fresh mesh or
complete historical counter audit. Durable Pi sequence allocation and restart
history remain enabled. See [transmit-history policy](SEQUENCE-HANDOFF.md).
The Pi account-import path is tested with synthetic services; first use on a real
account and a Pi test installation remains a hardware acceptance step.

### Create a new local network

Choose **Start a new network**, then confirm **Create a new mesh** to generate a new key and initial transmit-history
record together. Until an explicit create/import action no operational database
is created. Use **Add devices** to commission lights and controllers. Follow battery
wake prompts and review uncertain operations rather than repeatedly pairing.

The new-mesh and cloud-import options apply only to an empty installation.
For an existing HALO Local network, choose **Restore a backup** on the
first-run page instead. Upload a full `.halobackup`, review its contents, and
confirm the old bridge is stopped and will stay off. Paired-device and Home
Assistant identities are retained; no re-pairing is performed. The replacement
keeps its own network, adapter and browser-login settings. See [Backup & restore](BACKUP.md)
for the limits of older backups and the checks to perform after restore.
Replacement-Pi recovery still needs a physical test.

## Home Assistant

In **Settings → Home Assistant over MQTT**, enter your existing broker, test the
connection and save. No broker is required to start or use native lighting control.
Compose does not create another MQTT broker. Share selected devices and groups
using **Add to Home Assistant** in their options. Controller HA mode publishes
events; room actions belong in HA automations.

Standard setup enables the available light/group and controller editors. New
installations using development build 0.5.0.dev59 or later also enable device and
controller removal. The hardware table in README describes remaining acceptance
tests. Claim-reset recovery remains disabled by default. Cloud sync is enabled for
an imported setup; it runs only when requested, not periodically in the background.

## Rooms and lighting groups

Rooms organise the bridge page. Assigning a light or controller to a room does
not create a lighting group, add it to a group, or change its wall controls.

For the best response when controlling several lights together, create a group
in **Groups** and add those lights to it. Groups appear first on **Home**.
Room controls use a group only when it contains exactly that room's lights;
otherwise they send commands to each light separately. Each room card shows
which method it uses when it contains more than one light.

To reuse a room's lights, choose **Create group from room** on its Home card.
The group name is prefilled. Choose **Create group and review lights**, then
**Add selected** to save and verify the preselected lights. Controllers are
excluded; unsupported lights are listed before creation. An exact existing
group opens directly, and a same-named empty group is reused after cancellation.
RL4 lights and A19 bulbs support this verified membership workflow, including
mixed groups. A change is saved only after matching complete table readbacks;
uncertain writes are retained for read-only recovery and are not retried.

## Removing lights, controllers, groups and scenes

- **Lights and controllers:** open the item and choose **Advanced tools → Remove
  from mesh…**. Review the listed dependencies before confirming. Keep it powered
  and follow any controller wake instructions. The bridge sends one removal
  request, then checks automatically that the device is unpaired. It stays listed
  until removal is verified. If verification cannot finish, use **Settings →
  Operations**; the removal request is not repeated automatically.
- **Groups:** open the group under **Groups**. Use **Remove lights** first and
  wait for membership verification, then choose **Remove group…** in its **Remove
  group** section. Only ordinary empty groups can be removed; protected controller
  targets are not offered. Removing the empty group changes bridge records only,
  leaves its lights paired and does not reuse its identifier. Reassign wall
  controls that still point at the group.
- **Scenes:** open **Scenes → Edit / details → Remove scene…**. This clears the
  stored preset from its lights and verifies removal; the lights stay paired.
  **Forget scene…** only removes the bridge record and Home Assistant entry,
  leaves the preset in the lights, and reserves its slot. See the
  [scene guide](SCENES.md).

Removing an item from Home Assistant is separate: **Remove from Home Assistant**
only stops sharing it and does not unpair a device or erase its native settings.
Review your Home Assistant automations separately before removing their targets.

### Cloud-imported lights

Cloud import retains the mesh address and model, but older imports do not include
an original pairing UUID. For these lights, removal checks that the selected
address responds, records currently unpaired devices, then sends the unpair
command once. It looks for a newly unpaired light of the expected model.

The light remains listed until you confirm its physical reset in **Settings →
Operations**. The page offers confirmation only when one new matching candidate
is found, and checks it again before completing removal. If several candidates
are present, switch off unrelated unpaired lights and choose **Check what
happened** again. Keep the selected light powered on. This records your physical
confirmation plus observed unpaired status; it does not claim that the bridge
knew the bulb's UUID before removal. A reconnect or page refresh never repeats
the unpair command. This development workflow is ready for physical testing.

### Enable removal on an existing Compose installation

Updating keeps the saved removal setting. For installations created by earlier
builds, add this to the repository's local `.env` file, preserving any existing
settings:

```dotenv
HALO_REMOVAL=true
```

Then run `docker compose up -d --build` to recreate the bridge with the setting.
Use the same Compose file as before (include `-f compose.bluetooth.yaml` if that
is how you run bundled Bluetooth). This enables the reviewed removal controls;
it does not remove anything. `HALO_REMOVAL=false` disables device/controller
removal. Neither value changes group or scene removal, and neither bypasses
unfinished operations, dependency checks or verification.

## Containers and persistent storage

The bridge runs as UID/GID **65532**, with all Linux capabilities dropped, a read-only
root filesystem and host networking for its private LAN address. Neither stack
uses privileged mode, host PID access or the Docker socket.

The optional companion runs as root with only **NET_ADMIN** and **NET_RAW** added
back. BlueZ needs kernel Bluetooth access; host networking makes those permissions
significant even though no host filesystem is writable. It has its own private
D-Bus policy, no TCP bus listener or service activation, and allows only root and
the bridge UID to connect. A read-only host bus mount enables ownership checks;
read-only mounting a socket does not by itself restrict D-Bus methods.

| Volume | Contents | Preserve |
| --- | --- | --- |
| `halo-local-data` | Mesh identity, sequence history, journals, inventory, login and MQTT settings | Always; this is the operational installation |
| `halo-local-bluetooth-state` | BlueZ adapter cache/state (bundled mode) | Include in a stopped backup |
| `halo-local-bluetooth-bus` | Private socket, service lock and D-Bus machine ID | Managed by the companion; no mesh key or database |

BlueZ state is separate from HALO mesh pairing and transmit history. Copying or
removing BlueZ state cannot restore or migrate a mesh.

## Optional settings

Most installs need no settings beyond selecting the Compose file. If needed, add
the following to `.env`:

```dotenv
HALO_HOST=192.168.50.20
HALO_PORT=18089
HALO_ADAPTER=hci0
```

Use the Pi's actual private LAN IPv4 address. Automatic selection picks a private
address on a default-route interface, skipping common Docker/VPN interfaces. A
DHCP change is picked up on restart without changing mesh identity/history. Reserve
the Pi's DHCP address for predictable access. Both bundled services use the same
`HALO_ADAPTER`; only that adapter receives the supervisor's power-on request.

## Pairing capacity

**Add devices** shows the remaining pairing attempts before you start. The bridge retains up to 128 ordinary pairing attempts over the lifetime of a mesh, including retired attempts. Each retained attempt reserves its address and records what happened during programming. Removing a light preserves that history. If capacity is exhausted, keep the installation and its latest full backup for review.

## Stop, update and preserve state

Follow the [update and rollback procedure](INSTALL.md#update) using the Compose selection you installed. The running bridge stays available while the replacement images build.

Before an update, download a full `.halobackup` from **Settings → Backup & restore**
and keep it off the Pi with its password stored separately. Backups are manual;
this build does not yet schedule or send them to remote storage.

Named volumes are retained. Never use `docker compose down -v` or delete volumes
to fix startup. Missing state or interrupted initialization is preserved and
startup refuses to generate a replacement. If preserving raw volumes for diagnosis,
stop both services first; a raw-volume archive is not a UI restore file.

To restore, use the reviewed **Backup & restore** workflow rather than copying an
older database over the active one. Current-Pi restore preserves the latest counter
and address history and refuses conflicting newer physical changes. Replacement
restore requires the old bridge and all copied instances to stay off, and explicitly
acknowledges that an older backup cannot establish later radio history. See the
[full backup guide](BACKUP.md) for recovery steps, retained pre-restore copies and
post-restore scene/controller reads.

### Switching between these two Compose options

This applies only to an installation already using the new setup's complete
`halo-local-data` volume. Stop the current stack **before** changing providers;
never run both bridges or daemons concurrently. Do not change the data volume name,
create a new mesh, re-pair lights or copy only part of the database.

Host to bundled (remove any conflicting `COMPOSE_FILE` entry first):

```sh
docker compose -f compose.yaml down
sudo systemctl mask --now bluetooth.service
docker compose -f compose.bluetooth.yaml up -d --build
```

Bundled to host, with BlueZ installed on the host:

```sh
docker compose -f compose.bluetooth.yaml down
sudo systemctl unmask bluetooth.service
sudo systemctl enable --now bluetooth
sudo bluetoothctl power on
docker compose -f compose.yaml up -d --build
```

Do not add `-v`. Align `.env` with the chosen file afterward. Host and bundled BlueZ
keep separate caches. After a provider change, verify adapter health, bridge
reconnection, MQTT and a light/group command. Keep the previous image for rollback.

An older installation using `serve CONFIG --allow-live` must retain its exact
configuration, state mounts and user permissions. These quick-start commands do
not migrate that deployment; upgrade through its existing deployment procedure.
`compose.live.yaml` remains an advanced manual example, not a quick-start overlay.

## Troubleshooting bundled Bluetooth

```sh
docker compose -f compose.bluetooth.yaml ps
docker compose -f compose.bluetooth.yaml logs --tail=100 bluetooth bridge
```

- **Host BlueZ is running:** stop/mask the host service on a dedicated Pi or use
  host mode. The companion never stops it for you. It checks again while running
  and shuts both child daemons down if a host owner appears or checks fail.
- **Cannot check host ownership:** confirm the host D-Bus service and `/run/dbus`
  exist. Do not remove the guard or substitute a permissive host policy.
- **Adapter not found:** check `HALO_ADAPTER`, host kernel drivers/firmware and
  Bluetooth initialization services. The container cannot supply kernel drivers.
- **Could not enable Bluetooth:** inspect host `rfkill list`. If soft-blocked,
  install the host `rfkill` utility if needed and run `sudo rfkill unblock bluetooth`.
  Hardware blocks or missing firmware require a host fix. The stack does not
  mount `/dev/rfkill` or load kernel modules.
- **A Bluetooth service exited:** the companion stops both of its services and
  exits so Docker can restart it. Before 0.5.0.dev57 it could instead stay running
  without Bluetooth on a Pi; update if you see that.
- **Unhealthy after a daemon restart:** after fixing the cause, use
  `docker compose -f compose.bluetooth.yaml restart bluetooth bridge`. Initial
  startup waits for health, but Compose does not automatically restart a dependent
  container for every runtime health change. A companion crash is restarted by
  Docker; recovery of an existing BLE connection still requires live acceptance.
- **Private bus permission denied:** the bundled policy allows bridge UID 65532.
  Do not override the bridge user or reuse a legacy user-1000 deployment here.

## Offline replay

```sh
docker compose -f compose.replay.yaml run --rm --build replay
```

This has no network, Bluetooth or home-service mounts. A plain image build/run
still defaults to offline replay. CI builds the optional Bluetooth target and tests
real private D-Bus with a fake adapter service inside a network-isolated container;
it never starts the live stack or connects to host Bluetooth.

Implementation references: [BlueZ daemon capabilities](https://github.com/bluez/bluez/blob/master/src/bluetooth.service.in),
[D-Bus custom buses and policies](https://dbus.freedesktop.org/doc/dbus-daemon.1.html),
and [Compose startup ordering](https://docs.docker.com/compose/how-tos/startup-order/).
