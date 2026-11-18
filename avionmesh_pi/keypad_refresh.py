"""Bounded, read-only restoration of wired keypad mappings after a session loss."""

from .keypad import BackgroundReadDeferred


class WiredKeypadRefresh:
    """One tick belongs to Runtime's existing observation worker, never another task.

    A foreground request stops admission of the next table query. The current
    transport send is allowed to finish; only reply waits consume the read budget.
    There are at most three attempts per keypad in a mesh session. No setters,
    journal recovery, battery polling or controller exposure changes occur here.
    """

    def __init__(self, runtime, keypad, *, settle=5000, budget=10000):
        if settle < 0 or budget <= 0:
            raise ValueError('Invalid keypad refresh timing')
        self.runtime, self.keypad = runtime, keypad
        self.settle, self.budget = settle, budget
        self.generation = None
        self.schedule = {}
        self.ready_at = 0
        self.progress = {'state': 'waiting_mesh', 'target': None, 'attempts': 0}

    async def tick(self):
        r = self.runtime
        if self.generation != r.generation:
            self.generation = r.generation
            self.schedule.clear()
            self.ready_at = r.clock() + self.settle
            self.progress.update(state='settling', target=None, attempts=0)
        if not r.capabilities.get('wired_editor') or getattr(r, 'preview_only', False):
            self.progress['state'] = 'disabled'
            return
        if not (r.mesh_ready and r.transport.ready):
            self.progress['state'] = 'waiting_mesh'
            return
        if r.operation.locked() or r._foreground_requests or r.store.journals(pending=True):
            self.progress['state'] = 'waiting_operation'
            return
        if r.clock() < self.ready_at:
            self.progress['state'] = 'settling'
            return
        missing = {device.device_id for device in r.core.devices.values()
                   if device.product_id == 127
                   and device.device_id not in r.core.controllers.bindings
                   and device.device_id not in r.quarantined()}
        for target in set(self.schedule) - missing:
            del self.schedule[target]
        for target in missing:
            self.schedule.setdefault(target, (0, r.clock()))
        due = [target for target in missing
               if self.schedule[target][0] < 3 and self.schedule[target][1] <= r.clock()]
        if not due:
            self.progress['state'] = ('ready' if not missing else
                'exhausted' if all(self.schedule[t][0] >= 3 for t in missing) else 'backoff')
            return
        target = min(due, key=lambda t: (self.schedule[t][1], t))
        generation = r.generation
        attempts = self.schedule[target][0] + 1
        async with r._background_read() as admitted:
            if not admitted:
                return
            self.progress.update(state='reading', target=target, attempts=attempts)
            try:
                await self.keypad.read(target, background_deadline=r.clock() + self.budget)
                self.progress['state'] = 'verified'
            except BackgroundReadDeferred:
                # Normal user traffic must not exhaust a keypad's retry budget.
                self.progress.update(state='waiting_foreground', attempts=attempts - 1)
                if generation == r.generation:
                    self.schedule[target] = (attempts - 1, r.clock() + 1000)
                return
            except Exception:
                # This is convenience observation, never a service-fatal failure.
                # Cancellation still propagates for normal service shutdown.
                self.progress['state'] = 'failed'
        if generation == r.generation:
            self.schedule[target] = (attempts, r.clock() + (30000 if attempts == 1 else 120000))
