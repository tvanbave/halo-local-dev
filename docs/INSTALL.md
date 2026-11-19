# Install on a fresh Raspberry Pi

This guide starts from a blank SD card and ends with the HALO Local page open in
your browser. Allow about 30 minutes on a Raspberry Pi 4 and an hour on a
Raspberry Pi 3. [Setup](SETUP.md) is the reference for the options mentioned here.

HALO Local is a development build for experienced testers. Read
[what works and what is still being tested](../README.md#hardware-status) first.

## What you need

- A Raspberry Pi with built-in Bluetooth. Development and testing use a
  **Raspberry Pi 4 Model B with 2 GB of memory**. This guide has also been
  followed on a **Raspberry Pi 3 Model B** through a successful build, healthy
  startup and update. Opening the page, first-run setup and pairing have not yet
  been tested on that installation. Builds are slow on the Pi 3. Other models
  are untested.
- A microSD card (16 GB or larger) and the official power supply.
- A wired Ethernet connection to your home network and a computer on it.
- The Pi placed within Bluetooth range of at least one of your lights.

**Wired Ethernet with the Pi's Wi-Fi disabled is recommended. Operation over
Wi-Fi has not been tested.** Confirm you can reach the Pi over Ethernet before
disabling its Wi-Fi. Keep Bluetooth enabled; it connects to the lights.

HALO Local does not need a keyboard or screen on the Pi.

## 1. Prepare the SD card

1. Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on your computer.
2. Choose your Pi model, then **Raspberry Pi OS (other)** and a **Lite (64-bit)**
   version. The 64-bit version is required. Long-term testing uses the Debian 12
   "Bookworm" release, which Imager lists as "Legacy". The install steps have
   also been followed on Debian 13 "Trixie". The desktop version works too but
   uses more memory.
3. Choose the SD card. When Imager offers customisation settings:
   - set a hostname, for example `halo`;
   - set a username and password;
   - leave Wi-Fi unconfigured for the recommended Ethernet setup;
   - under **Services**, enable SSH with password authentication.
4. Write the card, put it in the Pi and power it on. The first start takes a few minutes.

## 2. Connect and update the Pi

From your computer (replace `pi` with the username you chose):

```sh
ssh pi@halo.local
```

If the name is not found, use the Pi's IP address from your router. Then update it:

```sh
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

Reconnect with `ssh` after a minute. Reserve the Pi's address in your router
(a "DHCP reservation") so the page stays at the same address.

## 3. Install Docker and Git

```sh
sudo apt install -y git
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
```

That is Docker's own install script; you can read it first, or follow the
[manual steps for Debian](https://docs.docker.com/engine/install/debian/) instead.

The last command lets your user run Docker, but only for logins that start after
it. **Log out and connect again now:**

```sh
exit
```

```sh
ssh pi@halo.local
```

Then check:

```sh
groups
docker compose version
```

`groups` must list `docker`. If it does not, you are still in the old login:
disconnect and connect again. `docker compose version` must report **v2.17 or
newer**. Docker Desktop and rootless Docker are not supported.

## 4. Check Bluetooth

Raspberry Pi OS already runs the Bluetooth service that HALO Local uses.

```sh
bluetoothctl show
```

Look for `Powered: yes`. If it says `no`, or no controller is listed:

```sh
sudo rfkill unblock bluetooth
sudo bluetoothctl power on
```

If the command itself is missing, install it with
`sudo apt install -y bluez rfkill` and `sudo systemctl enable --now bluetooth`.

This guide uses the Pi's own Bluetooth service ("Option B" in [Setup](SETUP.md)).
It is the arrangement the project's hardware results come from. The alternative
that bundles Bluetooth in a container is described in Setup and has not yet been
accepted on real hardware.

## 5. Create the bridge's user

The bridge runs inside its container as an unprivileged user with the ID
**65532**. The Pi's system message bus, which carries requests to the Bluetooth
service, hangs up on any user ID that does not exist on the Pi. Create a matching
user that cannot log in:

```sh
sudo groupadd --gid 65532 halo-local
sudo useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin halo-local
```

`useradd` prints a warning that the ID is "outside of the UID_MIN 1000 and
UID_MAX 60000 range". That is expected. Nothing needs restarting.

## 6. Download and start HALO Local

For a fresh installation on 64-bit Raspberry Pi OS, you can skip the image build. Follow steps 1–5 above, then use the [prebuilt dev78 image and commands](https://github.com/tvanbave/halo-local-dev/releases/tag/v0.5.0-dev78). To build from source, continue below.

```sh
git clone https://github.com/tvanbave/halo-local-dev.git halo-local
cd halo-local
docker compose up -d --build
```

The first build downloads the base image and Python packages. On a Raspberry
Pi 4 it takes a few minutes. On a **Raspberry Pi 3 it took 21 minutes**, most of
it unpacking to the SD card. Later starts reuse the build.

Docker may print "Your kernel does not support memory limit capabilities …
Limitation discarded." Raspberry Pi OS ships with that kernel feature off, so
Docker skips the bridge's 512 MB memory cap. The bridge runs the same without it.

When the build finishes:

```sh
docker compose logs bridge
```

You should see lines like these:

```text
HALO Local: open http://<address of your Pi>:18089
Open the page to create a new mesh or import your HALO Home setup.
```

New installations do not require a browser username or password. To enable login,
or turn it off on an older installation, see [Optional browser login](SETUP.md#optional-browser-login).

If the log shows an error instead, see [If something goes wrong](#if-something-goes-wrong).

## 7. First-run setup in the browser

Open the address from the log on a computer or phone on the same network. No
browser login is required by default. Then choose **one** of:

- **Start a new network** to pair factory-reset lights and controllers locally. An A19 can be the first light with automatic setup.
- **Import existing lights** to bring over a HALO Home or Avi-on network. Sign in, pick your location and review the inventory. Stop any other bridge controlling that network before confirming the import.
- **Restore a backup** to move an existing bridge to an empty replacement Pi. Stop the source bridge, select its full backup file and review the installation before confirming. See [Backup and restore](BACKUP.md).

Home Assistant can be connected after any of these choices.

## 8. Connect Home Assistant (optional)

In **Settings → Home Assistant over MQTT**, enter your existing MQTT broker, test
the connection and save. Then use **Add to Home Assistant** on the lights, groups
and scenes you want there. HALO Local does not install a broker and works without one.

## Day-to-day commands

Run these from the `halo-local` folder on the Pi.

| To | Command |
| --- | --- |
| See what the bridge is doing | `docker compose logs --tail=100 bridge` |
| Check that it is running | `docker compose ps` |
| Restart it | `docker compose restart bridge` |
| Stop it | `docker compose stop` |
| Start it again | `docker compose up -d` |

The bridge starts by itself when the Pi boots.

### Update

Download a full backup, resolve unfinished operations and use the same Compose file as your running installation. For the optional bundled Bluetooth stack, first set `export COMPOSE_FILE=compose.bluetooth.yaml` in the terminal. Run the following block from the checkout:

```sh
(
  set -eu
  for service in $(docker compose config --services); do
    container=$(docker compose ps -q "$service")
    image=$(docker inspect --format '{{.Image}}' "$container")
    docker image tag "$image" "halo-rollback-$service:previous"
  done
  git pull --ff-only
  docker compose build
  docker compose up -d --no-build --pull never --wait
)
```

The running bridge keeps serving during download and build. After the switch, open the page and check Bluetooth, MQTT and inventory. If the new build fails to start and the release supports the previous data format, restore the saved image tags in the same terminal:

```sh
(
  set -eu
  docker image tag halo-rollback-bridge:previous halo-local:local
  if docker compose config --services | grep -qx bluetooth; then
    docker image tag halo-rollback-bluetooth:previous halo-local-bluetooth:local
  fi
  docker compose up -d --no-build --pull never --wait
)
```

If the release also changed Compose settings, use the previous release’s Compose file for the rollback. Keep the existing data volume and review the bridge status before sending commands.

Updates keep an existing installation's saved device-removal setting. To enable
removal controls on an older installation, follow
[Enable removal on an existing Compose installation](SETUP.md#enable-removal-on-an-existing-compose-installation).

### Back up and restore

Fresh dev78 installations support full backups and restores on your LAN without editing `.env` or entering a bridge owner password. In **Settings → Backup & restore**, choose **Download backup** and keep the file off the Pi. Select **Protect with a password** if you want an encrypted copy. Save that password separately. See [Trusted-LAN backups](BACKUP.md#trusted-lan-backups) for access settings and upgrade behaviour.

To replace a Pi without re-pairing lights, choose **Restore a backup**
on the replacement's first-run page, review the file and confirm that the old
bridge is stopped and will stay off. Current-Pi restore is available in Settings.
Read the [backup guide](BACKUP.md) first: it explains older-backup limits,
Home Assistant identity, retained recovery copies and post-restore checks.
Replacement-Pi recovery still needs a physical test.

#### Preserve a raw volume for diagnosis

The installation files live in the `halo-local-data` Docker volume. The following
command makes a private, unencrypted raw archive while the bridge is stopped.
It is useful for preserving files, but its `.tar` output is **not** a full UI
backup and cannot be uploaded to the `.halobackup` restore workflow:

```sh
(
  set -eu
  umask 077
  install -d -m 700 "$HOME/halo-backups"
  archive=$(mktemp --suffix=.partial "$HOME/halo-backups/halo-local-$(date +%Y%m%d-%H%M%S)-XXXXXXXX")
  trap 'docker compose start' EXIT
  docker compose stop
  docker run --rm --network none --user 0 --entrypoint tar \
    -v halo-local-data:/data:ro halo-local:local -C /data -cf - . > "$archive"
  tar -tf "$archive" > /dev/null
  mv "$archive" "${archive%.partial}.tar"
  printf 'Saved %s\n' "${archive%.partial}.tar"
)
```

The raw archive contains credentials and private state. Store it privately.
If using the bundled Bluetooth stack, its separate Bluetooth volume is not part
of this command. See the [storage reference](SETUP.md).

Do not copy an older database over a running installation or start another bridge
from a copied volume. That can discard later counter, address and operation
history. Use the [reviewed UI restore](BACKUP.md) for a `.halobackup` file; preserve
raw-volume archives for a separately reviewed recovery procedure.

### Remove

```sh
cd halo-local
docker compose down
```

This removes the containers and keeps your data. Do **not** add `-v` and do not
delete the `halo-local-data` volume unless you intend to abandon this
installation: the mesh key is in it, and without the key the bridge can no longer
control lights paired to a mesh it created.

## If something goes wrong

Start with the log: `docker compose logs --tail=100 bridge`.

| Message or symptom | What to do |
| --- | --- |
| `No private LAN address found` | Create a file named `.env` in the `halo-local` folder containing `HALO_HOST=` followed by the Pi's address, then `docker compose up -d`. |
| `Cannot bind the bridge address/port` | Another program uses port 18089. Add `HALO_PORT=18090` (or another free port) to `.env`. |
| `Bluetooth adapter not found` | Run `bluetoothctl show`. If nothing is listed, the Pi's Bluetooth is disabled or blocked; repeat step 4. |
| `Bluetooth is powered off` | `sudo rfkill unblock bluetooth`, `sudo bluetoothctl power on`, then `docker compose restart bridge`. |
| `The system message bus closed the connection` | The bridge's user does not exist on the Pi. Run the two commands in step 5; the bridge then starts by itself within a minute. Versions before 0.5.0.dev58 print `Cannot access Bluetooth` for this. |
| `Cannot access Bluetooth (…)` | The Pi's message bus could not be reached. Check `systemctl is-active dbus bluetooth`; the word in brackets is the kind of failure. |
| `BlueZ is unavailable or D-Bus access was denied` | Check `systemctl status bluetooth`. On a standard Raspberry Pi OS install the default policy already allows the bridge. |
| `Setup was interrupted` | Do not delete anything. Keep the volume and open an issue describing what happened. |
| The page does not load | Use `http://`, not `https://`. Confirm the address and that your device is on the same network. |
| You enabled browser login and lost the password | `docker compose exec bridge python -m avionmesh_pi login-info` |
| `permission denied while trying to connect to the docker API` | Your login started before you joined the `docker` group. Log out and back in (step 3), and check that `groups` lists `docker`. |
| The bridge shows `Restarting` in `docker compose ps` | It stops when a start-up check fails, and Docker starts it again. The reason is in the log. |

Never run `docker compose down -v`, and never delete a volume to fix a start-up
problem. The bridge refuses to create a replacement installation on purpose.

## Security

HALO Local is for a trusted home network. Do not forward its port to the internet.
Browser login is optional and off by default for new installations. Anyone who
can reach the bridge on your LAN can manage it. Optional login travels over plain
HTTP, so use a network you trust. See [Security](../SECURITY.md).

## What this guide has and has not been tested on

**Raspberry Pi 3 Model B (Rev 1.2), Raspberry Pi OS with desktop, Debian 13
"Trixie", 64-bit, on 2026-11-16.** A tester followed this guide with version
0.5.0.dev57:

- The download and build worked. The build took 21 minutes.
- The bridge could not reach Bluetooth until the user in step 5 was created. That
  step was missing from the guide and was added because of this test. With the
  user in place the bridge started by itself, read the Pi's Bluetooth adapter,
  reported healthy and printed the login, the address and the first-run line.
- Opening the page, first-run setup and pairing a light were not part of that test.
- The Pi was then updated to 0.5.0.dev58 with the commands under
  [Update](#update). The rebuild took 8 minutes, the existing data volume was
  reused, and the bridge started and reported healthy.
- The new start-up message that names a missing bridge user has been checked
  against a real message bus in a container, but has not been triggered on a Pi.

**Raspberry Pi 4 Model B (2 GB), Raspberry Pi OS Lite, Debian 12 "Bookworm",
64-bit, Docker 28 and Compose 2.35:**

- The published source builds with `docker compose build` (about a minute once
  the base image is downloaded), and both Compose files are accepted.
- The complete offline test suite passes in the built image with no network,
  along with the installed-package, replay and dependency checks.
- The optional bundled-Bluetooth image builds and its isolated tests pass.
- The project's bridge has run long-term on that Pi using the Pi's own Bluetooth
  service. That is an earlier manual deployment which runs as the Pi's normal
  user, not the Compose commands in this guide.

Also true:

- The message bus refusing a user ID that does not exist on its host, and
  accepting it once the user in step 5 is created, is reproduced in containers
  on Debian 12 and Debian 13.
- First-run setup, import and new-mesh creation are covered by offline tests.

Not yet done:

- First-run setup and pairing a light on a newly installed Pi. If you try it,
  please [open an issue](https://github.com/tvanbave/halo-local-dev/issues) and say
  what happened.
- A complete installation on a Raspberry Pi 4 or on the Lite image following
  these exact steps.
- Other Pi models.
