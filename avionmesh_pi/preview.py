"""Loopback read-only UI, with no transport, broker, poll or TX admission."""
import argparse
import asyncio
import os
import secrets

from .protocol import derive_mcp_key
from .runtime import Runtime
from .service import private_text
from .storage import Store


class NoTransport:
    ready = False
    dropped = 0
    counts = {}
    async def send(self, frame):
        raise RuntimeError('Preview cannot transmit')
    async def connect(self):
        raise RuntimeError('Preview cannot connect')


async def preview(directory, key_file, port=18089):
    from aiohttp import web
    from .server import create_app
    os.umask(0o077)
    key = derive_mcp_key(private_text(key_file))
    store = Store(directory, key)
    runner = None
    try:
        runtime = Runtime(store, key, NoTransport(), node='pi_claim_lab')
        runtime.preview_only = True
        runtime.commissioning_only = True
        runner = web.AppRunner(create_app(runtime, secrets.token_urlsafe(48), read_only=True), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', port).start()
        await asyncio.Future()
    finally:
        try:
            if runner is not None:await runner.cleanup()
        finally:
            store.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-directory',required=True)
    parser.add_argument('--mesh-passphrase-file',required=True)
    parser.add_argument('--port',type=int,default=18089)
    args=parser.parse_args()
    if not 1<=args.port<=65535:parser.error('Invalid port')
    asyncio.run(preview(args.state_directory,args.mesh_passphrase_file,args.port))

if __name__=='__main__':main()
