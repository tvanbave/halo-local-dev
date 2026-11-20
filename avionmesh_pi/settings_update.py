"""Owner-triggered release checks and restart admission for the host helper."""
import asyncio
import json
import time

from . import __version__
from . import release_update as release


class UpdateRequestError(Exception):
    def __init__(self, message, status=409):
        self.status = status
        super().__init__(message)


async def open_helper():
    return await asyncio.wait_for(asyncio.open_unix_connection(release.SOCKET, limit=2048), 4)


async def write(writer, value):
    writer.write((json.dumps(value) + '\n').encode())
    await writer.drain()


async def response(reader, timeout):
    line = await asyncio.wait_for(reader.readline(), timeout)
    if not line:
        raise ConnectionError('Update helper disconnected')
    return json.loads(line)


class BridgeUpdates:
    def __init__(self, runtime, busy):
        self.runtime = runtime
        self.busy = busy
        self.latest = None
        self.checked = 0
        self.task = None
        self.check_lock = asyncio.Lock()
        self.restarting = False

    async def status(self):
        try:
            reader, writer = await open_helper()
            try:
                await write(writer, {'action': 'status'})
                job = await response(reader, 5)
            finally:
                writer.close()
                await writer.wait_closed()
        except (OSError, ValueError, asyncio.TimeoutError):
            job = {'phase': 'unavailable'}
        latest = self.latest
        return dict(installed=__version__, latest=latest, job=job,
                    available=bool(latest and release.version_key(latest) > release.version_key(__version__)))

    async def check(self):
        async with self.check_lock:
            if self.latest is None or time.monotonic() - self.checked > 30:
                self.latest = None
                try:
                    self.latest = await asyncio.to_thread(release.latest_release)
                except (OSError, ValueError, release.UpdateError, KeyError, TypeError) as exc:
                    raise UpdateRequestError('Could not check GitHub for a bridge release. Try again when the Pi has internet access.', 502) from exc
                self.checked = time.monotonic()
        return await self.status()

    async def install(self, data):
        if (not isinstance(data, dict) or set(data) != {'version', 'confirm_restart'}
                or data['confirm_restart'] is not True):
            raise UpdateRequestError('Confirm the version and bridge restart first.', 400)
        if (not self.latest or data['version'] != self.latest or time.monotonic() - self.checked > 900
                or release.version_key(self.latest) <= release.version_key(__version__)):
            raise UpdateRequestError('Check for updates again before installing.')
        if self.task and not self.task.done() or self.busy():
            raise UpdateRequestError('Finish the current bridge operation before updating.')
        writer = None
        try:
            reader, writer = await open_helper()
            await write(writer, {'action': 'install', 'current': __version__, 'target': self.latest})
            job = await response(reader, 6)
            if job.get('phase') != 'preparing':
                raise UpdateRequestError(job.get('message', 'The Pi refused the update.'))
            self.task = asyncio.create_task(self._handoff(reader, writer))
            writer = None  # the task owns this connection through restart
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            raise UpdateRequestError('Pi updater unavailable. Check its status before trying again.', 503) from exc
        finally:
            if writer:
                writer.close()
        return dict(installed=__version__, latest=self.latest, available=True, job=job)

    async def _handoff(self, reader, writer):
        try:
            if (await response(reader, 1800)).get('phase') != 'ready':
                return
            # Nothing can enter a new handler between the idle check and maintenance flag.
            async with self.runtime.foreground_request():
                if self.busy(own_request=True):
                    await write(writer, {'action': 'cancel'})
                    return
                self.restarting = self.runtime.maintenance = True
                await write(writer, {'action': 'restart'})
            # The socket closes when the bounded systemd job exits, also on failure.
            await reader.read()
        except (OSError, RuntimeError, ValueError, asyncio.TimeoutError):
            pass  # the host owns the final status even if this process restarts
        finally:
            if self.restarting:
                self.restarting = self.runtime.maintenance = False
            writer.close()

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
