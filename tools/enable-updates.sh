#!/bin/sh
# Run once from the public checkout as the regular user who runs Docker Compose.
set -eu
stack=${1:-compose.yaml}
case "$stack" in compose.yaml|compose.bluetooth.yaml) ;; *) echo "Choose compose.yaml or compose.bluetooth.yaml." >&2; exit 2;; esac
test "$#" -le 1
owner=$(id -un)
root=$(pwd -P)
case "$owner" in ''|*[!a-zA-Z0-9_-]*) exit 2;; esac
case "$root" in *[!a-zA-Z0-9_./\ -]*) echo "The updater supports standard Linux directory names." >&2; exit 2;; esac
if [ "$(id -u)" = 0 ] || [ ! -f avionmesh_pi/release_update.py ] || [ ! -f "$stack" ]; then
  echo "Run this in the bridge checkout as your regular Docker-enabled Pi user." >&2
  exit 1
fi
docker compose -f "$stack" ps -q bridge | grep -Eq '^[a-f0-9]{12,64}$' || {
  echo "Start the bridge with this Compose file before enabling updates." >&2; exit 1; }
tmp=$(mktemp -d)
trap 'rm -f "$tmp/halo-local-update.socket" "$tmp/halo-local-update@.service"; rmdir "$tmp"' EXIT
cat > "$tmp/halo-local-update.socket" <<'UNIT'
[Unit]
Description=HALO Local bridge update socket
[Socket]
ListenStream=/run/halo-local-update/control
Accept=yes
SocketUser=65532
SocketMode=0600
DirectoryMode=0755
MaxConnections=10
[Install]
WantedBy=sockets.target
UNIT
cat > "$tmp/halo-local-update@.service" <<UNIT
[Unit]
Description=HALO Local bridge update request
[Service]
User=$owner
SupplementaryGroups=docker
WorkingDirectory=$root
StateDirectory=halo-local-update
StateDirectoryMode=0700
UMask=0077
StandardInput=socket
StandardOutput=journal
StandardError=journal
NoNewPrivileges=yes
RuntimeMaxSec=40min
ExecStart=/usr/bin/python3 -I /usr/local/libexec/halo-local-updater.py $stack
UNIT
sudo install -D -m 0644 avionmesh_pi/release_update.py /usr/local/libexec/halo-local-updater.py
sudo install -m 0644 "$tmp/halo-local-update.socket" "$tmp/halo-local-update@.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now halo-local-update.socket
docker compose -f "$stack" exec -T bridge python -Ic '
from avionmesh_pi.release_update import SOCKET, send, read_line
import socket
with socket.socket(socket.AF_UNIX) as client:
    client.settimeout(10)
    client.connect(SOCKET)
    send(client, {"action":"status"})
    with client.makefile("rb") as stream:
        assert "phase" in read_line(stream)
' || {
  echo "The running bridge cannot reach its updater. Check that this build and its Compose file are installed." >&2
  exit 1
}
echo "Pi updater enabled. Open Settings → Bridge updates."
