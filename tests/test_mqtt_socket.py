import asyncio
import importlib.util
import socket
import threading
import unittest
from types import SimpleNamespace

from avionmesh_pi.mqtt_socket import GuardedSocketWrites, make_client


class Socket:
    def __init__(self, descriptor=17):self.descriptor=descriptor
    def fileno(self):return self.descriptor


class Loop:
    def __init__(self):self.pending=[];self.writers={};self.error=None
    def call_soon_threadsafe(self,callback):self.pending.append(callback)
    def add_writer(self,descriptor,callback):
        if self.error:raise self.error
        self.writers[descriptor]=callback
    def drain(self):
        for callback in self.pending:callback()
        self.pending.clear()


class SocketLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop=Loop();self.sock=Socket();self.active=self.sock;self.wanted=True;self.writes=0
        self.client=SimpleNamespace(socket=lambda:self.active,want_write=lambda:self.wanted,loop_write=self.write)
        self.adapter=GuardedSocketWrites();self.adapter._loop=self.loop
        self.adapter._disconnected=asyncio.get_running_loop().create_future()
        self.adapter._connected=asyncio.get_running_loop().create_future()
        self.adapter._connected.set_result(None)
    def write(self):self.writes+=1
    def schedule(self):self.adapter._on_socket_register_write(self.client,None,self.sock)
    async def test_closed_socket_never_registers_queued_descriptor(self):
        self.schedule();self.sock.descriptor=-1;self.active=None;self.loop.drain()
        self.assertEqual(self.loop.writers,{})
        self.assertFalse(self.adapter._disconnected.done())
    async def test_reused_descriptor_does_not_replace_another_socket_writer(self):
        self.schedule();other=lambda:None;self.loop.writers[17]=other
        self.active=Socket(17);self.loop.drain()
        self.assertIs(self.loop.writers[17],other)
    async def test_drained_queue_does_not_resurrect_unregistered_writer(self):
        self.schedule();self.wanted=False;self.loop.drain()
        self.assertEqual(self.loop.writers,{})
    async def test_current_socket_writes_but_stale_ready_callback_does_not(self):
        self.schedule();self.loop.drain();ready=self.loop.writers[17]
        ready();self.assertEqual(self.writes,1)
        self.active=Socket(17);ready();self.assertEqual(self.writes,1)
    async def test_registration_error_reaches_connection_failure(self):
        error=OSError('synthetic selector failure');self.loop.error=error
        self.schedule();self.loop.drain()
        self.assertIs(self.adapter._disconnected.exception(),error)
    async def test_write_error_reaches_connection_failure(self):
        error=OSError('synthetic broken write')
        def broken():raise error
        self.client.loop_write=broken;self.schedule();self.loop.drain();self.loop.writers[17]()
        self.assertIs(self.adapter._disconnected.exception(),error)
    async def test_registration_failure_before_connack_resolves_connect_future(self):
        self.adapter._connected=asyncio.get_running_loop().create_future()
        self.loop.error=OSError('synthetic registration failure');self.schedule();self.loop.drain()
        self.assertIsInstance(self.adapter._connected.exception(),RuntimeError)
        self.assertFalse(self.adapter._disconnected.done())
    @unittest.skipUnless(importlib.util.find_spec('aiomqtt'),'Requires installed MQTT dependency')
    async def test_real_client_binds_guarded_callback_without_connecting(self):
        client=make_client(hostname='unused.invalid')
        self.assertIsInstance(client,GuardedSocketWrites)
        self.assertEqual(client._client.on_socket_register_write,client._on_socket_register_write)
    @unittest.skipUnless(importlib.util.find_spec('aiomqtt'),'Requires installed MQTT dependency')
    async def test_real_selector_closed_socket_does_not_emit_loop_error(self):
        loop=asyncio.get_running_loop();old=loop.get_exception_handler();errors=[]
        loop.set_exception_handler(lambda loop,context:errors.append(context))
        left,right=socket.socketpair()
        try:
            client=make_client(hostname='unused.invalid')
            peer=SimpleNamespace(socket=lambda:left,want_write=lambda:True,loop_write=lambda:None)
            client._on_socket_register_write(peer,None,left)
            left.close();await asyncio.sleep(0);await asyncio.sleep(0)
            self.assertEqual(errors,[])
        finally:
            left.close();right.close();loop.set_exception_handler(old)
    @unittest.skipUnless(importlib.util.find_spec('aiomqtt'),'Requires installed MQTT dependency')
    async def test_failed_enter_closes_socket_and_releases_lock(self):
        import aiomqtt
        left,right=socket.socketpair()
        client=make_client(hostname='unused.invalid')
        def broken():raise OSError('synthetic setup failure')
        peer=SimpleNamespace(socket=lambda:left,want_write=broken,_sock_close=left.close)
        client._client=peer
        client._client_connect=lambda:client._on_socket_register_write(peer,None,left)
        try:
            with self.assertRaises(aiomqtt.MqttError):await client.__aenter__()
            self.assertEqual(left.fileno(),-1)
            self.assertFalse(client._lock.locked())
        finally:left.close();right.close()
    @unittest.skipUnless(importlib.util.find_spec('aiomqtt'),'Requires installed MQTT dependency')
    async def test_cancelled_enter_waits_for_connect_thread_then_releases_transport(self):
        left,right=socket.socketpair();client=make_client(hostname='unused.invalid')
        loop=asyncio.get_running_loop();started=asyncio.Event();finish=threading.Event();closed=[]
        def close():closed.append(True);left.close()
        peer=SimpleNamespace(socket=lambda:left,_sock_close=close)
        client._client=peer
        def connect():
            loop.call_soon_threadsafe(started.set)
            if not finish.wait(5):raise RuntimeError('Synthetic test gate was not released')
        client._client_connect=connect
        task=asyncio.create_task(client.__aenter__())
        try:
            await asyncio.wait_for(started.wait(),1)
            task.cancel();await asyncio.sleep(0);await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(closed,[])
            self.assertTrue(client._lock.locked())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,1)
            self.assertEqual(left.fileno(),-1)
            self.assertFalse(client._lock.locked())
            self.assertFalse(client._entering)
        finally:
            finish.set();task.cancel();await asyncio.gather(task,return_exceptions=True)
            left.close();right.close()
    @unittest.skipUnless(importlib.util.find_spec('aiomqtt'),'Requires installed MQTT dependency')
    async def test_timed_enter_probe_cancels_connack_wait_without_leaking_socket(self):
        left,right=socket.socketpair();client=make_client(hostname='unused.invalid')
        client._client=SimpleNamespace(socket=lambda:left,_sock_close=left.close)
        client._client_connect=lambda:None
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(client.__aenter__(),.02)
            self.assertEqual(left.fileno(),-1)
            self.assertFalse(client._lock.locked())
            self.assertFalse(client._entering)
        finally:left.close();right.close()
    @unittest.skipUnless(importlib.util.find_spec('aiomqtt'),'Requires installed MQTT dependency')
    async def test_established_failure_closes_socket_and_maintenance_task(self):
        for registration in (False,True):
            with self.subTest(registration=registration):
                left,right=socket.socketpair();client=make_client(hostname='unused.invalid')
                loop=Loop();client._loop=loop;client._connected.set_result(None)
                await client._lock.acquire()
                client._misc_task=asyncio.create_task(asyncio.Event().wait())
                error=OSError('synthetic established I/O failure')
                def broken():raise error
                peer=SimpleNamespace(socket=lambda:left,want_write=lambda:True,
                                     loop_write=broken,_sock_close=left.close)
                client._client=peer
                try:
                    if registration:loop.error=error
                    client._on_socket_register_write(peer,None,left);loop.drain()
                    if not registration:loop.writers[left.fileno()]()
                    self.assertIs(client._disconnected.exception(),error)
                    await client.__aexit__(OSError,error,None)
                    self.assertEqual(left.fileno(),-1)
                    self.assertTrue(client._misc_task.done())
                    self.assertFalse(client._lock.locked())
                finally:
                    left.close();right.close();client._misc_task.cancel()
                    await asyncio.gather(client._misc_task,return_exceptions=True)
    @unittest.skipUnless(importlib.util.find_spec('aiomqtt'),'Requires installed MQTT dependency')
    async def test_close_failure_still_cancels_task_and_releases_lock(self):
        client=make_client(hostname='unused.invalid');client._connected.set_result(None)
        client._disconnected.set_result(None);await client._lock.acquire()
        client._misc_task=asyncio.create_task(asyncio.Event().wait())
        def broken():raise OSError('synthetic close callback failure')
        client._client=SimpleNamespace(_sock_close=broken)
        with self.assertRaises(OSError):await client.__aexit__(None,None,None)
        self.assertTrue(client._misc_task.done())
        self.assertFalse(client._lock.locked())
    @unittest.skipUnless(importlib.util.find_spec('aiomqtt'),'Requires installed MQTT dependency')
    async def test_reentrant_entry_does_not_close_active_context(self):
        client=make_client(hostname='unused.invalid');closed=[]
        client._client=SimpleNamespace(_sock_close=lambda:closed.append(True))
        await client._lock.acquire()
        try:
            with self.assertRaises(Exception) as result:await client.__aenter__()
            self.assertEqual(type(result.exception).__name__,'MqttReentrantError')
            self.assertEqual(closed,[])
            self.assertTrue(client._lock.locked())
        finally:client._lock.release()
