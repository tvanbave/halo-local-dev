# Security

This is development software for a trusted local network. Do not forward its HTTP
port to the internet. New Compose installations default to `trusted_lan_no_auth`:
reachable LAN clients have management and control access, including initial setup.
Setup still requires explicit new-mesh or import confirmation, and Host/Origin
checks remain active. Enable optional browser login with `HALO_BROWSER_LOGIN=true`
as described in [Setup](docs/SETUP.md#optional-browser-login). Existing installations
keep their saved login mode unless explicitly changed.

HTTP Basic login and Bearer API tokens require a trusted LAN or a secure network
path; HTTP alone does not encrypt them. MQTT TLS verifies certificates by default.
Login-enabled first-run container logs include the generated browser password;
treat those logs as private. Default no-login startup does not print that password.

Mesh passphrases, API tokens, broker credentials, SQLite state and backups are
private. Keep their files access-restricted and outside Git. State backups contain
sensitive material and sequence/journal history; restoring an old database can
rewind transmission history. A backup is not automatic permission to resume a
transmitter. Run only one bridge for a given key/source history.

Report suspected vulnerabilities using GitHub's **Report a vulnerability** option
if enabled for the repository. If it is unavailable, open an issue requesting a
private reporting channel without including exploit details or secrets. Do not
attach live credentials, private state or raw captures to public issues.

No supported stable release or security response SLA is claimed yet. Keep a
record of the exact version/image and review open operations before upgrading.

The optional `compose.bluetooth.yaml` companion reserves Bluetooth on a dedicated
Linux host. It runs as root with NET_ADMIN and NET_RAW in the host network namespace;
these are host-relevant permissions. The bridge remains unprivileged. Private D-Bus
allows only root and bridge UID 65532, with no TCP listener or service activation.
The host system-bus mount is used only for non-activating ownership checks; a
read-only socket mount alone does not restrict which D-Bus methods could be called.
No container can stop/mask host services for you. Follow SETUP before switching
providers, and do not run another BlueZ daemon on a private bus concurrently.
