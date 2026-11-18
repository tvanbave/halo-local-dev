"""Private, revision-checked MQTT settings and broker-only reconfiguration.

The private override is separate from public inventory/SQLite backups. Applying
it never restarts Bluetooth or changes mesh identity, sequence history or lights.
"""
import asyncio
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import stat
import uuid

from .errors import OperationNotStarted
from .mqtt import MqttService


class SettingsError(ValueError):
    def __init__(self, code, message, status=400):
        self.code, self.status = code, status
        super().__init__(message)


def _fail(message, code='invalid_mqtt_settings', status=400):
    raise SettingsError(code, message, status)


def _text(value, maximum, *, empty=False):
    return (isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _broker(value, enabled):
    if not isinstance(value, dict) or set(value) - {'host', 'port', 'username', 'password', 'tls', 'ca_file'}:
        _fail('Invalid broker settings.')
    result = copy.deepcopy(value)
    result.setdefault('host', '')
    result.setdefault('username', '')
    result.setdefault('tls', True)
    if type(result['tls']) is not bool:
        _fail('TLS must be enabled or disabled.')
    result.setdefault('port', 8883 if result['tls'] else 1883)
    host = result['host']
    if not _text(host, 253, empty=not enabled) or any(c.isspace() or c in '/@\\' for c in host):
        _fail('Enter a broker hostname or IP address, without a URL scheme.')
    if host:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            labels = host.rstrip('.').split('.')
            if any(not re.fullmatch(r'[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?', label)
                   for label in labels):
                _fail('Enter a valid broker hostname or IP address.')
    if type(result['port']) is not int or not 1 <= result['port'] <= 65535:
        _fail('Broker port must be between 1 and 65535.')
    if not _text(result['username'], 256, empty=True):
        _fail('Invalid broker username.')
    if 'password' in result and not _text(result['password'], 4096):
        _fail('Enter a nonempty broker password, or choose to clear it.')
    if 'ca_file' in result and (not _text(result['ca_file'], 4096) or not Path(result['ca_file']).is_absolute()):
        _fail('Invalid configured certificate path.')
    return result


def _document(value):
    if (not isinstance(value, dict) or set(value) != {'format', 'revision', 'enabled', 'broker'}
            or type(value['format']) is not int or value['format'] != 1
            or type(value['enabled']) is not bool
            or not isinstance(value['revision'], str)
            or not re.fullmatch('[0-9a-f]{32}', value['revision'])):
        _fail('Stored MQTT settings are invalid.', 'invalid_stored_mqtt_settings')
    return dict(value, broker=_broker(value['broker'], value['enabled']))


class MqttSettings:
    filename = 'mqtt-settings.json'
    admission_timeout = 20
    test_timeout = 8

    def __init__(self, runtime, state_directory, initial_config=None, client_factory=None):
        self.runtime, self.client_factory = runtime, client_factory
        self._change = asyncio.Lock()
        self._closed = asyncio.Event()
        self._running = False
        self._service = self._task = self._poll_task = None
        self.error = None
        self._directory = Path(state_directory)
        self._fd = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(self._fd)
            if info.st_mode & 0o077 or info.st_uid != os.geteuid():
                _fail('MQTT settings require an owner-private state directory.')
            stored = self._read()
            self.source = 'settings' if stored is not None else 'service'
            self._value = stored or {'format': 1, 'revision': uuid.uuid4().hex,
                'enabled': initial_config is not None,
                'broker': _broker(initial_config or {}, initial_config is not None)}
            self._flags()
        except BaseException:
            os.close(self._fd)
            self._fd = None
            raise

    def _flags(self):
        self.runtime.mqtt_enabled = self._value['enabled']
        self.runtime.commissioning_only = not self._value['enabled']

    def _read(self):
        try:
            fd = os.open(self.filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=self._fd)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_size > 65536):
                _fail('Stored MQTT settings must be a private regular file.', 'invalid_stored_mqtt_settings')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                data = stream.read(65537)
            if len(data) > 65536:
                _fail('Stored MQTT settings are too large.', 'invalid_stored_mqtt_settings')
            try:
                return _document(json.loads(data))
            except (UnicodeError, json.JSONDecodeError):
                _fail('Stored MQTT settings cannot be read.', 'invalid_stored_mqtt_settings')
        finally:
            os.close(fd)

    def _persist(self, value):
        temporary = '.mqtt-settings-' + uuid.uuid4().hex + '.tmp'
        fd = None
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=self._fd)
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write((json.dumps(value, separators=(',', ':')) + '\n').encode())
                stream.flush()
                os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(temporary, self.filename, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            try:
                os.fsync(self._fd)
            except OSError:
                # The rename already happened. Adopt this revision rather than
                # claiming the old configuration still matches the durable file.
                return False
            return True
        except OSError:
            _fail('MQTT settings could not be saved. Existing settings were kept.',
                  'mqtt_settings_write_failed', 503)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self._fd)
            except FileNotFoundError:
                pass

    def public(self):
        broker = self._value['broker']
        return {key: broker[key] for key in ('host', 'port', 'username', 'tls')} | {
            'revision': self._value['revision'], 'enabled': self._value['enabled'],
            'password_set': bool(broker.get('password')), 'connected': bool(self.runtime.mqtt_ready),
            'source': self.source, 'error': self.error}

    def _proposed(self, payload):
        fields = {'revision', 'enabled', 'host', 'port', 'username', 'tls', 'password_action'}
        if (not isinstance(payload, dict) or not fields <= set(payload)
                or set(payload) - fields - {'password'} or type(payload['enabled']) is not bool):
            _fail('Expected the reviewed MQTT settings fields.')
        if payload['revision'] != self._value['revision']:
            _fail('MQTT settings changed. Reload them before saving or testing.', 'mqtt_settings_changed', 409)
        action = payload['password_action']
        if action not in ('keep', 'replace', 'clear'):
            _fail('Choose whether to keep, replace or clear the password.')
        if ('password' in payload) != (action == 'replace'):
            _fail('Supply a password only when replacing it.')
        broker = {key: payload[key] for key in ('host', 'port', 'username', 'tls')}
        previous = self._value['broker']
        same_identity = all(broker[key] == previous[key] for key in ('host', 'port', 'username', 'tls'))
        if action == 'replace':
            broker['password'] = payload['password']
        elif action == 'keep' and previous.get('password'):
            if not same_identity:
                _fail('Re-enter or clear the password when changing the broker, port, username or TLS.',
                      'mqtt_password_reentry_required')
            broker['password'] = previous['password']
        if (previous.get('ca_file') and all(broker[key] == previous[key] for key in ('host', 'port', 'tls'))):
            broker['ca_file'] = previous['ca_file']
        broker = _broker(broker, payload['enabled'])
        if payload['enabled']:
            self._tls_context(broker)
        return {'format': 1, 'revision': uuid.uuid4().hex, 'enabled': payload['enabled'], 'broker': broker}

    @staticmethod
    def _tls_context(broker):
        try:
            return ssl.create_default_context(cafile=broker.get('ca_file')) if broker['tls'] else None
        except (OSError, ValueError, ssl.SSLError):
            _fail('The configured broker certificate could not be loaded.', 'mqtt_certificate_invalid')

    async def test(self, payload):
        async with self._change:
            self._available()
            proposed = self._proposed(payload)
            if not proposed['enabled']:
                _fail('Enter broker settings and enable this connection test.')
            broker = proposed['broker']
            context = self._tls_context(broker)
            from .mqtt_socket import make_client
            factory = self.client_factory or make_client
            async def connect():
                # A distinct identifier cannot evict the active bridge client.
                # No will, publish, subscription, HA discovery or radio access.
                async with factory(hostname=broker['host'], port=broker['port'],
                    identifier='avionmesh-test-' + uuid.uuid4().hex[:12],
                    username=broker['username'] or None, password=broker.get('password'),
                    tls_context=context, timeout=self.test_timeout,
                    max_queued_incoming_messages=1):
                    pass
            try:
                await asyncio.wait_for(connect(), self.test_timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                return {'connected': False, 'saved': False, 'published': False, 'subscribed': False,
                        'message': 'Connection failed. Check the broker address, port, credentials and TLS.'}
            return {'connected': True, 'saved': False, 'published': False, 'subscribed': False,
                    'message': 'Broker accepted the connection. No messages were published; settings were not saved.'}

    def _available(self):
        if getattr(self.runtime, 'maintenance', False):
            _fail('The bridge is restarting for restore.', 'restore_in_progress', 409)
        if self._fd is None or self._closed.is_set():
            _fail('MQTT settings are shutting down. Reload the page.', 'mqtt_settings_unavailable', 503)

    async def save(self, payload):
        async with self._change:
            self._available()
            proposed = self._proposed(payload)
            try:
                async with self.runtime.foreground_request(timeout=self.admission_timeout):
                    try:
                        await asyncio.wait_for(self.runtime.operation.acquire(), self.admission_timeout)
                    except asyncio.TimeoutError:
                        raise OperationNotStarted('mesh_busy') from None
                    try:
                        # Once durable replacement begins, finish the broker-only
                        # switch even if the HTTP caller disconnects. The changed
                        # revision prevents blindly repeating an uncertain save.
                        task = asyncio.create_task(self._save_admitted(proposed))
                        try:
                            return await asyncio.shield(task)
                        except asyncio.CancelledError:
                            while not task.done():
                                try:
                                    await asyncio.shield(task)
                                except asyncio.CancelledError:
                                    pass
                            task.result()
                            raise
                    finally:
                        self.runtime.operation.release()
            except OperationNotStarted as error:
                _fail(str(error), error.code, 409)

    async def _save_admitted(self, proposed):
        self._available()
        if self.runtime.store.journals(pending=True):
            raise OperationNotStarted('pending_operation')
        durable = self._persist(proposed)
        self._value, self.source = proposed, 'settings'
        await self._apply()
        return {'saved': True, 'applied': True, 'settings': self.public(), 'durability_confirmed': durable,
                'message': ('Settings saved. The connection status updates independently of Bluetooth.' if durable else
                            'Settings applied, but disk durability was not confirmed. Reload settings before another change.')}

    def _worker_done(self, task):
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and task in (self._task, self._poll_task):
            self.error = 'MQTT stopped unexpectedly. Save the settings again to reconnect.'
            self.runtime.mqtt_connection(False)

    async def _stop(self):
        service, task, poll = self._service, self._task, self._poll_task
        self._service = self._task = self._poll_task = None
        if service is not None:
            service._connection(False)
        for worker in (task, poll):
            if worker is not None:
                worker.cancel()
        await asyncio.gather(*(worker for worker in (task, poll) if worker is not None), return_exceptions=True)
        if service is not None and service.invalidate_session in self.runtime.session_observers:
            self.runtime.session_observers.remove(service.invalidate_session)
        self.runtime.sink = lambda message: None
        self.runtime.mqtt_metrics = lambda: {}
        self.runtime.mqtt_connection(False)

    async def _apply(self):
        await self._stop()
        self._flags()
        self.error = None
        if self._running and self._value['enabled']:
            broker = copy.deepcopy(self._value['broker'])
            if not broker.get('username'):
                broker.pop('username', None)
            self._service = MqttService(self.runtime, broker, self.client_factory)
            self.runtime.sink = self._service.outbox.put
            self._task = asyncio.create_task(self._service.run())
            self._poll_task = asyncio.create_task(self.runtime.poll())
            self._task.add_done_callback(self._worker_done)
            self._poll_task.add_done_callback(self._worker_done)

    async def run(self):
        started = False
        try:
            async with self._change:
                self._available()
                if self._running:
                    _fail('MQTT settings manager already started.')
                self._running = True
                started = True
                await self._apply()
            await self._closed.wait()
        finally:
            if started:
                self._running = False
                await self._stop()

    async def close(self):
        async with self._change:
            self._closed.set()
            self._running = False
            await self._stop()
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
