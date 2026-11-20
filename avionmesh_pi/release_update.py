"""Public ARM64 release discovery and systemd-activated Pi installer (stdlib only)."""
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import urllib.request

REPO = 'tvanbave/halo-local-dev'
PUBLIC = 'https://github.com/' + REPO
SOCKET = '/run/halo-local-update/control'
STATE = Path('/var/lib/halo-local-update')
STACKS = ('compose.yaml', 'compose.bluetooth.yaml')
ACTIVE = ('preparing', 'ready', 'installing', 'rolling_back')
VERSION = re.compile(r'(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})(?:\.dev(0|[1-9]\d{0,5}))?')


class UpdateError(Exception):
    pass


def version_key(value):
    match = VERSION.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise UpdateError('Unrecognized bridge version.')
    major, minor, patch, dev = match.groups()
    return (int(major), int(minor), int(patch), dev is None, int(dev or 0))


def tag(version):
    version_key(version)
    return 'v' + version.replace('.dev', '-dev')


def archive(version):
    return 'halo-local-' + version + '-linux-arm64.tar.gz'


def open_url(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'halo-local-release-check'})
    return urllib.request.urlopen(request, timeout=20)


def small_file(url, limit):
    with open_url(url) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise UpdateError('Release metadata exceeds the supported size.')
    return data


def latest_release():
    releases = json.loads(small_file('https://api.github.com/repos/' + REPO + '/releases?per_page=100', 5 << 20))
    versions = []
    for release in releases:
        name = release.get('tag_name', '')
        candidate = name.removeprefix('v').replace('-dev', '.dev')
        if release.get('draft') or not VERSION.fullmatch(candidate) or tag(candidate) != name:
            continue
        names = {item['name'] for item in release.get('assets', []) if item.get('state') == 'uploaded'}
        if {archive(candidate), 'SHA256SUMS'} <= names:
            versions.append(candidate)
    if not versions:
        raise UpdateError('GitHub has no published bridge release with a 64-bit Pi image.')
    return max(versions, key=version_key)


def download(version, path):
    name = archive(version)
    url = PUBLIC + '/releases/download/' + tag(version) + '/'
    checks = small_file(url + 'SHA256SUMS', 65536).decode().splitlines()
    digests = [m[1].lower() for row in checks if
               (m := re.fullmatch(r'([0-9a-fA-F]{64}) [ *]' + re.escape(name), row))]
    if len(digests) != 1:
        raise UpdateError('The release has no unique checksum for the bridge image.')
    digest = hashlib.sha256()
    size = 0
    with open_url(url + name) as response, path.open('xb') as out:
        while chunk := response.read(1 << 20):
            size += len(chunk)
            if size > 1 << 30:
                raise UpdateError('The release image exceeds the supported size.')
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != digests[0]:
        raise UpdateError('The downloaded image failed its checksum. The running bridge was kept.')


def run(*args, timeout=180):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        logging.error('%s %s failed with status %d', args[0], args[1], result.returncode)
        raise UpdateError(f'{args[0]} {args[1]} failed. Check the Pi updater journal.')
    return result.stdout.strip()


class Installation:
    def __init__(self, stack):
        if stack not in STACKS:
            raise UpdateError('Unsupported Compose installation.')
        self.stack = stack

    def compose(self, *args, **kwargs):
        return run('docker', 'compose', '-f', self.stack, *args, **kwargs)

    def running(self):
        cid = self.compose('ps', '-q', 'bridge')
        if not re.fullmatch(r'[0-9a-f]{12,64}', cid):
            raise UpdateError('The running bridge was not found in this installation.')
        info = json.loads(run('docker', 'inspect', cid))[0]
        labels = info['Config']['Labels']
        volume = any(m.get('Name') == 'halo-local-data' and m['Destination'] == '/data'
                     for m in info['Mounts'])
        if (labels.get('com.docker.compose.project.config_files') != str(Path(self.stack).resolve())
                or labels.get('com.docker.compose.project.working_dir') != str(Path.cwd())
                or info['Config']['Image'] != 'halo-local:local' or not volume):
            raise UpdateError('This installation uses custom Compose settings. Update it from the Pi.')
        version = run('docker', 'exec', cid, 'python', '-Ic', 'import avionmesh_pi; print(avionmesh_pi.__version__)')
        return info['Image'], version

    def clean(self):
        if run('git', 'status', '--porcelain', '--untracked-files=no'):
            raise UpdateError('The Pi checkout has local changes. Review them before updating.')

    def prepare(self, current, target):
        if latest_release() != target or version_key(target) <= version_key(current):
            raise UpdateError('The available release changed. Check for updates again.')
        old_image, installed = self.running()
        if installed != current:
            raise UpdateError('The running version changed. Reload Settings.')
        remote = run('git', 'remote', 'get-url', 'origin').removesuffix('.git')
        if remote not in (PUBLIC, 'git@github.com:' + REPO):
            raise UpdateError('Automatic installation supports the public halo-local-dev checkout.')
        self.clean()
        old_head = run('git', 'rev-parse', 'HEAD')
        run('git', 'fetch', '--no-tags', PUBLIC + '.git', 'refs/tags/' + tag(target))
        commit = run('git', 'rev-parse', 'FETCH_HEAD^{commit}')
        run('git', 'merge-base', '--is-ancestor', old_head, commit)
        with tempfile.TemporaryDirectory(dir=STATE) as folder:
            path = Path(folder) / 'image.tar.gz'
            download(target, path)
            loaded = run('docker', 'load', '--input', str(path), timeout=600)
            if 'Loaded image: halo-local:' + target not in loaded.splitlines():
                raise UpdateError('The release archive did not contain the expected image tag.')
        image = 'halo-local:' + target
        platform = run('docker', 'image', 'inspect', '--format', '{{.Os}}/{{.Architecture}}', image)
        if platform != 'linux/arm64':
            raise UpdateError('The release image is not for 64-bit Raspberry Pi OS.')
        return old_head, commit, old_image, image, run('git', 'rev-parse', '--abbrev-ref', 'HEAD')

    def replace(self, image, version):
        run('docker', 'tag', image, 'halo-local:local')
        self.compose('up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--wait',
                     '--wait-timeout', '210', 'bridge', timeout=270)
        expected = run('docker', 'image', 'inspect', '--format', '{{.Id}}', image)
        if self.running() != (expected, version):
            raise UpdateError('The restarted bridge did not report the expected image and version.')

    def validate_stack(self):
        config = json.loads(self.compose('config', '--format', 'json'))
        bridge = config['services']['bridge']
        data = [m for m in bridge['volumes'] if m['target'] == '/data']
        if (bridge.get('image') != 'halo-local:local' or len(data) != 1
                or data[0].get('type') != 'volume'
                or config['volumes'][data[0]['source']].get('name') != 'halo-local-data'):
            raise UpdateError('The new Compose file changes the bridge image or data volume. Review it on the Pi.')

    def apply(self, prepared, current, target, record):
        old_head, commit, old_image, image, branch = prepared
        self.clean()
        if (run('git', 'rev-parse', 'HEAD') != old_head or self.running() != (old_image, current)
                or run('git', 'rev-parse', '--abbrev-ref', 'HEAD') != branch):
            raise UpdateError('The installation changed during download. Check for updates again.')
        run('docker', 'tag', old_image, 'halo-rollback-bridge:previous')
        run('git', 'update-ref', 'refs/halo-local/previous', old_head)
        record('installing', 'Restarting the bridge and checking its health.')
        # Advance only a clean checkout so Compose uses the reviewed release's
        # configuration, including from a shallow, detached prebuilt installation.
        run('git', 'merge', '--ff-only', commit)
        try:
            self.validate_stack()
            self.replace(image, target)
        except Exception:
            record('rolling_back', 'The update failed. Restarting the previous bridge image.')
            try:
                self.clean()
                if (run('git', 'rev-parse', 'HEAD') != commit
                        or run('git', 'rev-parse', '--abbrev-ref', 'HEAD') != branch):
                    raise UpdateError('The checkout changed. Review it on the Pi.')
                run('git', 'reset', '--keep', old_head)
                self.replace(old_image, current)
            except Exception:
                record('needs_review', 'Rollback could not be verified. Follow the recovery steps on the Pi.')
            else:
                record('rolled_back', 'The previous bridge version is running again.')
            return
        record('complete', 'The new bridge version passed its health check.')


def read_state():
    try:
        return json.loads((STATE / 'status.json').read_text())
    except FileNotFoundError:
        return {'phase': 'idle'}


def write_state(value):
    temp = STATE / 'status.new'
    with temp.open('w') as f:
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(STATE / 'status.json')
    directory = os.open(STATE, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def send(sock, data):
    sock.sendall((json.dumps(data) + '\n').encode())


def read_line(stream):
    line = stream.readline(1025)
    if not line.endswith(b'\n') or len(line) > 1024:
        raise UpdateError('Invalid local update request.')
    return json.loads(line)


def handle(sock, stack):
    sock.settimeout(30)
    with sock.makefile('rb') as stream, (STATE / 'update.lock').open('a') as lock:
        request = read_line(stream)
        state = read_state()
        if request == {'action': 'status'} and state['phase'] not in ACTIVE:
            send(sock, state)
            return
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            idle = True
        except BlockingIOError:
            idle = False
        state = read_state()  # recheck after acquiring the lock
        if idle and state['phase'] in ACTIVE:
            if state['phase'] in ('preparing', 'ready'):
                state.update(phase='failed', message='Preparation was interrupted. Check for updates again.')
            else:
                state.update(phase='needs_review', message='The Pi restarted during installation. Verify the bridge and follow the recovery guide.')
            write_state(state)
        if request == {'action': 'status'}:
            send(sock, state)
            return
        if not idle or state['phase'] == 'needs_review':
            send(sock, {'phase': 'refused', 'message': 'Another update is active or needs review.'})
            return
        if set(request) != {'action', 'current', 'target'} or request['action'] != 'install':
            raise UpdateError('Invalid local update request.')
        current, target = request['current'], request['target']
        if version_key(target) <= version_key(current):
            raise UpdateError('Select a newer release.')
        def record(phase, message):
            write_state(dict(phase=phase, target=target, message=message))
        try:
            record('preparing', 'Downloading and verifying the release.')
            send(sock, read_state())
            install = Installation(stack)
            prepared = install.prepare(current, target)
            send(sock, {'phase': 'ready'})
            if read_line(stream) != {'action': 'restart'}:
                raise UpdateError('The bridge is busy. Finish its operation and try again.')
            install.apply(prepared, current, target, record)
        except Exception as exc:
            logging.exception('Bridge update stopped')
            record('failed', str(exc) if isinstance(exc, UpdateError) else 'Update preparation stopped. Check the network, disk space and Pi updater journal.')


if __name__ == '__main__':
    with socket.socket(fileno=0) as connection:
        handle(connection, sys.argv[1])
