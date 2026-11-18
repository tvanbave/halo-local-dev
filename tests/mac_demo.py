"""Explicit synthetic-only Mac demonstration; never uses the host Bluetooth stack."""
import asyncio
import copy
import os
from pathlib import Path
import signal
import tempfile
from unittest.mock import patch

from avionmesh_pi.bluez import BlueZTransport
from avionmesh_pi.protocol import derive_mcp_key
from avionmesh_pi.service import serve
from avionmesh_pi.ui import HTML
from support import synthetic_store
from test_service import INVENTORY
from wire_lab import Relay, WireBroker

TOKEN = 'synthetic-demo-token-not-a-real-secret'

async def main():
    if os.environ.get('AVIONMESH_SIMULATED_DEMO') != '1':
        raise SystemExit('This fixture requires explicit AVIONMESH_SIMULATED_DEMO=1')
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    broker = WireBroker()
    await broker.start()
    try:
        with tempfile.TemporaryDirectory(prefix='synthetic-demo-') as directory:
            root = Path(directory)
            phrase = 'synthetic-mac-demo-never-use-on-real-mesh'
            key = derive_mcp_key(phrase)
            relay = Relay(key)
            for name, value in [('key', phrase), ('token', TOKEN)]:
                path = root / name
                path.write_text(value)
                path.chmod(0o600)
            inventory = copy.deepcopy(INVENTORY)
            for device in inventory['devices']:
                device['name'] = 'Simulated ' + device['name']
            store = synthetic_store(root / 'state', key)
            store.import_inventory(inventory)
            store.close()
            config = {'relay_address':'00:11:22:33:44:55', 'node':'mac_demo',
                      'state_directory':str(root/'state'), 'mesh_passphrase_file':str(root/'key'),
                      'api_token_file':str(root/'token'), 'http_host':'0.0.0.0', 'http_port':8088,
                      'mqtt':{'host':'127.0.0.1', 'port':broker.port, 'tls':False}}
            page = HTML.replace('<main>', '<main><section><strong>Simulated demo — synthetic devices, no real Bluetooth or Home Assistant.</strong><p>Light brightness, color temperature, metadata and diagnostics are interactive. Controller programming and commissioning are unavailable. State resets when this demo restarts.</p></section>')
            page = page.replace('id="token" type="password"', 'id="token" value="'+TOKEN+'" type="password"')
            page = page.replace('Enter your token to load the bridge.', 'Synthetic token filled in. Click Connect / Refresh to load the demo.')
            def transport(*args, **kwargs):
                return BlueZTransport('00:11:22:33:44:55', scanner=relay, client_factory=relay.client)
            with patch('avionmesh_pi.service.BlueZTransport', side_effect=transport), patch('avionmesh_pi.server.HTML', page):
                task = asyncio.create_task(serve(config))
                stopping = asyncio.create_task(stop.wait())
                try:
                    print('Synthetic demo: HTTP :8088; fake GATT and container-loopback MQTT only.', flush=True)
                    done, _ = await asyncio.wait([task, stopping], return_when=asyncio.FIRST_COMPLETED)
                    if task in done:
                        await task
                finally:
                    task.cancel(); stopping.cancel()
                    await asyncio.gather(task, stopping, return_exceptions=True)
    finally:
        await broker.close()

if __name__ == '__main__':
    asyncio.run(main())
