"""Verified table reads and pure editor planning; writes remain capability gated."""
import asyncio
import copy
import hashlib
import json

from .protocol import integer
from .battery_mapping import BatteryMappingCache
from .keypad_ownership import owned_targets, reservation, used_targets, validate_managed


def table_request(target, slot):
    integer(target, 32896, 65407, "controller")
    integer(slot, 0, 4, "slot")
    payload = bytearray(8 if slot else 7)
    payload[:2] = bytes([1 if slot else 4, 48])
    if slot:
        payload[5] = slot
    return target.to_bytes(2, "little") + b"\x73" + payload


def table_write(target, slot, token, action, *, group=None, level=255):
    integer(target,32896,65407,'controller')
    integer(slot,1,4,'slot')
    integer(token,1,255,'transaction token')
    if action == 'primary':
        integer(group,256,24575,'group')
        integer(level,1,255,'preset')
        value = bytes([slot])+group.to_bytes(2,'big')+bytes([8,level])
    elif action in ('delete','secondary'):
        value = bytes([slot if action=='delete' else slot|0x20])+bytes(4)
    else:
        raise ValueError('Unknown typed table operation')
    return target.to_bytes(2,'little')+b'\x73'+bytes([5 if action=='delete' else 2,48,0,0,token])+value


def table_match(target, slot, packet):
    p = packet.payload
    return (packet.crypto_source == target and packet.opcode == 115
            and len(p) == (8 if slot else 5) and p[:3] == bytes([1 if slot else 4, 48, 0])
            and (not slot or p[3] == slot))


def snapshot(count, records):
    if len(records) != 4 or len(count) != 5 or count[:3] != b"\x04\x30\x00":
        raise ValueError("Incomplete keypad table")
    rows = []
    for slot, raw in enumerate(records, 1):
        if len(raw) != 8 or raw[:4] != bytes([1, 48, 0, slot]):
            raise ValueError("Invalid keypad slot")
        rows.append({"slot": slot, "target": int.from_bytes(raw[4:6], "big"),
                     "flags": raw[6], "level": raw[7], "raw": raw.hex()})
    data = {"count": count.hex(), "slots": rows}
    assigned = sum(row["target"] != 65535 or row["flags"] or row["level"] for row in rows)
    if count != bytes([4, 48, 0, assigned, 4]) or any(row["target"] == 0 or
            row["target"] == 65535 and (row["flags"] or row["level"]) for row in rows):
        raise ValueError("Inconsistent keypad table count or record")
    data["revision"] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return data


def plan_edit(table, slot, mode, target=None, brightness=None):
    integer(slot, 1, 4, "slot")
    current = table["slots"][slot - 1]
    unused = current["target"] == 65535 and current["flags"] == 0 and current["level"] == 0
    known = current["flags"] == 8 and 256 <= current["target"] <= 24575 and current["level"] > 0
    if not unused and not known:
        raise ValueError("Unknown records remain read-only")
    if mode not in ("native", "unassigned"):
        raise ValueError("HA target allocation requires a separate owned reservation")
    if mode == "unassigned":
        if target is not None or brightness is not None:
            raise ValueError("Unassigned mode has no target or preset")
        desired = bytes([1, 48, 0, slot, 255, 255, 0, 0])
    else:
        integer(target, 256, 24575, "target")
        if any(s["slot"] != slot and s["target"] == target for s in table["slots"]):
            raise ValueError("Duplicate targets make button identity ambiguous")
        level = brightness if brightness is not None else current["level"] if target == current["target"] else 255
        integer(level, 1, 255, "preset")
        desired = bytes([1, 48, 0, slot]) + target.to_bytes(2, "big") + bytes([8, level])
    return {"revision": table["revision"], "slot": slot, "original": current["raw"],
            "desired": desired.hex(), "changed": desired.hex() != current["raw"]}


def replace_slot(table, slot, raw):
    """Return the complete verified-table shape with one slot record replaced."""
    records = [bytes.fromhex(s["raw"]) for s in table["slots"]]
    records[integer(slot, 1, 4, "slot") - 1] = raw
    assigned = sum(record[4:8] != b"\xff\xff\0\0" for record in records)
    return snapshot(bytes([4, 48, 0, assigned, 4]), records)


def unused_record(slot):
    return bytes([1, 48, 0, slot, 255, 255, 0, 0])


class AdmissionExpired(RuntimeError):
    """A write window closed before dispatch. The setter was not transmitted."""


class BackgroundReadDeferred(RuntimeError):
    """A foreground request took priority at a safe read boundary."""


class ProtectedAssignmentRefused(ValueError):
    """An unowned protected source is reviewable but cannot be edited."""


class KeypadService:
    def __init__(self, runtime, *, sleeper=asyncio.sleep):
        self.runtime = runtime
        self.sleep = sleeper
        self.progress = {}
        self.saves = {}
        self.wake_at = {}
        self.wake_sequences = {}
        self.battery_cache = getattr(runtime, 'battery_mapping_cache', None) or runtime.core.controllers.battery_cache
        if self.battery_cache is None:
            self.battery_cache = BatteryMappingCache(runtime)
            runtime.packet_observers.append(self.battery_cache.observe)
            runtime.session_observers.append(self.battery_cache.attach_current_router)
        runtime.battery_mapping_cache = self.battery_cache
        self.battery_cache.attach_current_router()

    def remember_binding(self, target, table):
        self.battery_cache.attach_current_router()
        self.battery_cache.remember(target, table)

    def invalidate(self, target):
        r = self.runtime
        self.battery_cache.attach_current_router()
        self.battery_cache.revoke(target, "assignment_read_or_edit_started")
        r.store.db.execute("UPDATE bindings SET verified=0 WHERE target=?", (target,))
        r.core.controllers.bindings.pop(target, None)
        r.emit(r.discovery())

    def check_session(self, generation):
        r = self.runtime
        if generation != r.generation or not r.mesh_ready or not r.transport.ready:
            raise RuntimeError('Keypad operation belongs to an offline or earlier session')

    def _battery_read_remaining(self, deadline, generation):
        # Bounds dispatch/reply acceptance. A delayed transport send may return
        # later, but cannot renew this session or admit an expired reply.
        self.check_session(generation)
        remaining = deadline - self.runtime.clock()
        if remaining <= 0:
            raise asyncio.TimeoutError('Battery read session deadline expired')
        return remaining / 1000

    async def wait_wake(self, target, generation, *, read_deadline=None):
        """Admit one fresh wake, then let its announcement settle before reading."""
        r = self.runtime
        self.check_session(generation)
        remaining = max(0, 20000 - (r.clock() - self.wake_at.get(target, -20000)))
        self.progress[target] = "cooldown" if remaining else "waiting_for_wake"
        if read_deadline is None:
            await self.sleep(remaining / 1000)
        else:
            await self.sleep(min(remaining / 1000,
                self._battery_read_remaining(read_deadline, generation)))
        self.check_session(generation)
        if read_deadline is not None:
            self._battery_read_remaining(read_deadline, generation)
        self.progress[target] = "waiting_for_wake"
        future = asyncio.get_running_loop().create_future()
        since = r.clock()
        received = {}

        def matches(packet):
            if not (packet.crypto_source == target and packet.opcode == 115
                    and len(packet.payload) == 7 and packet.payload[:2] == b"\x0b\x1c"):
                return False
            previous = self.wake_sequences.get(target)
            return (previous is None or previous[0] != generation
                    or 0 < ((packet.sequence - previous[1]) & 0xffffff) < 0x800000)

        def observe(packet, when):
            if since <= when and matches(packet):
                received[packet.sequence] = when

        waiter = (lambda p: matches(p) and p.sequence in received, future, since)
        r.packet_observers.append(observe)
        r.waiters.append(waiter)
        try:
            timeout = (120 if read_deadline is None else
                       self._battery_read_remaining(read_deadline, generation))
            packet = await asyncio.wait_for(future, timeout)
            self.check_session(generation)
            if read_deadline is not None:
                self._battery_read_remaining(read_deadline, generation)
            self.wake_at[target] = received[packet.sequence]
            self.wake_sequences[target] = (generation, packet.sequence)
        finally:
            r.waiters.remove(waiter)
            r.packet_observers.remove(observe)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()
        self.progress[target] = "settling"
        settle = max(0, 150 - (r.clock() - self.wake_at[target])) / 1000
        if read_deadline is None:
            await self.sleep(settle)
        else:
            await self.sleep(min(settle,
                self._battery_read_remaining(read_deadline, generation)))
        self.check_session(generation)
        if read_deadline is not None:
            self._battery_read_remaining(read_deadline, generation)
        if not 150 <= r.clock() - self.wake_at[target] <= 1500:
            raise RuntimeError('Battery wake expired while settling')

    def _admit_background_read(self, deadline, generation):
        self.check_session(generation)
        if self.runtime._foreground_requests:
            raise BackgroundReadDeferred('Background keypad read yielded to a foreground request')
        if self.runtime.clock() >= deadline:
            raise asyncio.TimeoutError('Background keypad read deadline expired')

    async def _read_entry(self, target, slot, battery, generation, background_deadline=None,
                          battery_deadline=None):
        # Only COUNT/READ are recoverable. Every setter has its own single dispatch.
        for attempt in range(1 if battery else 3):
            self.check_session(generation)
            options = {}
            timeout = .75 if battery else 8
            request_deadline = self.runtime.clock() + 750 if battery else None
            if battery_deadline is not None:
                timeout = min(timeout, self._battery_read_remaining(battery_deadline, generation))
                request_deadline = min(request_deadline, battery_deadline)

            def admit_battery_read():
                self.check_session(generation)
                if self.runtime.clock() >= request_deadline:
                    raise asyncio.TimeoutError('Battery table request deadline expired')

            if battery:
                options['admission'] = admit_battery_read
            if background_deadline is not None:
                self._admit_background_read(background_deadline, generation)
                timeout = min(timeout, (background_deadline - self.runtime.clock()) / 1000)
                options['admission'] = lambda: self._admit_background_read(background_deadline, generation)
            try:
                reply = await self.runtime.exchange(table_request(target, slot),
                    lambda p: table_match(target, slot, p) and
                        (not battery or self.runtime.clock() < request_deadline),
                    timeout=timeout, generation=generation, **options)
                if battery:
                    admit_battery_read()
                return reply
            except asyncio.TimeoutError:
                self.check_session(generation)
                if battery or attempt == 2:
                    raise

    async def _round(self, target, battery, background_deadline=None, *, battery_budget=None):
        r = self.runtime
        generation = r.generation
        if battery and battery_budget is None:
            battery_budget = {'deadline': r.clock() + 180000, 'retries': 0}
        for attempt in range(3 if battery else 1):
            if battery:
                await self.wait_wake(target, generation, read_deadline=battery_budget['deadline'])
            self.progress[target] = "reading"
            rows = []
            try:
                for slot in range(5):
                    reply = await self._read_entry(target, slot, battery, generation, background_deadline,
                        battery_deadline=battery_budget['deadline'] if battery else None)
                    rows.append(reply.payload)
                return snapshot(rows[0], rows[1:])
            except asyncio.TimeoutError:
                self.check_session(generation)
                if not battery or battery_budget['retries'] >= 2:
                    raise
                self._battery_read_remaining(battery_budget['deadline'], generation)
                battery_budget['retries'] += 1
                # A battery timeout invalidates the whole partial round. Retry
                # COUNT on another wake after cooldown, never a sleeping slot.

    async def prepare_write(self, target, slot, battery, generation):
        if battery:
            await self.wait_wake(target, generation)
        self.check_session(generation)
        checked = self.runtime.clock()
        received = {}

        def observe(packet, when):
            if checked <= when and table_match(target, slot, packet):
                received[id(packet)] = when

        # Slot freshness starts at the authenticated reply, not its request.
        # Both inserts retain this exact receipt timestamp; an ACK or later task
        # resume cannot renew it. Battery reads also retain their request budget
        # and the independent 1500 ms wake cap.
        self.runtime.packet_observers.append(observe)
        try:
            reply = await self.runtime.exchange(table_request(target, slot),
                lambda p: table_match(target, slot, p), timeout=.75, generation=generation)
        finally:
            self.runtime.packet_observers.remove(observe)
        if id(reply) not in received:
            raise AdmissionExpired('Selected-slot receipt timestamp unavailable')
        if battery and received[id(reply)] - checked > 750:
            raise AdmissionExpired('Selected-slot read deadline expired')
        checked = received[id(reply)]
        self.admit_write(target, battery, checked, generation)
        return reply, checked

    def admit_write(self, target, battery, checked, generation):
        self.check_session(generation)
        now = self.runtime.clock()
        if not 0 <= now - checked <= 750:
            raise AdmissionExpired('Fresh-slot admission expired')
        if battery and (self.wake_sequences.get(target, (None,))[0] != generation
                        or not 150 <= now - self.wake_at.get(target, -20000) <= 1500):
            raise AdmissionExpired('Battery wake admission expired')

    async def read(self, target, *, background_deadline=None):
        r = self.runtime
        integer(target, 32896, 65407, "controller")
        device = r.core.devices.get(target)
        if device is None or device.product_id not in (126, 127):
            raise ValueError("Table reading supports known scene keypads only")
        if background_deadline is not None and device.product_id != 127:
            raise ValueError('Background table refresh supports wired keypads only')
        if any(j['target'] == target for j in r.store.journals(pending=True)):
            raise RuntimeError('Reconcile the pending controller operation before refreshing its mapping')
        if r.operation.locked():
            raise RuntimeError("Another mesh operation is active")
        async with r.operation:
            # Revoke both live and persisted mapping until two fresh rounds pass.
            self.invalidate(target)
            try:
                second = await self.verified_rounds(target,device.product_id==126,
                                                    background_deadline=background_deadline)
                if background_deadline is not None:
                    self._admit_background_read(background_deadline, r.generation)
                r.store.save_binding(target, second)
                r.core.controllers.bindings[target] = second
                self.remember_binding(target, second)
                self.progress[target] = "verified"
                r.emit(r.discovery())
                return second
            except BaseException:
                self.progress[target] = "needs_review"
                raise

    async def verified_rounds(self, target, battery, *, background_deadline=None):
        generation=self.runtime.generation
        budget = {'deadline': self.runtime.clock() + 180000, 'retries': 0} if battery else None
        first = await self._round(target, battery, background_deadline, battery_budget=budget)
        second = await self._round(target, battery, background_deadline, battery_budget=budget)
        if budget is not None:
            self._battery_read_remaining(budget['deadline'], generation)
        if generation!=self.runtime.generation:raise RuntimeError('Table verification crossed sessions')
        if first != second:
            raise ValueError("Keypad tables disagree")
        return second

    def _token(self, *, checkpoint=None):
        """Commit a unique token and optional synchronous intent together."""
        with self.runtime.store.transaction():
            token = self.runtime.store.get("table_token", 0) + 1
            if token > 255:
                raise RuntimeError("Table transaction token space exhausted; no reuse")
            self.runtime.store.set("table_token", token)
            if checkpoint is not None:
                checkpoint()
        return token

    def _require_managed_editor(self, target, *, product_id=None):
        device = self.runtime.core.devices.get(target)
        flag = {126: 'battery_editor', 127: 'wired_editor'}.get(
            device.product_id if device else None)
        if (flag is None or self.runtime.capabilities.get(flag) is not True
                or self.runtime.capabilities.get('inventory_complete') is not True
                or (product_id is not None and device.product_id != product_id)):
            raise ValueError('Managed HA editing requires the model editor and authoritative inventory')

    def _plan(self, target, table, slot, mode, group, brightness, *, used=None, ha_target=None):
        """Resolve a reviewed destination without reserving anything or using the radio."""
        r = self.runtime
        groups = {g['group_id']: g for g in r.store.inventory()['groups']}
        row = table['slots'][integer(slot, 1, 4, 'slot') - 1]
        owned = owned_targets(r.store, target).get(row['target']) if target is not None else None
        owned = owned if owned and owned['slot'] == slot and row['flags'] == 8 and row['level'] == 255 else None
        if row['target'] in groups and groups[row['target']]['protected'] and not owned:
            raise ProtectedAssignmentRefused('Protected assignment requires its managed lifecycle')
        if mode == 'native' and (group not in groups or groups[group]['protected']):
            raise ValueError('Expected known native group')
        managed = None
        if mode == 'ha' or owned:
            self._require_managed_editor(target)
            managed = {'mode': mode, 'old': owned['reservation'] if owned else None, 'new': None}
        if mode == 'ha':
            if group is not None or brightness is not None:
                raise ValueError('Home Assistant mode chooses its own target and preset')
            if owned:
                chosen = row['target']
                managed['new'] = owned['reservation']
            else:
                used = used_targets(r.store, table) if used is None else used
                chosen = ha_target if ha_target is not None else next((i for i in range(24575, 255, -1) if i not in used), None)
                if chosen is None or chosen in used or not 256 <= chosen <= 24575:
                    raise ValueError('No free owned event target')
                if len(groups) >= 128:
                    raise ValueError('Inventory group capacity exceeded')
                managed['new'] = reservation(target, slot, chosen)
                used.add(chosen)
            plan = plan_edit(table, slot, 'native', chosen, 255)
        else:
            plan = plan_edit(table, slot, mode, group, brightness)
        return plan, managed

    async def apply(self, target, slot, mode, group, brightness, revision, *, allowed_models=(), _resume_id=None, _ha_target=None):
        r = self.runtime
        device = r.core.devices.get(target)
        if device is None or device.product_id not in allowed_models or device.product_id not in (126, 127):
            raise ValueError("Editor capability is disabled for this model")
        destination = next((g for g in r.store.inventory()["groups"] if g["group_id"] == group), None)
        if mode == "native" and (destination is None or destination["protected"]):
            raise ValueError("Expected known native group")
        if r.operation.locked() or not r.mesh_ready:
            raise RuntimeError("Mesh busy or offline")
        if any(j['id'] != _resume_id for j in r.store.journals(pending=True)):
            raise RuntimeError('Another pending transaction requires reconciliation')
        async with r.operation:
            generation = r.generation
            battery = device.product_id == 126
            self.invalidate(target)
            before = await self.verified_rounds(target, battery)
            if before["revision"] != revision:
                raise ValueError("Displayed revision is stale")
            prior = None
            if _resume_id:
                prior = next(j for j in r.store.journals() if j['id']==_resume_id)
                if (prior['kind']!='keypad_edit' or prior['status']!='uncertain' or prior['target']!=target
                        or prior['detail'].get('delete_checkpoint')!=before
                        or prior['detail']['stages']!=['delete']):
                    raise ValueError('No verified checkpoint before an unattempted insertion')
            if prior and prior['detail'].get('managed'):
                managed = prior['detail']['managed']
                self._require_managed_editor(target, product_id=prior['detail'].get('product_id'))
                validate_managed(r.store, target, prior['detail'])
                proposed = prior['detail']['desired']['slots'][slot - 1]
                plan = plan_edit(before, slot, 'unassigned' if proposed['target'] == 65535 else 'native',
                    None if proposed['target'] == 65535 else proposed['target'],
                    None if proposed['target'] == 65535 else proposed['level'])
            else:
                plan, managed = self._plan(target, before, slot, mode, group, brightness, ha_target=_ha_target)
            if not plan["changed"]:
                r.store.save_binding(target, before)
                r.core.controllers.bindings[target] = before
                r.emit(r.discovery())
                if prior:
                    r.store.transition(_resume_id,'uncertain','verified')
                self.remember_binding(target, before)
                return {"changed": False}
            records = [bytes.fromhex(s["raw"]) for s in before["slots"]]
            records[slot - 1] = bytes.fromhex(plan["desired"])
            assigned = sum(record[4:8] != b"\xff\xff\0\0" for record in records)
            desired = snapshot(bytes([4, 48, 0, assigned, 4]), records)
            if prior:
                if desired != prior['detail']['desired']:
                    raise ValueError('Resume cannot change the retained proposal')
                identifier = _resume_id
            else:
                detail = {"before": before, "desired": desired, "slot": slot, "stages": []}
                data, reserved = None, []
                if managed:
                    detail.update(managed=managed, product_id=device.product_id)
                    if managed['new'] and managed['new'] != managed['old']:
                        data = copy.deepcopy(r.store.inventory())
                        data['groups'].append(managed['new'])
                        reserved = [managed['new']['group_id']]
                identifier = r.store.begin("keypad_edit", target, detail,
                    inventory=data, reserved_targets=reserved)
            attempted = False
            checked_at = None

            async def stage(label, verb, value, expected_code):
                nonlocal attempted, checked_at
                if managed:
                    self._require_managed_editor(target, product_id=device.product_id)
                if label != "insert_secondary":
                    expected_raw = bytes.fromhex(plan["original"]) if label == "delete" else bytes([1, 48, 0, slot, 255, 255, 0, 0])
                    fresh, checked_at = await self.prepare_write(target, slot, battery, generation)
                    if fresh.payload != expected_raw:
                        raise ValueError("Selected slot changed before programming")
                token = self._token()
                action = {'delete':'delete','insert_primary':'primary','insert_secondary':'secondary'}[label]
                command = table_write(target,slot,token,action,group=int.from_bytes(value[1:3],'big'),level=value[-1])
                with r.store.transaction():
                    row = next(j for j in r.store.journals() if j["id"] == identifier)
                    validate_managed(r.store, target, row['detail'])
                    if row["status"] not in (("prepared", "attempted", "uncertain") if prior else ("prepared", "attempted")) or label in row["detail"]["stages"]:
                        raise RuntimeError("Stage already attempted")
                    row["detail"]["stages"].append(label)
                    r.store.db.execute("UPDATE journals SET status='attempted', detail=? WHERE id=?",
                                       (json.dumps(row["detail"]), identifier))
                attempted = True
                # Matches the frozen ESP editor: a token-correlated reply of any
                # status is only a hint, and a missing reply is not a failed write.
                # Nothing is resent. Two matching full-table reads decide the outcome.
                code = None
                def admit():
                    self.admit_write(target, battery, checked_at, generation)
                    if managed:
                        self._require_managed_editor(target, product_id=device.product_id)
                        receipt = next(j for j in r.store.journals() if j['id'] == identifier)
                        validate_managed(r.store, target, receipt['detail'])
                try:
                    reply = await r.exchange(command,
                        lambda p: p.crypto_source == target and p.opcode == 115
                        and len(p.payload) == 4 and p.payload[:3] == bytes([verb, 48, token]),
                        timeout=2, generation=generation,
                        admission=admit)
                    code = reply.payload[3]
                except asyncio.TimeoutError:
                    self.check_session(generation)
                except AdmissionExpired:
                    # Only the follow-up record may be skipped: its primary was sent,
                    # so the table read below still establishes what was stored.
                    if label != "insert_secondary":
                        raise
                    self.check_session(generation)
                with r.store.transaction():
                    row = next(j for j in r.store.journals() if j["id"] == identifier)
                    row["detail"].setdefault("replies", {})[label] = code
                    r.store.db.execute("UPDATE journals SET detail=? WHERE id=?",
                                       (json.dumps(row["detail"]), identifier))
                if code != expected_code:
                    unconfirmed.append(label)
                return code

            unconfirmed = []
            try:
                old = before["slots"][slot - 1]
                if old["target"] != 65535:
                    await stage("delete", 5, bytes([slot, 0, 0, 0, 0]), 1)
                    empty_records = [bytes.fromhex(s["raw"]) for s in before["slots"]]
                    empty_records[slot - 1] = bytes([1, 48, 0, slot, 255, 255, 0, 0])
                    empty = snapshot(bytes([4, 48, 0, int(bytes.fromhex(before["count"])[3]) - 1, 4]), empty_records)
                    if await self.verified_rounds(target, battery) != empty:
                        raise ValueError("Deletion did not reconcile")
                    with r.store.transaction():
                        row = next(j for j in r.store.journals() if j['id']==identifier)
                        row['detail']['delete_checkpoint'] = empty
                        r.store.db.execute('UPDATE journals SET detail=? WHERE id=?',(json.dumps(row['detail']),identifier))
                if mode != "unassigned":
                    raw = bytes.fromhex(plan["desired"])
                    # Only the observed `05` primary status permits the follow-up record.
                    if await stage("insert_primary", 2, bytes([slot]) + raw[4:], 5) == 5:
                        await stage("insert_secondary", 2, bytes([slot | 0x20, 0, 0, 0, 0]), 1)
                actual = await self.verified_rounds(target, battery)
                if actual != desired:
                    raise ValueError("Edited table does not match exact proposal")
                with r.store.transaction():
                    receipt = next(j for j in r.store.journals() if j['id'] == identifier)
                    validate_managed(r.store, target, receipt['detail'])
                    r.store.save_binding(target, actual)
                    r.store.db.execute("UPDATE journals SET status='verified' WHERE id=?", (identifier,))
                r.core.controllers.bindings[target] = actual
                self.remember_binding(target, actual)
                self.progress[target] = "verified"
                r.emit(r.discovery())
                result = {"changed": True, "verified": True, "transaction": identifier}
                if unconfirmed:
                    # Verified by readback although a reply was missing or unexpected.
                    result["verified_without_reply"] = unconfirmed
                return result
            except BaseException:
                if attempted or prior:
                    try:
                        row = next(j for j in r.store.journals() if j['id']==identifier)
                        if row['status']=='attempted':
                            r.store.transition(identifier, "attempted", "uncertain")
                    finally:
                        await r.transport.disconnect()
                        r.mesh_ready = False
                        r.session()
                else:
                    r.store.transition(identifier, "prepared", "cancelled")
                self.progress[target] = "needs_review"
                raise

    def plan_many(self, table, changes, *, target=None):
        """Validate a whole set of button changes offline, in the order they will run.

        Removals run first so a group can move between buttons. Raises before any
        radio use when a step is unsupported, repeated or would duplicate a target.
        """
        if not isinstance(changes, list) or not 1 <= len(changes) <= 4:
            raise ValueError("Expected one to four button changes")
        seen, ordered = set(), []
        for change in changes:
            if not isinstance(change, dict) or set(change) - {"slot", "mode", "group", "brightness"}:
                raise ValueError("Invalid button change")
            slot = change.get("slot")
            if type(slot) is not int or slot in seen:
                raise ValueError("Each button may appear once")
            seen.add(slot)
            ordered.append(change)
        ordered.sort(key=lambda c: (c.get("mode") != "unassigned", c["slot"]))
        groups = {g["group_id"]: g for g in self.runtime.store.inventory()["groups"]}
        used = used_targets(self.runtime.store, table)
        reservations = 0
        working, steps = table, []
        for change in ordered:
            slot, mode, group, level = (change["slot"], change.get("mode"), change.get("group"),
                                        change.get("brightness"))
            if level is not None and type(level) is not int:
                raise ValueError("Preset must be a whole number")
            plan, managed = self._plan(target, working, slot, mode, group, level, used=used)
            if plan["changed"]:
                if managed and managed['new'] and managed['new'] != managed['old']:
                    reservations += 1
                    if len(groups) + reservations > 128:
                        raise ValueError('Inventory group capacity exceeded')
                working = replace_slot(working, slot, bytes.fromhex(plan["desired"]))
                steps.append({"slot": slot, "mode": mode, "group": group, "brightness": level,
                              **({'ha_target': managed['new']['group_id']} if mode == 'ha' else {})})
        return steps, working

    async def apply_many(self, target, changes, revision, *, allowed_models=()):
        """Apply several button changes one at a time; stop at the first that is not verified.

        Every button is its own journaled, fully read-back edit. A later button is
        never started after an earlier one fails, and nothing is retried.
        """
        r = self.runtime
        device = r.core.devices.get(target)
        if device is None or device.product_id not in allowed_models or device.product_id not in (126, 127):
            raise ValueError("Editor capability is disabled for this model")
        table = r.core.controllers.bindings.get(target)
        if not table or table["revision"] != revision:
            raise ValueError("Displayed revision is stale")
        steps, final = self.plan_many(table, changes, target=target)
        state = {"buttons": [{"slot": s["slot"], "state": "waiting"} for s in steps], "state": "running"}
        self.saves[target] = state
        try:
            for index, step in enumerate(steps):
                state["buttons"][index]["state"] = "writing"
                current = r.core.controllers.bindings.get(target)
                if not current:
                    raise ValueError("Verified table was lost between buttons")
                try:
                    result = await self.apply(target, step["slot"], step["mode"], step["group"],
                                              step["brightness"], current["revision"], allowed_models=allowed_models,
                                              _ha_target=step.get('ha_target'))
                except BaseException:
                    # Without an open receipt, no setter left the bridge for this button.
                    open_receipt = any(j["target"] == target for j in r.store.journals(pending=True))
                    state["buttons"][index]["state"] = "needs_review" if open_receipt else "not_sent"
                    raise
                state["buttons"][index]["state"] = "verified"
                if result.get("verified_without_reply"):
                    state["buttons"][index]["verified_without_reply"] = True
            if steps and r.core.controllers.bindings.get(target) != final:
                raise ValueError("Final table does not match the reviewed changes")
            state["state"] = "verified"
            return {"changed": bool(steps), "verified": True, "buttons": [s["slot"] for s in steps],
                    "revision": final["revision"]}
        except BaseException:
            state["state"] = ("needs_review" if any(b["state"] == "needs_review" for b in state["buttons"])
                              else "stopped")
            for button in state["buttons"]:
                if button["state"] == "waiting":
                    button["state"] = "not_started"
            raise

    async def resume_edit(self, identifier, *, allowed_models=()):
        journal = next((j for j in self.runtime.store.journals() if j['id']==identifier),None)
        if not journal or journal['kind']!='keypad_edit' or journal['status']!='uncertain':
            raise ValueError('Expected uncertain preset edit')
        checkpoint = journal['detail'].get('delete_checkpoint')
        if not checkpoint or journal['detail']['stages']!=['delete']:
            raise ValueError('Only a verified deletion before any insertion attempt can resume')
        slot = journal['detail']['slot']
        desired = journal['detail']['desired']['slots'][slot-1]
        unused = desired['target']==65535
        mode = journal['detail'].get('managed', {}).get('mode', 'unassigned' if unused else 'native')
        return await self.apply(journal['target'],slot,mode,
            None if unused or mode == 'ha' else desired['target'],None if unused or mode == 'ha' else desired['level'],checkpoint['revision'],
            allowed_models=allowed_models,_resume_id=identifier)

    async def reconcile(self, identifier):
        r = self.runtime
        journal = next((j for j in r.store.journals() if j["id"] == identifier), None)
        if not journal or journal["kind"] != "keypad_edit" or journal["status"] != "uncertain":
            raise ValueError("Expected uncertain keypad transaction")
        target = journal["target"]
        if r.operation.locked() or not r.mesh_ready:
            raise RuntimeError("Mesh busy or offline")
        async with r.operation:
            actual = await self.verified_rounds(target, r.core.devices[target].product_id == 126)
            detail = journal["detail"]
            validate_managed(r.store, target, detail)
            if detail.get("before") is not None and actual == detail["before"]:
                # Two matching reads show the original table: nothing was stored.
                # Close the receipt (kept for history) so the owner can start a new,
                # separately verified edit. This never repeats the old write.
                detail["resolution"] = "original_table_verified_unchanged"
                with r.store.transaction():
                    r.store.save_binding(target, actual)
                    r.store.db.execute("UPDATE journals SET status='cancelled', detail=? WHERE id=? AND status='uncertain'",
                                       (json.dumps(detail), identifier))
                r.core.controllers.bindings[target] = actual
                self.remember_binding(target, actual)
                self.progress[target] = "verified"
                r.emit(r.discovery())
                return {"verified": False, "unchanged": True, "requires_review": False, "write_retried": False}
            if (detail.get("stages") == ["delete"] and "delete_checkpoint" not in detail
                    and detail.get("before") is not None and type(detail.get("slot")) is int
                    and actual != detail["desired"]
                    and actual == replace_slot(detail["before"], detail["slot"], unused_record(detail["slot"]))):
                # The removal is verified and no insertion was ever attempted, so the
                # explicit resume route may send that first insertion exactly once.
                detail["delete_checkpoint"] = actual
                r.store.db.execute("UPDATE journals SET detail=? WHERE id=? AND status='uncertain'",
                                   (json.dumps(detail), identifier))
                return {"verified": False, "requires_review": True, "resumable": True, "write_retried": False}
            if actual != detail["desired"]:
                return {"verified": False, "requires_review": True, "write_retried": False}
            with r.store.transaction():
                r.store.save_binding(target, actual)
                r.store.db.execute("UPDATE journals SET status='verified' WHERE id=? AND status='uncertain'", (identifier,))
            r.core.controllers.bindings[target] = actual
            self.remember_binding(target, actual)
            r.emit(r.discovery())
            return {"verified": True, "write_retried": False}
