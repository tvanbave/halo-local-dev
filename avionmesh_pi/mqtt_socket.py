"""Narrow socket-lifetime adapter for the pinned aiomqtt 2.5.1 client.

Its write-registration callback queues a numeric fd for later registration.
A disconnect can close/reuse that fd before the event loop handles the request.
Validate the original socket at execution time instead. This does not install
an exception handler or suppress unrelated event-loop failures.
"""
import asyncio


class GuardedSocketWrites:
    """Mixin using aiomqtt's private callback hook; review on dependency updates."""
    _connection_error = RuntimeError

    def _on_socket_register_write(self, client, userdata, sock):
        descriptor = sock.fileno()

        def current():
            return descriptor >= 0 and sock.fileno() == descriptor and client.socket() is sock

        def failed(error):
            if not self._connected.done():
                # Before CONNACK, __aenter__ awaits _connected, not _disconnected.
                self._connected.set_exception(self._connection_error('MQTT socket setup failed'))
            elif not self._connected.cancelled() and self._connected.exception() is None and not self._disconnected.done():
                self._disconnected.set_exception(error)

        def write_ready():
            try:
                if current():
                    client.loop_write()
            except Exception as error:
                failed(error)

        def register_current():
            try:
                if current() and client.want_write():
                    self._loop.add_writer(descriptor, write_ready)
            except Exception as error:
                # An unexpected registration failure ends this connection through
                # aiomqtt's usual error path; it is not silently hidden globally.
                failed(error)

        self._loop.call_soon_threadsafe(register_current)


def make_client(**kwargs):
    import aiomqtt

    class Client(GuardedSocketWrites, aiomqtt.Client):
        _connection_error = aiomqtt.MqttError

        async def _release_transport(self):
            try:
                self._client._sock_close()
            finally:
                try:
                    if self._misc_task is not None:
                        self._misc_task.cancel()
                        await asyncio.gather(self._misc_task, return_exceptions=True)
                finally:
                    if self._lock.locked():
                        self._lock.release()

        async def __aenter__(self):
            if self._lock.locked():
                # Preserve upstream's reentrancy refusal without closing the
                # transport owned by the already-active context.
                return await super().__aenter__()
            if getattr(self, '_entering', False):
                raise aiomqtt.MqttReentrantError('The client context manager is not reentrant')
            self._entering = True
            loop = asyncio.get_running_loop()
            connected_in_thread = loop.create_future()
            original_connect = self._client_connect

            def finished():
                if not connected_in_thread.done():
                    connected_in_thread.set_result(None)

            def tracked_connect():
                try:
                    original_connect()
                finally:
                    loop.call_soon_threadsafe(finished)

            self._client_connect = tracked_connect
            enter = asyncio.create_task(super().__aenter__())

            async def failed_enter_cleanup():
                if not enter.done():
                    # Paho's connect runs in an executor. Cancelling its asyncio
                    # waiter does not stop that thread; closing first can leave a
                    # later-created socket and queued writer behind. Keep the
                    # entry task owned until the connect thread finishes, then
                    # cancel only the remaining CONNACK wait.
                    await asyncio.wait([enter, connected_in_thread], return_when=asyncio.FIRST_COMPLETED)
                    enter.cancel()
                await asyncio.gather(enter, return_exceptions=True)
                if self._connected.done() and not self._connected.cancelled():
                    self._connected.exception()
                await self._release_transport()

            try:
                return await asyncio.shield(enter)
            except BaseException:
                # A failed __aenter__ does not get an __aexit__. Release resources
                # after the connect executor has returned, including cancellation.
                cleanup = asyncio.create_task(failed_enter_cleanup())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        pass
                cleanup.result()
                raise
            finally:
                self._client_connect = original_connect
                self._entering = False

        async def __aexit__(self, *args):
            try:
                return await super().__aexit__(*args)
            finally:
                # Upstream returns early when _disconnected is already done.
                # A local I/O failure still needs its socket/task fully closed.
                await self._release_transport()

    return Client(**kwargs)
