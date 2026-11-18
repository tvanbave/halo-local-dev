"""Offline replay and private state tools; live service requires explicit opt-in."""
import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from .bridge import Bridge, load_devices


async def serve_until_stopped(config):
    """Translate process termination into one cancellation, including as PID 1."""
    from .service import serve
    return await run_until_stopped(serve(config))


async def run_until_stopped(work):
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(work)
    stopping = False
    installed = []

    def stop():
        nonlocal stopping
        if not stopping:
            stopping = True
            task.cancel()

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop)
            installed.append(sig)
        try:
            await task
        except asyncio.CancelledError:
            if not stopping:
                raise
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def replay(path):
    with Path(path).open(encoding="utf-8") as stream:
        header = json.loads(next(stream, ""))
        if (not isinstance(header, dict)
                or set(header) != {"format", "synthetic", "key_hex", "devices"}
                or type(header["format"]) is not int or header["format"] != 1
                or header["synthetic"] is not True):
            raise ValueError("Only version 1 synthetic fixture files are supported")
        bridge = Bridge(load_devices(header["devices"]), bytes.fromhex(header["key_hex"]))
        yield from bridge.discovery()
        for line in stream:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Expected replay record object")
            if row.get("kind") == "connection" and set(row) == {"kind", "ready", "now_ms"}:
                yield from bridge.connection(row["ready"], row["now_ms"])
            elif row.get("kind") == "frame" and set(row) == {"kind", "hex", "received_ms", "now_ms"}:
                yield from bridge.receive(bytes.fromhex(row["hex"]), row["received_ms"], row["now_ms"])
            elif row.get("kind") == "tick" and set(row) == {"kind", "now_ms"}:
                yield from bridge.availability(row["now_ms"])
            else:
                raise ValueError("Invalid replay record")
        yield {"mode": "offline_summary", "counters": dict(bridge.counts)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("replay", help="Replay a synthetic fixture offline")
    command.add_argument("fixture", type=Path)
    scene = commands.add_parser('scene-inspect', help='Inspect saved scene diagnostics offline; never send commands')
    scene.add_argument('capture', type=Path)
    scene.add_argument('--input-format', choices=('pi-json', 'esp-jsonl'), default='pi-json',
                       help='Saved Pi diagnostics JSON or owned ESP schema-2 collector JSONL')
    live = commands.add_parser("serve", help="Run the development live service (requires explicit opt-in)")
    live.add_argument("config", type=Path)
    live.add_argument("--allow-live", action="store_true")
    start = commands.add_parser('start', help='First-run setup and persistent Compose service with optional browser login')
    start.add_argument('--data-directory', type=Path, default=Path('/data'))
    start.add_argument('--allow-live', action='store_true')
    for name in ('login-info', 'healthcheck'):
        helper = commands.add_parser(name, help='Managed Compose installation helper')
        helper.add_argument('--data-directory', type=Path, default=Path('/data'))
    for name in ('sequence-status','sequence-admit','sequence-retire'):
        offline = commands.add_parser(name, help='Offline private-state TX history management; no network')
        offline.add_argument('--state-directory',type=Path,required=True)
        offline.add_argument('--mesh-passphrase-file',type=Path,required=True)
        if name == 'sequence-admit':
            offline.add_argument('document',type=Path)
    snapshot = commands.add_parser('state-backup', help='Create a credential-private offline snapshot; never restore')
    snapshot.add_argument('--state-directory', type=Path, required=True)
    snapshot.add_argument('destination', type=Path)
    inspect = commands.add_parser('state-inspect', help='Verify a private snapshot without loading operational state')
    inspect.add_argument('backup', type=Path)
    recovery = commands.add_parser('state-recovery-plan', help='Compare private snapshots; never restore or authorize startup')
    recovery.add_argument('candidate', type=Path)
    recovery.add_argument('--reference', type=Path, help='A separately captured current stopped-state snapshot')
    args = parser.parse_args(argv)
    try:
        if args.command == 'scene-inspect':
            from .scene_research import inspect_file
            print(json.dumps(inspect_file(args.capture, input_format=args.input_format), indent=2, sort_keys=True))
            return 0
        if args.command in ('start', 'login-info', 'healthcheck'):
            from .quickstart import SetupError, start, healthcheck
            from .service import private_text
            try:
                if args.command == 'login-info':
                    print('Username: halo')
                    print('Password: ' + private_text(args.data_directory / 'ui-password'))
                elif args.command == 'healthcheck':
                    healthcheck(args.data_directory)
                elif not args.allow_live:
                    print('Live setup requires --allow-live', file=sys.stderr)
                    return 2
                else:
                    os.umask(0o077)
                    asyncio.run(run_until_stopped(start(args.data_directory,
                        host=os.environ.get('HALO_HOST') or None,
                        port=int(os.environ.get('HALO_PORT', '18089')),
                        adapter=os.environ.get('HALO_ADAPTER', 'hci0'))))
            except SetupError as exc:
                print('Setup: ' + str(exc), file=sys.stderr)
                return 2
            return 0
        if args.command in ('state-backup', 'state-inspect', 'state-recovery-plan'):
            from .backup import BackupError, create_backup, inspect_backup, recovery_plan
            try:
                if args.command == 'state-backup':
                    result = create_backup(args.state_directory, args.destination)
                elif args.command == 'state-inspect':
                    result = inspect_backup(args.backup)
                else:
                    result = recovery_plan(args.candidate, args.reference)
            except BackupError as exc:
                print('Operation failed [' + exc.code + ']: ' + str(exc), file=sys.stderr)
                return 2
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command.startswith('sequence-'):
            from .storage import Store
            from .protocol import derive_mcp_key
            from .service import private_text
            from .migration import status, admit, retire
            os.umask(0o077)
            store = Store(args.state_directory,derive_mcp_key(private_text(args.mesh_passphrase_file)))
            try:
                if args.command == 'sequence-status':
                    result = status(store)
                elif args.command == 'sequence-retire':
                    result = retire(store)
                else:
                    if args.document.stat().st_size > 8192:
                        raise ValueError('History document too large')
                    result = admit(store,json.loads(args.document.read_text()))
                print(json.dumps(result,sort_keys=True))
            finally:
                store.close()
            return 0
        if args.command == "serve":
            if not args.allow_live:
                print("Live service requires --allow-live after hardware coordination", file=sys.stderr)
                return 2
            from .service import load_config
            asyncio.run(serve_until_stopped(load_config(args.config)))
            return 0
        for item in replay(args.fixture):
            print(json.dumps(item, sort_keys=True, separators=(",", ":")))
    except (ValueError, KeyError, TypeError, OSError, StopIteration, RuntimeError):
        # Do not include input values, file contents, keys or credentials.
        print("Operation failed: invalid configuration or unreadable input", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
