"""Management HTTP API with explicit access modes. No listeners start on import."""
import asyncio
import hmac
import ipaddress
import json
import logging
import uuid
from pathlib import Path

from aiohttp import web, ClientError

from .bridge import Bridge, load_devices
from .keypad import KeypadService, ProtectedAssignmentRefused, plan_edit

SCENE_QUIET_TEST_SECONDS = 3

KEYPAD_SERVICE_KEY = web.AppKey('keypad_service', KeypadService)
# RequestKey was introduced after the oldest supported aiohttp release.
RECEIVED_MS_KEY = web.RequestKey('control_received_ms', int) if hasattr(web, 'RequestKey') else 'control_received_ms'
from .association import AssociationError
from .commissioning import Commissioning, ClaimValidationError
from .lifecycle import Lifecycle, reload_inventory
from .management import revision, edit_metadata, recovery_advice, cancel_unattempted
from .onboarding import KeypadOnboarding
from .accessory import AccessoryOnboarding
from .anyplace import AnyplaceOnboarding
from .membership import Membership
from .rooms import Rooms
from .controller_workflow import controller_workflows
from .ha_readiness import assess as assess_ha
from .identity import examine
from .dimmer import DimmerEditor
from .errors import OperationNotStarted
from .cloud import CloudSync, CloudImport, CloudImportError
from .relay_settings import RelayConflict, relay_status, save_preference
from .bluez import BlueZTransport
from .bootstrap import AutoBlueZTransport, RelayScanBusy
from .mqtt_settings import SettingsError

from .ui import HTML, management_html
from .bridge import CONTROLLERS, LIGHTS, COLOR_LIGHTS
from . import __version__
from .settings_update import BridgeUpdates, UpdateRequestError
from .backup_manager import BackupManagerError, MAX_HTTP_BYTES
from .installation_backup import InstallationBackupError
from .restore_state import RestoreStateError

# Redesigned trusted-LAN home page (package data). The previous page stays at /classic.
_WEB = Path(__file__).resolve().parent / 'web'
_WEB_ASSETS = {'/ui.css': ('ui.css', 'text/css'), '/ui.js': ('ui.js', 'text/javascript'),
               '/settings.js': ('settings.js', 'text/javascript'), '/ops.js': ('ops.js', 'text/javascript'), '/backup.js': ('backup.js', 'text/javascript')}
# Existing ordinary-claim models offered by the redesigned page. Controller
# configuration remains separately gated; pairing never implicitly sets it up.
PAIRABLE_TYPES = (93, 134, 127, 126, 91, 81)
ROOM_NAME_LIMIT = 40
ROOM_LIMIT = 64


class _JobKeypadProgress(dict):
    """Associate each display phase with its actual writer and radio session."""
    def __init__(self, runtime):
        super().__init__()
        self.runtime, self.owners = runtime, {}

    def __setitem__(self, target, phase):
        super().__setitem__(target, phase)
        self.owners[target] = (asyncio.current_task(), self.runtime.generation)

    def for_job(self, target, job, task, operation, instance):
        if (job.get('state') != 'running' or task is None or task.done()
                or operation not in ('keypad_read', 'keypad_save', 'keypad_setup', 'keypad_recovery')
                or self.owners.get(target) != (task, self.runtime.generation)
                or not self.runtime.mesh_ready or not self.runtime.transport.ready
                or self.get(target) not in ('cooldown', 'waiting_for_wake', 'settling', 'reading', 'verified', 'needs_review')):
            return None
        return {'phase': self[target], 'job_id': job['id'], 'target': target,
                'product_id': 126, 'operation': operation, 'instance': instance}


class _JobAccessoryProgress(_JobKeypadProgress):
    """Only the actual running Accessory setup/recovery may request input."""
    def for_job(self, target, job, task, operation, instance):
        if (job.get('state') != 'running' or task is None or task.done()
                or operation not in ('accessory_setup', 'accessory_native_setup', 'accessory_recovery')
                or self.owners.get(target) != (task, self.runtime.generation)
                or not self.runtime.mesh_ready or not self.runtime.transport.ready
                or self.get(target) not in ('preparing', 'writing', 'awaiting_input', 'observed')):
            return None
        return {'phase': self[target], 'job_id': job['id'], 'target': target,
                'product_id': 91, 'operation': operation, 'instance': instance}


def _clean_room(value):
    if not isinstance(value, str):
        raise ValueError('Room must be text')
    room = ' '.join(value.split())
    if len(room) > ROOM_NAME_LIMIT or any(not ch.isprintable() for ch in room):
        raise ValueError('Room names are at most 40 printable characters')
    return room


def _trusted_lan_host(value):
    """Require one canonical RFC1918 address and an explicit TCP port."""
    if not isinstance(value, str) or value.count(':') != 1:
        raise ValueError('Trusted LAN access requires an explicit private IPv4:port')
    host, port = value.split(':')
    try:
        address = ipaddress.IPv4Address(host)
        valid_port = port.isascii() and port.isdecimal() and 1 <= int(port) <= 65535 and str(int(port)) == port
    except ValueError:
        raise ValueError('Trusted LAN access requires an explicit private IPv4:port') from None
    private = any(address in ipaddress.IPv4Network(network)
                  for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    if not private or not valid_port:
        raise ValueError('Trusted LAN access requires an explicit private IPv4:port')
    return host if int(port) == 80 else value


def create_app(runtime, token, *, read_only=False, trusted_lan_no_auth=False, allowed_host=None,
               cloud_import=None, browser_login=False):
    if type(browser_login) is not bool or (browser_login and (trusted_lan_no_auth or read_only)):
        raise ValueError('Invalid browser login mode')
    if browser_login:
        allowed_host = _trusted_lan_host(allowed_host)
    if type(trusted_lan_no_auth) is not bool:
        raise ValueError('Trusted LAN access must be explicitly enabled with a boolean')
    if trusted_lan_no_auth:
        if read_only:
            raise ValueError('Trusted LAN access cannot be combined with the localhost preview')
        allowed_host = _trusted_lan_host(allowed_host)
    elif not isinstance(token, str) or len(token) < 32:
        raise ValueError("Management token must contain at least 32 characters")
    keypad = KeypadService(runtime)
    keypad.progress = _JobKeypadProgress(runtime)
    commissioning = Commissioning(runtime)
    lifecycle = Lifecycle(runtime, commissioning)
    onboarding = KeypadOnboarding(runtime, keypad)
    accessory = AccessoryOnboarding(runtime)
    accessory.progress = _JobAccessoryProgress(runtime)
    anyplace = AnyplaceOnboarding(runtime)
    membership = Membership(runtime)
    rooms = Rooms(runtime)
    dimmer = DimmerEditor(runtime)
    cloud = CloudSync(runtime)
    cloud_import = cloud_import or CloudImport(runtime)
    jobs = {}
    job_results = {}
    job_operations = {}
    job_transactions = {}
    streams = set()
    scene_task = None
    scene_job = None
    instance = uuid.uuid4().hex

    active_writes = 0

    def bridge_busy(*, own_request=False, own_write=False):
        operation = runtime.operation
        return (active_writes > int(own_write) or getattr(runtime, 'maintenance', False)
                or runtime._foreground_requests > int(own_request)
                or (operation.locked() and operation.acquired_by is not runtime._background_read_task)
                or any(not task.done() for task in jobs.values())
                or (scene_task is not None and not scene_task.done())
                or runtime.diagnostics.active())

    def update_busy(*, own_request=False):
        return bridge_busy(own_request=own_request) or bool(runtime.store.journals(pending=True))

    updates = BridgeUpdates(runtime, update_busy)

    @web.middleware
    async def guard(request, handler):
        nonlocal active_writes
        if trusted_lan_no_auth or browser_login:
            # Validate every route, including the HTML and read-only endpoints.
            # Do not trust forwarded host headers or DNS aliases in this mode.
            if request.headers.getall('Host', []) != [allowed_host]:
                return web.json_response({'error': 'invalid_host'}, status=403)
            cross_site = any(value.strip().lower() == 'cross-site'
                             for value in request.headers.getall('Sec-Fetch-Site', []))
            user_navigation = (request.method in ('GET','HEAD') and request.path == '/'
                               and request.headers.getall('Sec-Fetch-Mode', []) == ['navigate']
                               and request.headers.getall('Sec-Fetch-Dest', []) == ['document']
                               and request.headers.getall('Sec-Fetch-User', []) == ['?1'])
            if cross_site and not user_navigation:
                return web.json_response({'error': 'cross_site_request'}, status=403)
            origins = request.headers.getall('Origin', [])
            if origins and origins != [request.scheme + '://' + allowed_host]:
                return web.json_response({'error': 'invalid_origin'}, status=403)
        if read_only:
            if request.host.split(':')[0] not in ('127.0.0.1', 'localhost') or request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return web.json_response({'error':'preview_localhost_only'}, status=403)
            if request.method not in ('GET', 'HEAD'):
                return web.json_response({'error':'read_only_preview'}, status=403)
        if browser_login:
            from .browser_auth import authorized, challenge
            if not authorized(request.headers.get('Authorization', ''), token):
                return challenge()
        if request.path != "/" and not trusted_lan_no_auth and not browser_login:
            provided = request.headers.get("Authorization", "")
            if not hmac.compare_digest(provided.encode(), ("Bearer " + token).encode()):
                return web.json_response({"error": "unauthorized"}, status=401)
            if request.method == "POST":
                origin = request.headers.get("Origin")
                if origin and origin != str(request.url.origin()):
                    return web.json_response({"error": "invalid_origin"}, status=403)
        if request.method == 'POST' and getattr(runtime, 'maintenance', False):
            if updates.restarting:
                return web.json_response({'error': 'update_in_progress', 'message': 'The bridge is restarting for its software update.'}, status=409)
            return web.json_response({'error':'restore_in_progress','message':'The bridge is restarting after restore. Reload shortly.'}, status=409)
        if request.path != '/api/backup/preview':
            request._client_max_size = 256 * 1024
        if request.path != '/api/backup/preview' and request.content_length and request.content_length > 256 * 1024:
            raise web.HTTPRequestEntityTooLarge(max_size=256*1024, actual_size=request.content_length)
        if request.method == 'POST' and (trusted_lan_no_auth or browser_login or request.path != '/'):
            if request.content_type != "application/json":
                return web.json_response({"error": "json_required"}, status=415)
        if request.method == 'POST' and request.path in ('/api/control', '/api/scenes/activate', '/api/scenes/activate-keypad-test', '/api/scenes/activate-quiet-test'):
            request[RECEIVED_MS_KEY] = runtime.clock()
        update_route = request.path.startswith('/api/settings/update')
        write = request.method == 'POST' and not update_route
        if write:
            active_writes += 1
        try:
            # Pause convenience reads while an owner request waits for the
            # current read and runs once. Diagnostics remain usable meanwhile.
            if write and request.path not in ('/api/capture', '/api/capture/action',
                    '/api/settings/mqtt', '/api/settings/mqtt/test', '/api/settings/relay',
                    '/api/cloud-import/preview', '/api/cloud-import/apply'):
                reviewed_inventory = (runtime.store.inventory() if request.path in
                    ('/api/rooms/membership', '/api/group-membership', '/api/group-membership/batch') else None)
                async with runtime.foreground_request():
                    if reviewed_inventory is not None and reviewed_inventory != runtime.store.inventory():
                        raise OperationNotStarted('inventory_changed')
                    response = await handler(request)
            else:
                response = await handler(request)
        except (BackupManagerError, InstallationBackupError, RestoreStateError) as error:
            response = web.json_response({'error': error.code, 'message': str(error)}, status=getattr(error, 'status', 400))
        except RelayConflict as error:
            response = web.json_response({'error': 'relay_unavailable', 'message': str(error)}, status=409)
        except UpdateRequestError as error:
            response = web.json_response({'error': 'update_unavailable', 'message': str(error)}, status=error.status)
        except (SettingsError, CloudImportError) as error:
            response = web.json_response({'error': error.code, 'message': str(error)}, status=error.status)
        except OperationNotStarted as error:
            response = web.json_response(error.response(), status=409)
        except ClaimValidationError as error:
            response = web.json_response({"error": "claim_not_started", "message": str(error),
                "association_started": False}, status=409)
        except AssociationError as error:
            response = web.json_response({"error": error.code, "outcome": "uncertain",
                "retry_allowed": False, "message": "Claim stopped. Review Operations before retrying or resetting."}, status=409)
        except (ValueError, KeyError, TypeError, RecursionError):
            response = web.json_response({"error": "invalid_request"}, status=400)
        except asyncio.TimeoutError:
            response = web.json_response({"error": "timeout_outcome_unknown"}, status=504)
        except RuntimeError:
            response = web.json_response({"error": "operation_unavailable_or_uncertain"}, status=409)
        finally:
            if write:
                active_writes -= 1
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    redesigned = (trusted_lan_no_auth or browser_login) and not read_only and (_WEB / 'ui.html').is_file()
    web_files = {path: ((_WEB / name).read_bytes(), kind) for path, (name, kind) in _WEB_ASSETS.items()} if redesigned else {}
    redesigned_html = (_WEB / 'ui.html').read_text(encoding='utf-8') if redesigned else None

    async def home(request):
        if redesigned:
            return web.Response(text=redesigned_html, content_type='text/html')
        return await classic(request)

    async def web_asset(request):
        if request.path not in web_files:
            raise web.HTTPNotFound()
        body, kind = web_files[request.path]
        return web.Response(body=body, content_type=kind)

    def device_rooms():
        known = {str(d['device_id']) for d in runtime.store.inventory()['devices']}
        stored = runtime.store.get('device_rooms', {})
        return {k: v for k, v in stored.items() if k in known} if isinstance(stored, dict) else {}

    async def capabilities(request):
        caps = status_snapshot().get('capabilities', {})
        return web.json_response({'bridge': 'pi-avionmesh', 'version': __version__, 'instance': instance,
            'features': {'relay_settings': isinstance(runtime.transport, BlueZTransport) and not read_only,
                         'software_updates': not read_only, 'control': caps.get('light_control') is True, 'group_control': caps.get('group_control') is True,
                         'read_states': caps.get('light_control') is True, 'flash': caps.get('light_control') is True,
                         'pair_lights': caps.get('claim') is True and runtime.capabilities.get('ordinary_claim') is True,
                         'detect_type': True, 'labels': True, 'group_metadata': not read_only,
                         'group_edit': rooms.enabled and not read_only,
                         'scenes': not read_only and runtime.capabilities.get('native_scenes', True),
                         'keypad_edit': not read_only and (runtime.capabilities.get('wired_editor') is True
                                                         or runtime.capabilities.get('battery_editor') is True),
                         'mqtt_settings': not read_only and hasattr(runtime, 'mqtt_settings'),
                         'cloud_import': not read_only,
                         'remove': not read_only and runtime.capabilities.get('removal') is True,
                         'backup': hasattr(runtime, 'backup_manager') and not read_only, 'all_lights': False,
                         'pairable_types': list(PAIRABLE_TYPES)},
            'types': {'lights': {str(k): v for k, v in LIGHTS.items()},
                      'controllers': {str(k): v for k, v in CONTROLLERS.items()},
                      'color_temp': list(COLOR_LIGHTS)}})

    def scene_snapshot():
        try:
            raw = runtime.scene_engine.status()
        except Exception:
            # A damaged saved scene record must not take the whole page down.
            # Scenes pause; lights, controllers and every other page keep working.
            logging.getLogger(__name__).exception('Saved scene records could not be read')
            return {'scenes': [], 'operations': [], 'enabled': False, 'read_only': read_only,
                'job': dict(scene_job) if scene_job else None, 'supported_models': list(COLOR_LIGHTS),
                'error': 'Saved scene records could not be read, so scenes are paused. Lights and the other pages still work.',
                'capabilities': {key: False for key in ('create', 'edit', 'remove', 'activate', 'recover', 'exposure')}}
        scenes = [dict(scene, native_status=scene.get('status'),
            status='ready' if scene.get('status') == 'verified' else scene.get('status'))
            for scene in raw.get('scenes', [])]
        operations = [dict(journal, transaction=journal['id'],
            scene_id=journal.get('detail', {}).get('scene_id'),
            operation=journal.get('detail', {}).get('action'), state=journal['status'],
            progress=journal.get('detail', {})) for journal in raw.get('operations', [])]
        enabled = not read_only and runtime.capabilities.get('native_scenes', True)
        return dict(raw, scenes=scenes, operations=operations, enabled=enabled,
            read_only=read_only, job=dict(scene_job) if scene_job else None,
            capabilities={'create': enabled, 'edit': enabled, 'remove': enabled,
                'activate': enabled, 'recover': enabled, 'exposure': enabled,
                'supported_models': raw.get('supported_models', []),
                'experimental_models': raw.get('experimental_models', []),
                'native_validated_models': raw.get('native_validated_models', [])})

    async def scenes_view(request):
        return web.json_response(scene_snapshot())

    async def scenes_action(request):
        nonlocal scene_task, scene_job
        action = request.match_info['action']
        # Closing bridge records stays possible while scenes are switched off, so
        # an unfinished scene operation can never hold the rest of the bridge.
        if not runtime.capabilities.get('native_scenes', True) and action not in ('set-aside', 'forget'):
            return web.json_response({'error': 'scenes_disabled'}, status=501)
        data = await request.json()
        fields = {'create': {'name', 'members', 'exposed'},
            'edit': {'id', 'name', 'members'}, 'remove': {'id'}, 'activate': {'id'}, 'activate-keypad-test': {'id'}, 'activate-quiet-test': {'id'},
            'recover': {'transaction'}, 'resume': {'transaction'}, 'verify': {'id'}, 'exposure': {'id', 'exposed'},
            'set-aside': {'transaction'}, 'forget': {'id'}}
        # Owner-named unpowered lights to leave unread. Lights an operation
        # writes to are refused by the engine; nothing is ever sent to a skipped light.
        skip = []
        if isinstance(data, dict) and 'skip' in data and action in ('create', 'edit', 'remove', 'verify', 'recover'):
            skip = data.pop('skip')
            if (not isinstance(skip, list) or len(skip) > 64 or len(set(map(str, skip))) != len(skip)
                    or any(type(t) is not int or not 32896 <= t <= 65407 for t in skip)):
                raise ValueError('Expected a list of lights to leave unchecked')
        if not isinstance(data, dict) or action not in fields or set(data) != fields[action]:
            raise ValueError('Expected exact scene operation fields')
        if 'id' in data and (not isinstance(data['id'], str) or len(data['id']) != 32):
            raise ValueError('Expected scene identifier')
        if 'transaction' in data and not isinstance(data['transaction'], str):
            raise ValueError('Expected operation identifier')
        if 'exposed' in data and type(data['exposed']) is not bool:
            raise ValueError('Expected boolean scene sharing')
        if scene_task is not None and not scene_task.done():
            raise OperationNotStarted('mesh_busy')
        if action == 'exposure':
            result = runtime.scene_engine.set_exposed(data['id'], data['exposed'])
            return web.json_response({'saved': True, 'result': result})
        if action in ('set-aside', 'forget'):
            # Bridge records only: no radio work, so the mesh need not be online.
            if runtime.operation.locked():
                raise OperationNotStarted('mesh_busy')
            result = (runtime.scene_engine.set_aside(data['transaction']) if action == 'set-aside'
                      else runtime.scene_engine.forget(data['id']))
            scene_job = None
            return web.json_response({'saved': True, 'result': result})
        if not runtime.mesh_ready or not runtime.transport.ready:
            raise OperationNotStarted('mesh_offline')
        if runtime.operation.locked():
            raise OperationNotStarted('mesh_busy')
        identifier, generation = uuid.uuid4().hex, runtime.generation
        scene_job = {'id': identifier, 'operation': action, 'state': 'running'}
        async def run_scene_job():
            nonlocal scene_job
            try:
                async with runtime.foreground_request():
                    if generation != runtime.generation:
                        raise OperationNotStarted('session_changed')
                    engine_started_ms = runtime.clock()
                    engine = runtime.scene_engine
                    unchecked = {'skip': skip} if skip else {}
                    if action == 'create':
                        result = await engine.create(data['name'], data['members'], data['exposed'], **unchecked)
                    elif action == 'edit':
                        result = await engine.edit(data['id'], data['name'], data['members'], **unchecked)
                    elif action == 'recover':
                        result = await engine.recover(data['transaction'], **unchecked)
                    elif action in ('remove', 'verify'):
                        result = await getattr(engine, action)(data['id'], **unchecked)
                    elif action == 'activate-keypad-test':
                        result = await engine.activate(data['id'], recall_profile='keypad_capture')
                    elif action == 'activate-quiet-test':
                        # Keep this job's foreground reservation AFTER the normal
                        # recall. Only automatic observations wait; received reports
                        # still flow. No read or setter is replayed when it ends.
                        result = await engine.activate(data['id'])
                        result['background_read_quiet_ms'] = int(SCENE_QUIET_TEST_SECONDS * 1000)
                        await asyncio.sleep(SCENE_QUIET_TEST_SECONDS)
                    elif action == 'resume':
                        result = await engine.resume(data['transaction'])
                    else:
                        result = await getattr(engine, action)(data['id'])
                    if action in ('activate', 'activate-keypad-test', 'activate-quiet-test') and isinstance(result, dict) and isinstance(result.get('timing'), dict):
                        waiting = max(0, engine_started_ms - request[RECEIVED_MS_KEY])
                        result['timing']['request_to_handler_ms'] = waiting
                        result['timing']['request_to_submission_ms'] = round(
                            waiting + result['timing']['handler_to_submission_ms'], 1)
                scene_job = {'id': identifier, 'operation': action, 'state': 'failed' if isinstance(result, dict) and result.get('requires_review') else 'complete', 'result': result}
                if scene_job['state'] == 'failed':
                    scene_job['error'] = 'Some scene changes need review. Read the saved operation before continuing.'
                    scene_job['unreachable'] = result.get('unreachable')
            except asyncio.CancelledError:
                scene_job = {'id': identifier, 'operation': action, 'state': 'failed',
                    'error': 'Scene work stopped. Review its saved operation before continuing.'}
                raise
            except (ValueError, RuntimeError, asyncio.TimeoutError) as error:
                scene_job = {'id': identifier, 'operation': action, 'state': 'failed',
                    'error': str(error) or 'Scene verification timed out. Review the saved operation.',
                    'unreachable': getattr(error, 'target', None), 'skipped': skip}
            except Exception:
                # Never leave the job marked as running: that would lock every
                # scene button until the bridge restarts. Nothing is repeated.
                logging.getLogger(__name__).exception('Scene work stopped unexpectedly')
                scene_job = {'id': identifier, 'operation': action, 'state': 'failed',
                    'error': 'Scene work stopped unexpectedly. Check Operations before another scene change.'}
            finally:
                runtime.scene_changed()
        scene_task = asyncio.create_task(run_scene_job())
        return web.json_response({'started': True, 'job': dict(scene_job)}, status=202)

    async def set_device_rooms(request):
        """Bridge-side room labels only: no radio, no inventory or mesh membership change."""
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {'rooms'} or not isinstance(data['rooms'], dict) \
                or not 1 <= len(data['rooms']) <= 256:
            raise ValueError('Expected rooms by device id')
        known = {str(d['device_id']) for d in runtime.store.inventory()['devices']}
        updates = {}
        for key, value in data['rooms'].items():
            if key not in known:
                raise ValueError('Unknown device')
            updates[key] = _clean_room(value)
        with runtime.store.transaction():
            current = {k: v for k, v in device_rooms().items()}
            for key, room in updates.items():
                if room:
                    current[key] = room
                else:
                    current.pop(key, None)
            if len(set(current.values())) > ROOM_LIMIT:
                raise ValueError('Too many rooms')
            runtime.store.set('device_rooms', current)
        return web.json_response({'saved': True, 'device_rooms': current, 'radio_commands': 0})

    async def classic(request):
        html = management_html(trusted_lan_no_auth=True) if trusted_lan_no_auth else HTML
        if read_only:
            html = html.replace('<main>', '<main><p role="status">READ-ONLY PREVIEW: Bluetooth, MQTT and device operations are disabled.</p>')
            # Ephemeral read-only token, never the real management credential.
            html = html.replace('</html>', '<script>document.getElementById("token").value='+json.dumps(token)+';document.getElementById("refresh").click();for(const id of ["discover","createGroup","import","cloudSync"]){document.getElementById(id).disabled=true;}</script></html>')
        return web.Response(text=html, content_type="text/html")

    def job_snapshot():
        result = {target: dict(job) for target, job in job_results.items()}
        for target, job in result.items():
            device = runtime.core.devices.get(target)
            if device is not None and device.product_id == 126:
                # An explicit null marks unsupported/stale progress. Old clients
                # still receive the plain phase map; new tabs use this ownership.
                job['progress'] = keypad.progress.for_job(target, job, jobs.get(target),
                    job_operations.get(target), instance)
                if job['progress'] is not None and target in job_transactions:
                    job['progress']['transaction'] = job_transactions[target]
            elif (device is not None and device.product_id == 91
                    and job_operations.get(target) in ('accessory_setup', 'accessory_native_setup', 'accessory_recovery')):
                job['progress'] = accessory.progress.for_job(target, job, jobs.get(target),
                    job_operations.get(target), instance)
        return result

    def status_snapshot():
        status = runtime.status()
        bootstrap = status.get('bootstrap', {})
        direct_first = bootstrap.get('needs_first_claim') and not bootstrap.get('automatic')
        status['pairing'] = {'models': [93] if direct_first else list(PAIRABLE_TYPES),
                             'attempts_remaining': commissioning.remaining_claims()}
        return dict(status,keypad_progress=dict(keypad.progress),keypad_saves=dict(keypad.saves),
                    anyplace_progress=dict(anyplace.progress),claim_progress=dict(commissioning.progress),jobs=job_snapshot(),
                    inventory_revision=revision(runtime.store),instance=instance,
                    startup_read=getattr(runtime, 'startup_read_progress', None),
                    authentication_required=not trusted_lan_no_auth)

    async def status(request):
        return web.json_response(status_snapshot())

    def snapshot_data():
        data = dict(status=status_snapshot(), inventory=runtime.store.inventory(),
                    device_states=runtime.device_states(), last_known_device_states=runtime.last_known_device_states(),
                    initial_light_status=runtime.initial_light_status(),
                    power_read_status=runtime.power_read_status(),
                    bindings=runtime.core.controllers.bindings,
                    controllers=controller_workflows(runtime, read_only=read_only))
        data['ha_readiness'] = assess_ha(data)
        data['device_rooms'] = device_rooms()
        data['scene_workflow'] = scene_snapshot()
        data['group_workflow'] = rooms.overview()
        if read_only:
            data['group_workflow'].update(programming_enabled=False, disabled_reason='Read-only preview')
        return data

    async def snapshot_view(request):
        return web.json_response(snapshot_data())

    async def rooms_view(request):
        result = rooms.overview()
        if read_only:
            result.update(programming_enabled=False, disabled_reason='Read-only preview')
        return web.json_response(result)

    async def rooms_membership(request):
        if not rooms.enabled:
            return web.json_response({'error': 'verified_membership_disabled'}, status=501)
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {'targets', 'group', 'member'}:
            raise ValueError('Expected exact room membership fields')
        return web.json_response(await rooms.change(data['targets'], data['group'], data['member']))

    async def metadata(request):
        data = await request.json()
        if runtime.operation.locked():
            raise RuntimeError('Busy')
        async with runtime.operation:
            result = edit_metadata(runtime.store,data)
            reload_inventory(runtime)
        return web.json_response(result)

    async def cancel(request):
        data = await request.json()
        if set(data) != {'transaction'}:
            raise ValueError('Expected transaction')
        if runtime.operation.locked() or any(not job.done() for job in jobs.values()):
            raise RuntimeError('Cannot cancel an operation owned by an active job')
        async with runtime.operation:
            result = cancel_unattempted(runtime.store,data['transaction'])
            runtime.session()
            runtime.emit(runtime.discovery())
        return web.json_response(result)

    async def inventory(request):
        return web.json_response(runtime.store.inventory())

    async def import_inventory(request):
        data = await request.json()
        if runtime.operation.locked():
            raise RuntimeError("Busy")
        async with runtime.operation:
            old_topics = {m["topic"] for m in runtime.discovery() if m["topic"].startswith("homeassistant/")}
            runtime.store.import_inventory(data)
            runtime.core = Bridge(load_devices(data["devices"]), runtime.key, runtime.core.node)
            runtime.core.controllers.local_modes = set(runtime.store.get("local_modes", []))
            new_topics = {m["topic"] for m in runtime.discovery() if m["topic"].startswith("homeassistant/")}
            tombstones = set(runtime.store.get("discovery_tombstones", [])) | (old_topics - new_topics)
            runtime.store.set("discovery_tombstones", sorted(tombstones))
            runtime.session()
            for topic in tombstones - new_topics:
                runtime.emit([runtime.core.intent(topic, "", True)])
            runtime.emit(runtime.discovery())
            return web.json_response({"saved": True, "physical_memberships_changed": False})

    async def control(request):
        data = await request.json()
        if (not isinstance(data, dict) or not {"target", "values"} <= set(data)
                or set(data) - {"target", "values", "confirm"}
                or type(data.get("confirm", True)) is not bool):
            raise ValueError("Invalid fields")
        return web.json_response(await runtime.control(
            data["target"], data["values"], confirm=data.get("confirm", True), local=True, received_ms=request[RECEIVED_MS_KEY]))

    async def read_light(request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {"target"} or type(data["target"]) is not int:
            raise ValueError("Expected light target")
        return web.json_response(await runtime.read_light(data["target"]))

    async def read_keypad(request):
        data = await request.json()
        if set(data) != {"target"} or type(data["target"]) is not int:
            raise ValueError("Invalid fields")
        target = data["target"]
        if target not in runtime.core.devices or runtime.core.devices[target].product_id not in (126, 127):
            raise ValueError("Not a scene keypad")
        if runtime.operation.locked() or target in jobs and not jobs[target].done():
            raise RuntimeError("Busy")
        return web.json_response(await start_job(target,lambda:keypad.read(target), operation='keypad_read'),status=202)

    async def bindings(request):
        return web.json_response(runtime.core.controllers.bindings)

    async def preview_edit(request):
        data = await request.json()
        if set(data) - {"target", "slot", "mode", "group", "brightness", "revision"}:
            raise ValueError("Invalid fields")
        table = runtime.core.controllers.bindings.get(data["target"])
        if not table or table["revision"] != data["revision"]:
            raise ValueError("A fresh matching assignment table is required")
        if data.get("mode") == "native":
            group = next((g for g in runtime.store.inventory()["groups"] if g["group_id"] == data.get("group")), None)
            if group is None or group["protected"]:
                raise ValueError("Expected known native group")
        try:
            plan, managed = keypad._plan(data['target'], table, data["slot"], data["mode"],
                                          data.get("group"), data.get("brightness"))
        except ProtectedAssignmentRefused:
            # Classic review still shows the proposed ordinary edit and explains
            # why it cannot run. The writer continues to refuse this source.
            plan = plan_edit(table, data["slot"], data["mode"], data.get("group"), data.get("brightness"))
            managed = None
        product = runtime.core.devices[data['target']].product_id
        enabled = runtime.capabilities.get('wired_editor' if product==127 else 'battery_editor',False)
        old_target = table['slots'][data['slot'] - 1]['target']
        protected = any(g['protected'] and g['group_id'] == old_target for g in runtime.store.inventory()['groups']) and not managed
        reason = ('read_only_preview' if read_only or getattr(runtime, 'preview_only', False) else
                  'editor_transport_not_enabled' if not enabled else
                  'protected_assignment_requires_lifecycle' if protected else
                  'mesh_offline' if not runtime.mesh_ready else
                  'operation_in_progress' if runtime.operation.locked() else
                  'resolve_pending_operations' if runtime.store.journals(pending=True) else
                  'capability_enabled')
        return web.json_response(dict(plan, executable=reason == 'capability_enabled', reason=reason))

    async def local_mode(request):
        data = await request.json()
        if set(data) != {"target", "enabled"} or type(data["enabled"]) is not bool:
            raise ValueError("Invalid mode request")
        target = data["target"]
        if type(target) is not int or target not in runtime.core.devices or runtime.core.devices[target].product_id != 91:
            raise ValueError("Local reports apply only to Accessory 91")
        if runtime.operation.locked() or runtime.store.journals(pending=True):
            raise RuntimeError('Local interpretation cannot change during a managed operation')
        modes = set(runtime.store.get("local_modes", []))
        modes.add(target) if data["enabled"] else modes.discard(target)
        runtime.store.set("local_modes", sorted(modes))
        runtime.core.controllers.local_modes = modes
        runtime.core.controllers.baselines.pop(target, None)
        runtime.core.controllers.native_baselines.pop(target, None)
        runtime.core.controllers.native_targets.pop(target, None)
        if hasattr(runtime, 'controller_events'):
            runtime.controller_events.pop(target, None)
        runtime.emit(runtime.discovery())
        return web.json_response({"saved": True, "physical_assignment_changed": False})

    async def journals(request):
        return web.json_response({"transactions": [dict(j,recovery=recovery_advice(j)) for j in runtime.store.journals()]})

    async def bootstrap_scan(request):
        if not getattr(runtime,'bootstrap',None) or not runtime.capabilities.get('ordinary_claim'):
            return web.json_response({'error':'bootstrap_disabled'},status=501)
        if getattr(runtime.bootstrap,'automatic',False):
            return web.json_response({'error':'automatic_bootstrap'},status=409)
        data=await request.json()
        if data!={}:raise ValueError('Expected empty scan request')
        return web.json_response(await runtime.bootstrap.scan())

    async def bootstrap_select(request):
        if not getattr(runtime,'bootstrap',None) or not runtime.capabilities.get('ordinary_claim'):
            return web.json_response({'error':'bootstrap_disabled'},status=501)
        if getattr(runtime.bootstrap,'automatic',False):
            return web.json_response({'error':'automatic_bootstrap'},status=409)
        data=await request.json()
        if not isinstance(data,dict) or set(data)!={'address','owner_reset'}:raise ValueError('Expected first-device declaration')
        return web.json_response(await runtime.bootstrap.select(data['address'],data['owner_reset']))

    async def discover(request):
        if not runtime.capabilities.get("ordinary_claim"):
            return web.json_response({"error": "commissioning_capability_disabled"}, status=501)
        devices = await commissioning.discover()
        return web.json_response({"devices": devices, "candidates": commissioning.candidate_snapshot(),
                                  "truncated": commissioning.scan_truncated})

    async def claim(request):
        if not runtime.capabilities.get("ordinary_claim"):
            return web.json_response({"error": "commissioning_capability_disabled"}, status=501)
        data = await request.json()
        if set(data) - {"uuid", "name", "product_id", "setup_ha"} or type(data.get("setup_ha",False)) is not bool:
            raise ValueError("Invalid claim fields")
        if data.get('setup_ha'):
            flag = {81:'anyplace_onboarding',91:'accessory_onboarding',127:'wired_onboarding',126:'battery_onboarding'}.get(data['product_id'])
            if not flag or not runtime.capabilities.get(flag):
                raise ValueError('Requested setup capability is disabled')
        existing_receipts = {j['id'] for j in runtime.store.journals()}
        try:
            result = await commissioning.claim(data["uuid"], data["name"], data["product_id"],
                inventory_complete=runtime.capabilities.get("inventory_complete", False),
                setup_requested=data.get('setup_ha',False), refresh_identity=True)
        except (ValueError, RuntimeError, asyncio.TimeoutError):
            # Recover only this request's newly-created receipt. Never retry an
            # association or act on a different user's pending claim.
            pending = [j for j in runtime.store.journals(pending=True)
                       if j['id'] not in existing_receipts and j['kind']=='claim'
                       and j['detail'].get('uuid') == data['uuid']]
            if len(pending) != 1 or getattr(getattr(runtime,'bootstrap',None),'automatic',False) is not True:
                raise
            recovery = await commissioning.recover_safe_pending(pending[0]['id'])
            if recovery['outcome'] == 'verified':
                result = dict(recovery['result'], recovery_completed=True)
            elif recovery['outcome'] == 'pre_key_retired' and not recovery.get('requires_review'):
                return web.json_response({'error':'claim_safely_retired',
                    'message':'Pairing stopped before the bridge sent this mesh\'s network key. The failed attempt and its address are safely retained. You can start a new Claim when ready; no reset or retry was sent.',
                    'association_retried':False,'reset_dispatched':False},status=409)
            else:
                return web.json_response({'error':'claim_verification_pending',
                    'transaction':pending[0]['id'],
                    'message':'Pairing could not be verified automatically. The attempt is safely retained; do not reset or pair this light again.',
                    'association_retried':False,'reset_dispatched':False},status=409)
        if data.get('setup_ha') and not result.get('requires_review'):
            result['setup_job'] = await start_job(result['device_id'],
                lambda: anyplace.setup(result['device_id']) if data['product_id']==81
                else accessory.setup(result['device_id']) if data['product_id']==91
                else onboarding.setup(result['device_id']), operation='accessory_setup' if data['product_id']==91 else 'keypad_setup')
        return web.json_response(result)

    async def claim_batch(request):
        if not runtime.capabilities.get('ordinary_claim') or not runtime.capabilities.get('bulk_claim'):
            return web.json_response({'error':'bulk_claim_disabled'},status=501)
        data = await request.json()
        if set(data) != {'devices'}:
            raise ValueError('Expected devices')
        return web.json_response(await commissioning.claim_many(data['devices'],
            inventory_complete=runtime.capabilities.get('inventory_complete', False)))

    async def claim_pre_key(request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {'transaction'}:
            raise ValueError('Expected retained transaction')
        return web.json_response(await commissioning.reconcile_pre_key(data['transaction']))

    async def claim_reset(request):
        data = await request.json()
        if set(data) != {'transaction', 'owner_reset'}:
            raise ValueError('Expected retained claim and reset declaration')
        return web.json_response(await commissioning.reconcile_owner_reset(data['transaction'], data['owner_reset']))

    async def start_job(target, factory, *, operation=None, transaction=None):
        if type(target) is not int or target not in runtime.core.devices:
            raise ValueError('Job requires a known integer device ID')
        if target in jobs and not jobs[target].done():
            raise RuntimeError('Target job already active')
        if target not in jobs and len(jobs)>=64:
            completed=next((t for t,job in jobs.items() if job.done()),None)
            if completed is None:raise RuntimeError('Job capacity exhausted')
            jobs.pop(completed);job_results.pop(completed,None);job_operations.pop(completed,None);job_transactions.pop(completed,None)
        identifier = uuid.uuid4().hex
        generation = runtime.generation
        job_results[target] = {'state': 'running', 'id': identifier}
        job_operations[target] = operation
        job_transactions.pop(target, None)
        if transaction is not None:
            job_transactions[target] = transaction
        def failed_result(state, error):
            result = {'state': state, 'id': identifier}
            evidence = getattr(error, 'operation_result', None)
            # This receipt comes from this job's exception, never a latest-journal
            # guess. Expose only matching managed-operation identity, not errors.
            if (isinstance(evidence, dict) and set(evidence) == {'transaction', 'mode', 'group'}
                    and isinstance(evidence['transaction'], str)
                    and evidence['mode'] in ('ha', 'native') and type(evidence['group']) is int
                    and any(j['id'] == evidence['transaction'] and j['target'] == target
                        and j['kind'] == 'dimmer_edit' and j['status'] in ('prepared', 'attempted', 'uncertain')
                        and j['detail'].get('control_schema') == 'dimmer-control-v1'
                        and j['detail'].get('mode') == evidence['mode']
                        and j['detail'].get('reservation', {}).get('group_id') == evidence['group']
                        for j in runtime.store.journals())):
                result['result'] = dict(evidence)
            return result

        async def run():
            try:
                if operation in ('accessory_setup', 'accessory_native_setup', 'accessory_recovery'):
                    accessory.progress[target] = 'preparing'
                # The HTTP reservation ends with the accepted response; keep
                # background reads paused for the asynchronous job's lifetime.
                async with runtime.foreground_request():
                    if generation != runtime.generation:
                        raise OperationNotStarted('session_changed')
                    result = await factory()
                job_results[target] = {'state':'completed','id':identifier,'result':result}
            except OperationNotStarted as error:
                job_results[target] = dict(error.response(), state='not_started', id=identifier)
            except (ValueError, RuntimeError, asyncio.TimeoutError) as error:
                job_results[target] = failed_result('needs_review', error)
            except asyncio.CancelledError as error:
                job_results[target] = failed_result('interrupted', error)
                raise
            except Exception as error:
                job_results[target] = failed_result('failed_needs_review', error)
            finally:
                if operation in ('accessory_setup', 'accessory_native_setup', 'accessory_recovery'):
                    accessory.progress.pop(target, None)
        jobs[target] = asyncio.create_task(run())
        # Enter the child's reservation while this HTTP request still owns its
        # reservation. This leaves no scheduling gap for a new background read.
        await asyncio.sleep(0)
        return {'target':target,'accepted':True,'completed':False,'job_id':identifier}

    async def assign_dimmer(request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) not in ({'target','group'}, {'target','group','baseline'}):
            raise ValueError('Expected target, group and optional reviewed baseline')
        if type(data['target']) is not int or data['target'] not in runtime.core.devices or runtime.core.devices[data['target']].product_id not in (81,91):
            raise ValueError('Expected known dimmer')
        flag = 'anyplace_editor' if runtime.core.devices[data['target']].product_id == 81 else 'dimmer_editor'
        if not runtime.capabilities.get(flag):
            return web.json_response({'error':flag+'_disabled'},status=501)
        return web.json_response(await start_job(data['target'],lambda:dimmer.assign(data['target'],data['group'],expected_baseline=data.get('baseline'))),status=202)

    def dimmer_configuration_request(data, *, reviewed=False):
        fields = {'target', 'mode'} | ({'baseline', 'revision'} if reviewed else set())
        if (not isinstance(data, dict) or set(data) not in (fields, fields | {'group'})
                or type(data.get('target')) is not int or data.get('mode') not in ('ha', 'native')
                or (data['mode'] == 'ha' and 'group' in data)
                or (data['mode'] == 'native' and type(data.get('group')) is not int)):
            raise ValueError('Expected controller, control mode and native group only for native mode')
        device = runtime.core.devices.get(data['target'])
        if device is None or device.product_id not in (81, 91):
            raise ValueError('Expected known dimmer')
        if reviewed and (not isinstance(data['baseline'], dict)
                or set(data['baseline']) != {'target', 'observed_ms', 'target_generation'}
                or any(type(value) is not int for value in data['baseline'].values())
                or not isinstance(data['revision'], str) or len(data['revision']) != 64
                or any(c not in '0123456789abcdef' for c in data['revision'])):
            raise ValueError('Expected exact reviewed dimmer baseline and revision')
        return 'anyplace_editor' if device.product_id == 81 else 'dimmer_editor'

    async def preview_dimmer_configuration(request):
        data = await request.json()
        flag = dimmer_configuration_request(data)
        if runtime.capabilities.get(flag) is not True:
            return web.json_response({'error': flag+'_disabled'}, status=501)
        return web.json_response(dimmer.preview_configuration(data['target'], data['mode'], data.get('group')))

    async def configure_dimmer(request):
        data = await request.json()
        flag = dimmer_configuration_request(data, reviewed=True)
        if runtime.capabilities.get(flag) is not True:
            return web.json_response({'error': flag+'_disabled'}, status=501)
        # No job or write has started at this boundary. The managed backend
        # repeats admission after scheduling and immediately before dispatch.
        if not runtime.mesh_ready:
            raise OperationNotStarted('mesh_offline')
        if runtime.operation.locked():
            raise OperationNotStarted('mesh_busy')
        if runtime.store.journals(pending=True):
            raise OperationNotStarted('pending_operation')
        try:
            current = dimmer.preview_configuration(data['target'], data['mode'], data.get('group'))
        except (ValueError, RuntimeError):
            raise OperationNotStarted('inventory_changed') from None
        if current['baseline'] != data['baseline'] or current['revision'] != data['revision']:
            raise OperationNotStarted('inventory_changed')
        return web.json_response(await start_job(data['target'], lambda: dimmer.configure(
            data['target'], data['mode'], data.get('group'), expected_baseline=data['baseline'],
            expected_revision=data['revision'])), status=202)

    async def accept_dimmer(request):
        data = await request.json()
        if set(data) != {'transaction'}:
            raise ValueError('Expected transaction')
        return web.json_response(dimmer.accept_observed(data['transaction']))

    async def setup_controller(request):
        data = await request.json()
        if set(data) != {'target'} or type(data['target']) is not int:
            raise ValueError('Expected controller target')
        target = data['target']
        device = runtime.core.devices.get(target)
        if device is None:
            raise ValueError('Unknown controller')
        flag = {81:'anyplace_onboarding',91:'accessory_onboarding',127:'wired_onboarding',126:'battery_onboarding'}.get(device.product_id)
        if not flag or not runtime.capabilities.get(flag):
            return web.json_response({'error':'controller_setup_disabled'},status=501)
        return web.json_response(await start_job(target,lambda: anyplace.setup(target) if device.product_id==81
            else accessory.setup(target) if device.product_id==91 else onboarding.setup(target),
            operation='accessory_setup' if device.product_id==91 else 'keypad_setup'),status=202)

    async def setup_native_controller(request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {'target', 'group'} or type(data['target']) is not int or type(data['group']) is not int:
            raise ValueError('Expected controller target and native group')
        device = runtime.core.devices.get(data['target'])
        if device is None or device.product_id != 91:
            raise ValueError('Expected Accessory Dimmer')
        if not runtime.capabilities.get('accessory_onboarding'):
            return web.json_response({'error':'controller_setup_disabled'},status=501)
        return web.json_response(await start_job(data['target'], lambda: accessory.setup_native(
            data['target'], data['group']), operation='accessory_native_setup'), status=202)

    async def removal_preview(request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {'target'}:
            raise ValueError('Expected target')
        target = data['target']
        if type(target) is not int or target not in runtime.core.devices:
            raise OperationNotStarted('removal_target_unknown')
        try:
            review = lifecycle.preview_remove(target)
        except ValueError:
            # Preview performs no writes. Give a specific retained-identity
            # explanation without exposing internal exception text.
            raise OperationNotStarted('removal_identity_unavailable') from None
        return web.json_response(review)

    async def remove(request):
        if not runtime.capabilities.get('removal'):
            raise OperationNotStarted('removal_disabled')
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {'target', 'revision'} or not isinstance(data['revision'], str):
            raise OperationNotStarted('removal_review_changed')
        # These checks precede the lifecycle call and cannot have dispatched a
        # reset. Never turn exceptions from remove_and_verify into 'not started'.
        if type(data['target']) is not int or data['target'] not in runtime.core.devices:
            raise OperationNotStarted('removal_target_unknown')
        if not runtime.mesh_ready or not runtime.transport.ready:
            raise OperationNotStarted('mesh_offline')
        if runtime.operation.locked():
            raise OperationNotStarted('mesh_busy')
        if runtime.store.journals(pending=True):
            raise OperationNotStarted('pending_operation')
        try:
            review = lifecycle.preview_remove(data['target'])
        except ValueError:
            raise OperationNotStarted('removal_identity_unavailable') from None
        if 'active_relay_requires_reconnection' in review.get('blockers', []):
            raise OperationNotStarted('removal_active_relay')
        if review.get('blockers'):
            raise OperationNotStarted('removal_native_dependency')
        if review.get('revision') != data['revision']:
            raise OperationNotStarted('removal_review_changed')
        return web.json_response(await lifecycle.remove_and_verify(data['target'], expected_revision=data['revision']))

    async def confirm_imported_removal(request):
        data = await request.json()
        if (not isinstance(data, dict) or set(data) != {'transaction', 'uuid', 'owner_confirmed'}
                or not isinstance(data['transaction'], str) or not isinstance(data['uuid'], str)
                or data['owner_confirmed'] is not True):
            raise ValueError('Expected imported removal receipt, candidate and explicit owner confirmation')
        try:
            result = await lifecycle.confirm_imported_removal(
                data['transaction'], data['uuid'], data['owner_confirmed'])
        except (ValueError, RuntimeError):
            # This endpoint never sends a reset. A failed check is still an
            # unfinished removal, not a new 'removal not started' assurance.
            return web.json_response({'error':'removal_confirmation_unavailable',
                'message':'The selected light could not be confirmed. Keep it powered on, switch off unrelated unpaired lights, and use Check what happened in Operations before confirming again. No reset was sent again.',
                'requires_review':True, 'retry_allowed':False}, status=409)
        return web.json_response(result)

    async def reset_reconcile(request):
        data = await request.json()
        if set(data) != {'transaction','owner_reset'}:
            raise ValueError('Expected reset declaration and transaction')
        return web.json_response(await lifecycle.reconcile_reset(data['transaction'],data['owner_reset']))

    async def group_membership(request):
        if not runtime.capabilities.get('group_membership'):
            return web.json_response({'error':'membership_disabled'},status=501)
        data = await request.json()
        if set(data) != {'target','group','member'}:
            raise ValueError('Expected membership fields')
        return web.json_response(await membership.change(data['target'],data['group'],data['member']))

    async def read_membership(request):
        if not runtime.capabilities.get('group_membership_readback'):
            return web.json_response({'error':'membership_readback_disabled'},status=501)
        data = await request.json()
        if set(data) != {'target'}:
            raise ValueError('Expected target')
        return web.json_response(await membership.read(data['target']))

    async def bulk_membership(request):
        if not all(runtime.capabilities.get(flag) for flag in ('group_membership', 'group_membership_readback')):
            return web.json_response({'error':'verified_membership_disabled'},status=501)
        data = await request.json()
        if set(data) != {'targets', 'group', 'member'}:
            raise ValueError('Expected batch membership fields')
        return web.json_response(await membership.change_many(data['targets'], data['group'], data['member']))

    async def sync_cloud(request):
        if not runtime.capabilities.get('cloud_sync'):
            return web.json_response({'error':'cloud_sync_disabled'},status=501)
        data = await request.json()
        if set(data) != {'email','password'}:
            raise ValueError('Expected cloud credentials')
        # Await directly so credentials are not retained in job status/closures.
        return web.json_response(await cloud.sync(data['email'],data['password']))

    def mqtt_manager():
        manager = getattr(runtime, 'mqtt_settings', None)
        if manager is None:
            raise SettingsError('settings_unavailable', 'MQTT settings are unavailable in this session.', 501)
        return manager

    async def mqtt_settings_view(request):
        return web.json_response(mqtt_manager().public())

    async def mqtt_settings_save(request):
        return web.json_response(await mqtt_manager().save(await request.json()))

    async def mqtt_settings_test(request):
        return web.json_response(await mqtt_manager().test(await request.json()))

    async def cloud_import_preview(request):
        data = await request.json()
        if not isinstance(data, dict) or not {'email', 'password'} <= set(data) \
                or set(data) - {'email', 'password', 'location_id'}:
            raise ValueError('Expected cloud credentials and optional location')
        try:
            result = await cloud_import.preview(data['email'], data['password'], data.get('location_id'))
        except CloudImportError:
            raise
        except (ValueError, OSError, ClientError, asyncio.TimeoutError):
            return web.json_response({'error': 'cloud_request_failed',
                'message': 'Cloud sign-in or inventory could not be read. Check your account details and try again.'}, status=502)
        finally:
            data.clear()
        return web.json_response(result)

    async def cloud_import_apply(request):
        return web.json_response(await cloud_import.apply(await request.json()))

    async def examine_device(request):
        data = await request.json()
        if set(data) != {'target'}:
            raise ValueError('Expected target')
        return web.json_response(await examine(runtime,data['target']))

    async def capture(request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('Expected capture scope and duration')
        if set(data) == {'group','seconds'}:
            if type(data['group']) is not int:
                raise ValueError('Expected known ordinary lighting group')
            group = next((g for g in runtime.store.inventory()['groups']
                          if g['group_id'] == data['group']), None)
            if group is None or group['protected'] or not group['members']:
                raise ValueError('Expected nonempty ordinary lighting group')
            if any(member not in runtime.core.devices
                   or runtime.core.devices[member].product_id not in LIGHTS
                   for member in group['members']):
                raise ValueError('Expected known light members')
            return web.json_response(runtime.diagnostics.start_group(
                group['group_id'], group['members'], data['seconds']))
        if set(data) != {'target','seconds'} or data['target'] not in runtime.core.devices:
            raise ValueError('Expected known capture source/duration')
        return web.json_response(runtime.diagnostics.start(data['target'],data['seconds']))

    async def capture_export(request):
        return web.json_response(runtime.diagnostics.export(request.match_info['identifier']))

    async def capture_action(request):
        data = await request.json()
        if set(data) != {'id','action'}:
            raise ValueError('Expected capture owner and action')
        if data['action']=='stop':
            result=runtime.diagnostics.export(data['id'],stop=True)
        elif data['action']=='release':
            result=runtime.diagnostics.release(data['id'])
        else:
            raise ValueError('Unknown capture action')
        return web.json_response(result)

    stream_tick = None
    stream_revision = 0
    stream_signature = None
    stream_payload = b''

    def live_frame():
        nonlocal stream_tick, stream_revision, stream_signature, stream_payload
        tick = int(asyncio.get_running_loop().time())
        if stream_tick != tick:
            snapshot = snapshot_data()
            # Preserve the actual observation times in the transmitted snapshot.
            # Advancing ages alone don't change a reading; its fresh flag still does.
            comparable = dict(snapshot)
            for section in ('device_states', 'last_known_device_states'):
                comparable[section] = {target: {field: (reading['value'], reading['observed_ms'], reading['fresh'])
                    for field, reading in fields.items()} for target, fields in snapshot[section].items()}
            comparable['controllers'] = [
                dict(row, last_event={k: v for k, v in row['last_event'].items() if k != 'age_ms'})
                if row.get('last_event') else row for row in snapshot['controllers']]
            timings = snapshot['status']['command_timings']
            comparable['status'] = dict(snapshot['status'], command_timings=dict(timings, recent=[
                {k: v for k, v in row.items() if k != 'age_ms'} for row in timings['recent']]))
            signature = json.dumps(comparable, separators=(',', ':'))
            if signature != stream_signature:
                stream_revision += 1
                stream_signature = signature
            stream_payload = json.dumps(snapshot).encode()[:-1]
            stream_tick = tick
        return stream_revision, stream_payload

    async def events(request):
        if len(streams)>=4:
            return web.json_response({'error':'too_many_event_streams'},status=503)
        task=asyncio.current_task()
        streams.add(task)
        stream_id=uuid.uuid4().hex
        response=web.StreamResponse(headers={'Content-Type':'text/event-stream','Cache-Control':'no-store',
            'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY'})
        try:
            await asyncio.wait_for(response.prepare(request),5)
            # Every connection starts with a full snapshot, never replaying old events.
            previous = None
            for index in range(300):
                frame_revision, payload = live_frame()
                # A periodic full refresh also keeps older pages and time-limited
                # editor controls current. New connections always get their own resync.
                if previous != frame_revision or index % 8 == 0:
                    prefix = f'id: {instance}:{stream_id}:{index}\nevent: snapshot\ndata: '.encode()
                    suffix = (', "stream": "' + stream_id + '", "resync": ' + ('true' if index == 0 else 'false') + '}\n\n').encode()
                    message = prefix + payload + suffix
                else:
                    message = b': heartbeat\n\n'
                previous = frame_revision
                await asyncio.wait_for(response.write(message),5)
                await asyncio.sleep(1)
        except (ConnectionError,asyncio.TimeoutError):
            pass
        finally:
            streams.discard(task)
        return response

    async def configure_keypad(request):
        data = await request.json()
        if set(data) - {"target", "slot", "mode", "group", "brightness", "revision"}:
            raise ValueError("Invalid editor fields")
        allowed = tuple(model for model, flag in ((127, "wired_editor"), (126, "battery_editor"))
                        if runtime.capabilities.get(flag))
        if not allowed:
            return web.json_response({"error": "keypad_editor_disabled"}, status=501)
        return web.json_response(await keypad.apply(data["target"], data["slot"], data["mode"],
            data.get("group"), data.get("brightness"), data["revision"], allowed_models=allowed))

    async def save_keypad(request):
        """Accept a reviewed set of button changes as one background job.

        The whole set is validated against the displayed verified table before the
        job is accepted. Each button is then its own journaled, read-back edit.
        """
        data = await request.json()
        if (not isinstance(data, dict) or set(data) != {"target", "revision", "changes"}
                or type(data["target"]) is not int or not isinstance(data["revision"], str)):
            raise ValueError("Expected target, revision and changes")
        allowed = tuple(model for model, flag in ((127, "wired_editor"), (126, "battery_editor"))
                        if runtime.capabilities.get(flag))
        if not allowed:
            return web.json_response({"error": "keypad_editor_disabled"}, status=501)
        target = data["target"]
        device = runtime.core.devices.get(target)
        if device is None or device.product_id not in allowed:
            raise ValueError("Editor capability is disabled for this model")
        table = runtime.core.controllers.bindings.get(target)
        if not table or table["revision"] != data["revision"]:
            raise ValueError("A fresh matching assignment table is required")
        keypad.plan_many(table, data["changes"], target=target)
        # Explicit refusals: nothing has been sent, so the page may say so plainly.
        if not runtime.mesh_ready:
            raise OperationNotStarted('mesh_offline')
        if runtime.operation.locked() or target in jobs and not jobs[target].done():
            raise OperationNotStarted('mesh_busy')
        if runtime.store.journals(pending=True):
            raise OperationNotStarted('pending_operation')
        return web.json_response(await start_job(target, lambda: keypad.apply_many(
            target, data["changes"], data["revision"], allowed_models=allowed), operation='keypad_save'), status=202)

    async def resume_edit(request):
        data = await request.json()
        if set(data) != {'transaction'}:
            raise ValueError('Expected transaction')
        allowed = tuple(model for model,flag in ((127,'wired_editor'),(126,'battery_editor')) if runtime.capabilities.get(flag))
        return web.json_response(await keypad.resume_edit(data['transaction'],allowed_models=allowed))

    async def resolve_keypad_setup(request):
        data = await request.json()
        if (not isinstance(data, dict) or set(data) != {'transaction'}
                or not isinstance(data['transaction'], str)):
            raise ValueError('Expected transaction')
        journal = next((j for j in runtime.store.journals() if j['id'] == data['transaction']), None)
        if journal is None or 'resolve_keypad_setup' not in recovery_advice(journal)['actions']:
            raise ValueError('An uncertain partial keypad setup is required')
        target = journal['target']
        device = runtime.core.devices.get(target)
        model = journal['detail'].get('product_id')
        if device is None or device.product_id != model or model not in (126, 127):
            raise ValueError('The retained keypad must match the setup record')
        flag = 'battery_onboarding' if model == 126 else 'wired_onboarding'
        if not runtime.capabilities.get(flag):
            return web.json_response({'error':'keypad_onboarding_disabled', 'operation_started':False}, status=501)
        if not runtime.capabilities.get('inventory_complete'):
            return web.json_response({'error':'inventory_incomplete', 'operation_started':False,
                'message':'Complete inventory is required before reading and closing this setup.'}, status=409)
        if not runtime.mesh_ready:
            raise OperationNotStarted('mesh_offline')
        if runtime.operation.locked() or target in jobs and not jobs[target].done():
            raise OperationNotStarted('mesh_busy')
        if any(j['id'] != journal['id'] for j in runtime.store.journals(pending=True)):
            raise OperationNotStarted('pending_operation')
        return web.json_response(await start_job(target,
            lambda: onboarding.resolve_partial(journal['id']),
            operation='keypad_recovery', transaction=journal['id']), status=202)

    async def reconcile(request):
        data = await request.json()
        if set(data) != {"transaction"}:
            raise ValueError("Expected transaction")
        journal = next((j for j in runtime.store.journals() if j['id']==data['transaction']),None)
        if journal is None:
            raise ValueError('Unknown transaction')
        kind, identifier = journal['kind'], journal['id']
        if kind=='scene':
            return web.json_response(await runtime.scene_engine.recover(identifier))
        if kind=='group_membership':
            return web.json_response(await membership.reconcile(identifier))
        if kind=='claim':
            return web.json_response(await commissioning.reconcile(identifier))
        if kind in ('remove','external_reset'):
            return web.json_response(await lifecycle.reconcile(identifier))
        if kind in ('keypad_setup','accessory_setup','accessory_native_setup','anyplace_setup','dimmer_edit'):
            return web.json_response(await start_job(journal['target'],lambda:
                onboarding.resume(identifier) if kind=='keypad_setup' else dimmer.reconcile(identifier) if kind=='dimmer_edit'
                else anyplace.reconcile(identifier) if kind=='anyplace_setup' else accessory.reconcile(identifier),
                operation='keypad_setup' if kind=='keypad_setup' else 'accessory_recovery' if kind in ('accessory_setup','accessory_native_setup') else None,
                transaction=identifier if kind=='keypad_setup' else None),status=202)
        return web.json_response(await keypad.reconcile(identifier))

    async def backup_action(request):
        manager = getattr(runtime, 'backup_manager', None)
        if manager is None or read_only:
            return web.json_response({'error':'backup_unavailable'}, status=501)
        action = request.match_info['action']
        if request.method == 'GET':
            return web.json_response(dict(manager.status(), owner_password_required=trusted_lan_no_auth and not manager.trusted_lan_backups))
        if trusted_lan_no_auth and not manager.trusted_lan_backups:
            from .browser_auth import authorized, backup_owner_refused
            from .service import private_text
            header = request.headers.get('Authorization', '')
            if not header or not authorized(header, private_text(manager.config['api_token_file'])):
                return backup_owner_refused()
        data = await request.json()
        return web.json_response(await getattr(manager, action)(data))

    async def relay_settings(request):
        if request.method == 'GET':
            return web.json_response(relay_status(runtime))
        return web.json_response(save_preference(runtime, await request.json()))

    async def relay_action(request):
        transport = runtime.transport
        if not isinstance(transport, AutoBlueZTransport):
            raise RelayConflict('Relay discovery requires an installation using automatic Bluetooth discovery.')
        data = await request.json()
        if bridge_busy(own_request=True, own_write=True) or runtime.operation.locked():
            raise RelayConflict('Another bridge operation is active. Try again when it finishes.')
        async with runtime.operation:
            if request.match_info['action'] == 'scan':
                extended = data == {'seconds': 60} and type(data['seconds']) is int
                if data != {} and not extended:
                    raise ValueError('Unexpected relay scan fields')
                try:
                    if extended:
                        transport.start_extended_scan()
                        return web.json_response(relay_status(runtime), status=202)
                    await transport.scan(manual=True)
                except RelayScanBusy as error:
                    raise RelayConflict(str(error)) from error
                except Exception:
                    # BlueZ and its D-Bus backend can report driver-specific errors.
                    return web.json_response({'error': 'relay_scan_failed',
                        'message': 'The scan did not finish. Previous readings are still shown.'}, status=503)
                return web.json_response(relay_status(runtime))
            if transport.extended_phase == 'running':
                raise RelayConflict('Wait for the extended scan to finish before reconnecting.')
            status = relay_status(runtime)
            if (not status['current'] or data != {'revision': status['revision'], 'current': status['current']}
                    or runtime.waiters or transport.lock.locked() or runtime.store.journals(pending=True)):
                raise RelayConflict('Reconnect unavailable. Review current operations and refresh the relay settings.')
            # Close admission synchronously. The Bluetooth loop owns disconnection and the next scan.
            runtime.mesh_ready = transport.ready = False
            return web.json_response(relay_status(runtime), status=202)

    async def software_update(request):
        if read_only:
            raise UpdateRequestError('Software updates are unavailable in preview.', 403)
        if request.method == 'GET':
            return web.json_response(await updates.status())
        data = await request.json()
        if request.match_info['action'] == 'check':
            if data != {}:
                raise ValueError('Unexpected update check fields')
            return web.json_response(await updates.check())
        return web.json_response(await updates.install(data), status=202)

    async def unsupported(request):
        return web.json_response({"error": "not_implemented", "operation": request.match_info["operation"]}, status=501)

    async def shutdown(app):
        if isinstance(runtime.transport, AutoBlueZTransport):
            await runtime.transport.stop_extended_scan()
        await updates.close()
        tasks=list(jobs.values())+list(streams)
        if scene_task is not None:
            tasks.append(scene_task)
        for job in tasks:
            job.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = web.Application(middlewares=[guard], client_max_size=MAX_HTTP_BYTES)
    app[KEYPAD_SERVICE_KEY] = keypad
    app.add_routes([web.get("/", home), web.get("/classic", classic), web.get("/ui.css", web_asset), web.get("/ui.js", web_asset),
        web.get('/settings.js', web_asset), web.get('/ops.js', web_asset), web.get('/backup.js', web_asset),
        web.get('/api/backup/{action:status}', backup_action),
        web.post('/api/backup/{action:create|preview|restore}', backup_action),
        web.get("/api/scenes", scenes_view), web.post("/api/scenes/{action:create|edit|remove|activate|activate-keypad-test|activate-quiet-test|recover|resume|verify|exposure|set-aside|forget}", scenes_action),
        web.get("/api/capabilities", capabilities), web.post("/api/device-rooms", set_device_rooms),
        web.get('/api/settings/relay', relay_settings), web.post('/api/settings/relay', relay_settings),
        web.post('/api/settings/relay/{action:scan|reconnect}', relay_action),
        web.get('/api/settings/update', software_update),
        web.post('/api/settings/update/{action:check|install}', software_update),
        web.get('/api/settings/mqtt', mqtt_settings_view), web.post('/api/settings/mqtt', mqtt_settings_save),
        web.post('/api/settings/mqtt/test', mqtt_settings_test),
        web.post('/api/cloud-import/preview', cloud_import_preview), web.post('/api/cloud-import/apply', cloud_import_apply),
        web.get("/api/status", status), web.get("/api/snapshot",snapshot_view),
        web.post("/api/metadata",metadata), web.post("/api/cancel",cancel),
        web.get("/api/rooms", rooms_view), web.post("/api/rooms/membership", rooms_membership),
        web.get("/api/inventory", inventory), web.post("/api/import", import_inventory),
        web.post("/api/control", control), web.post("/api/device/read", read_light), web.get("/api/bindings", bindings),
        web.post("/api/keypad/read", read_keypad), web.post("/api/keypad/preview", preview_edit),
        web.post("/api/controller/local-mode", local_mode), web.get("/api/transactions", journals),
        web.post("/api/claim/pre-key-retire", claim_pre_key), web.post("/api/discover", discover), web.post("/api/claim", claim),
        web.post("/api/bootstrap/scan", bootstrap_scan), web.post("/api/bootstrap/select", bootstrap_select),
        web.post("/api/claim/batch", claim_batch), web.post("/api/claim/reset-reconcile", claim_reset),
        web.post("/api/keypad-configure", configure_keypad), web.post("/api/keypad/save", save_keypad), web.post("/api/keypad/resume", resume_edit), web.post("/api/keypad/resolve-setup", resolve_keypad_setup), web.post("/api/reconcile", reconcile),
        web.post('/api/unclaim', remove), web.post('/api/unclaim/preview', removal_preview), web.post('/api/unclaim/confirm', confirm_imported_removal), web.post('/api/reset-reconcile', reset_reconcile), web.post('/api/group-membership', group_membership),
        web.post('/api/group-membership/read', read_membership), web.post('/api/group-membership/batch', bulk_membership),
        web.post('/api/controller/setup', setup_controller), web.post('/api/controller/setup-native', setup_native_controller), web.post('/api/dimmer/preview', preview_dimmer_configuration), web.post('/api/dimmer/configure', configure_dimmer), web.post('/api/dimmer/assign', assign_dimmer), web.post('/api/dimmer/accept', accept_dimmer), web.post('/api/capture', capture), web.post('/api/examine', examine_device),
        web.post('/api/capture/action',capture_action), web.get('/api/capture/{identifier}', capture_export), web.get('/api/events', events),
        web.post("/api/cloud-sync", sync_cloud), web.post("/api/{operation:factory-reset}", unsupported)])
    app.on_shutdown.append(shutdown)
    return app
