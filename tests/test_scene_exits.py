"""Scene reads that can be repeated, lights left unchecked, and owner exits.

Everything here uses the synthetic radio from test_scene_engine. It shows what
the bridge decides from its own records; it does not show how a physical light
behaves after a power cut.
"""
import asyncio
import copy
import tempfile
import unittest
from types import SimpleNamespace

from avionmesh_pi.protocol import Packet
from avionmesh_pi.scene_engine import (SceneEngine, SceneLightSilent, EMPTY, KEY, FLOORS,
                                       SET_ASIDE, UNCHECKED)
from support import synthetic_store
from test_scene_engine import Radio


class SceneExitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = synthetic_store(self.tmp.name, bytes(range(16)))
        self.inventory = {'devices': [
            {'device_id': 40001, 'product_id': 134, 'name': 'Synthetic bulb', 'exposed': False},
            {'device_id': 40002, 'product_id': 93, 'name': 'Synthetic downlight', 'exposed': False},
            {'device_id': 40003, 'product_id': 134, 'name': 'Synthetic lamp', 'exposed': False}], 'groups': []}
        self.store.import_inventory(self.inventory)
        self.r = Radio(self.store)
        self.r.core.devices[40003] = SimpleNamespace(device_id=40003, product_id=134)
        self.engine = SceneEngine(self.r); self.engine.gap = 0; self.engine.pair_gap = 0
        self.bulb = {'target': 40001, 'brightness': 80, 'kelvin': 4000}
        self.downlight = {'target': 40002, 'brightness': 150, 'kelvin': 3000}

    def tearDown(self): self.store.close(); self.tmp.cleanup()
    def writes(self): return [f for f in self.r.frames if f[3] in (2, 5)]
    def pending(self): return self.store.journals(pending=True)

    def silence(self, *targets, only_reads=True):
        """Lights that never answer a read, like a lamp switched off at the wall."""
        answer = self.r.exchange
        async def exchange(frame, predicate, timeout=3, *, generation=None):
            if int.from_bytes(frame[:2], 'little') in targets:
                self.r.frames.append(frame)
                raise asyncio.TimeoutError('Synthetic silent light')
            return await answer(frame, predicate, timeout, generation=generation)
        self.r.exchange = exchange
        return lambda: setattr(self.r, 'exchange', answer)

    # ---- repeated reads ----
    async def test_one_lost_read_reply_is_asked_again_and_no_write_is_repeated(self):
        answer = self.r.exchange; calls = []
        async def flaky(frame, predicate, timeout=3, *, generation=None):
            calls.append(frame)
            if len(calls) in (3, 15, 22):
                raise asyncio.TimeoutError('Synthetic lost reply')
            return await answer(frame, predicate, timeout, generation=generation)
        self.r.exchange = flaky
        result = await self.engine.create('Evening', [self.bulb, self.downlight])
        self.assertTrue(result['verified']); self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.writes()), 4)
        self.assertEqual(len({bytes(f) for f in self.writes()}), 4)

    async def test_a_light_that_never_answers_is_named_and_nothing_is_written(self):
        self.silence(40003)
        with self.assertRaises(SceneLightSilent) as caught:
            await self.engine.create('Evening', [self.bulb])
        self.assertEqual(caught.exception.target, 40003)
        self.assertIsInstance(caught.exception, asyncio.TimeoutError)
        self.assertEqual(self.writes(), []); self.assertEqual(self.pending(), [])
        asked = [f for f in self.r.frames if int.from_bytes(f[:2], 'little') == 40003]
        self.assertEqual(len(asked), self.engine.read_attempts)
        self.assertEqual(len({f[7] for f in asked}), self.engine.read_attempts)  # a fresh token each time

    # ---- lights left unchecked by the owner ----
    async def test_owner_can_leave_an_unpowered_other_light_unchecked(self):
        restore = self.silence(40003)
        result = await self.engine.create('Evening', [self.bulb, self.downlight], skip=[40003])
        self.assertTrue(result['verified'])
        self.assertFalse(any(int.from_bytes(f[:2], 'little') == 40003 for f in self.r.frames))
        journal = self.store.journals()[-1]
        self.assertEqual(journal['detail']['assumed'], [40003])
        scene = self.engine.status()['scenes'][0]
        self.assertEqual(scene['unchecked'], [40003]); self.assertTrue(scene['activation_ready'])
        await self.engine.activate(scene['id'])
        # Once the lamp is powered again, a full check clears the note.
        restore()
        self.assertEqual((await self.engine.verify(scene['id']))['unchecked'], [])
        self.assertEqual(self.engine.status()['scenes'][0]['unchecked'], [])
        self.assertEqual(self.store.get(UNCHECKED), {})

    async def test_a_light_that_is_written_can_never_be_left_unchecked(self):
        with self.assertRaises(ValueError): await self.engine.create('Evening', [self.bulb], skip=[40001])
        with self.assertRaises(ValueError): await self.engine.create('Evening', [self.bulb], skip=[49999])
        with self.assertRaises(ValueError): await self.engine.create('Evening', [self.bulb], skip=[40003, 40003])
        self.assertEqual(self.r.frames, [])
        scene = (await self.engine.create('Evening', [self.bulb, self.downlight]))['scene']; self.r.frames.clear()
        changed = [dict(self.bulb, brightness=10), self.downlight]
        with self.assertRaises(ValueError): await self.engine.edit(scene['id'], 'Evening', changed, skip=[40001])
        with self.assertRaises(ValueError): await self.engine.remove(scene['id'], skip=[40002])
        with self.assertRaises(ValueError): await self.engine.verify(scene['id'], skip=[40001, 40002])
        self.assertEqual(self.writes(), []); self.assertEqual(self.pending(), [])
        # An unchanged member may be left unread during an edit of another light.
        self.silence(40002)
        result = await self.engine.edit(scene['id'], 'Evening', changed, skip=[40002])
        self.assertTrue(result['verified'])
        self.assertEqual(self.engine.status()['scenes'][0]['unchecked'], [40002])
        self.assertTrue(all(int.from_bytes(f[:2], 'little') == 40001 for f in self.writes()))

    async def test_recovery_can_leave_an_untouched_light_unread_but_not_a_written_one(self):
        answer = self.r.exchange; state = {'dark': False}
        async def exchange(frame, predicate, timeout=3, *, generation=None):
            if state['dark'] and int.from_bytes(frame[:2], 'little') == 40003:
                raise asyncio.TimeoutError('Synthetic silent light')
            return await answer(frame, predicate, timeout, generation=generation)
        self.r.exchange = exchange
        self.r.on_send = lambda frame: state.update(dark=True)   # the lamp loses power once writing starts
        result = await self.engine.create('Evening', [self.bulb])
        self.assertFalse(result['verified']); self.assertEqual(result['unreachable'], 40003)
        written = len(self.writes()); self.assertEqual(written, 2)
        with self.assertRaises(SceneLightSilent): await self.engine.recover(result['transaction'])
        self.assertEqual(self.pending()[0]['detail']['unreachable'], 40003)
        with self.assertRaises(ValueError): await self.engine.recover(result['transaction'], skip=[40001])
        recovered = await self.engine.recover(result['transaction'], skip=[40003])
        self.assertTrue(recovered['verified']); self.assertEqual(len(self.writes()), written)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.engine.status()['scenes'][0]['unchecked'], [40003])

    # ---- a light whose reply counter starts again ----
    def restartable(self):
        """Synthetic light replies; a rejected reply is simply never delivered."""
        async def exchange(frame, predicate, timeout=3, *, generation=None):
            self.r.frames.append(frame)
            p = frame[3:]; target = int.from_bytes(frame[:2], 'little'); selector = p[5]
            row = self.r.tables.get((target, selector & 31), EMPTY)
            raw = bytes.fromhex(row['brightness' if selector & 32 else 'colour'])
            self.r.sequence[target] = self.r.sequence.get(target, 0) + 1
            reply = Packet(self.r.sequence[target], target, 32768, 115, bytes([1, 34, p[4], selector]) + raw)
            if not predicate(reply): raise asyncio.TimeoutError('Synthetic reply below the saved floor')
            return reply
        self.r.exchange = exchange

    async def test_restarted_light_is_accepted_only_after_a_second_fresh_answer(self):
        scene = (await self.engine.create('Evening', [self.bulb]))['scene']
        before = self.store.get(FLOORS)['40001']; self.assertGreater(before, 4)
        self.restartable(); self.r.sequence[40001] = 0; self.r.frames.clear()
        self.assertTrue((await self.engine.verify(scene['id']))['verified'])
        mine = [f for f in self.r.frames if int.from_bytes(f[:2], 'little') == 40001]
        self.assertEqual(len(mine), 5)                       # one unanswered question, then four normal reads
        self.assertLess(self.store.get(FLOORS)['40001'], before)
        self.assertEqual(self.writes(), [])

    async def test_a_single_low_numbered_reply_is_never_accepted(self):
        await self.engine.create('Evening', [self.bulb])
        floor = self.store.get(FLOORS)['40001']
        offered = []
        async def replayed(frame, predicate, timeout=3, *, generation=None):
            p = frame[3:]
            stale = Packet(1, 40001, 32768, 115, bytes([1, 34, p[4], p[5]]) + bytes.fromhex('020fa000'))
            offered.append(predicate(stale))                 # the same old-numbered packet every time
            raise asyncio.TimeoutError('Synthetic silence')
        self.r.exchange = replayed
        with self.assertRaises(SceneLightSilent): await self.engine._read(40001, 1, 1, self.engine._identity())
        self.assertEqual(offered, [False] * self.engine.read_attempts)
        self.assertEqual(self.store.get(FLOORS)['40001'], floor)

    # ---- set aside ----
    async def test_set_aside_closes_an_unreadable_save_without_radio_and_reserves_its_slot(self):
        self.r.fail_after_write = True
        result = await self.engine.create('Interrupted', [self.bulb, self.downlight])
        self.assertFalse(result['verified']); frames = list(self.r.frames)
        with self.assertRaises(ValueError): self.store.import_inventory(self.inventory)
        closed = self.engine.set_aside(result['transaction'])
        self.assertEqual(self.r.frames, frames)              # no radio work at all
        self.assertTrue(closed['set_aside']); self.assertTrue(closed['reference_reserved']); self.assertFalse(closed['scene_kept'])
        self.assertEqual(self.pending(), [])
        receipt = next(j for j in self.store.journals() if j['id'] == result['transaction'])
        self.assertEqual(receipt['status'], 'retired'); self.assertEqual(receipt['detail']['outcome'], 'set_aside')
        self.assertTrue(receipt['detail']['steps'][0]['attempts'])   # the receipt keeps what was tried
        status = self.engine.status()
        self.assertEqual(status['scenes'], []); self.assertEqual(status['reserved_references'], [1])
        self.store.import_inventory(self.inventory)          # the rest of the bridge is free again
        # The half-written slot is never reused, even though the first light still holds a preset there.
        self.r.drop_query = False
        again = await self.engine.create('Second try', [self.downlight])
        self.assertTrue(again['verified']); self.assertEqual(again['scene']['reference'], 2)
        with self.assertRaises(ValueError): self.engine.set_aside(result['transaction'])

    async def test_set_aside_before_anything_was_sent_reserves_nothing(self):
        async def stop(journal, generation, identity, **kwargs): return {'transaction': journal['id']}
        self.engine._execute = stop
        result = await self.engine.create('Never sent', [self.bulb])
        self.assertEqual(self.pending()[0]['status'], 'prepared')
        closed = self.engine.set_aside(result['transaction'])
        self.assertFalse(closed['reference_reserved']); self.assertEqual(self.store.get(SET_ASIDE, []), [])
        self.assertEqual(self.writes(), [])

    async def test_set_aside_edit_keeps_the_scene_blocked_until_a_full_check_passes(self):
        scene = (await self.engine.create('Evening', [self.bulb]))['scene']
        self.r.fail_before_write = True
        result = await self.engine.edit(scene['id'], 'Evening', [dict(self.bulb, brightness=10)])
        self.r.fail_before_write = False
        closed = self.engine.set_aside(result['transaction'])
        self.assertTrue(closed['scene_kept'])
        row = self.engine.status()['scenes'][0]
        self.assertTrue(row['set_aside']); self.assertEqual(row['status'], 'needs_review'); self.assertFalse(row['activation_ready'])
        self.assertEqual(row['members'], [self.bulb])        # the earlier saved settings stay on record
        with self.assertRaises(RuntimeError): await self.engine.activate(scene['id'])
        self.assertTrue((await self.engine.verify(scene['id']))['verified'])
        row = self.engine.status()['scenes'][0]
        self.assertFalse(row['set_aside']); self.assertEqual(row['status'], 'verified'); self.assertTrue(row['activation_ready'])
        self.assertEqual(self.store.get(SET_ASIDE), [])

    async def test_set_aside_waits_for_running_work_and_rejects_unknown_records(self):
        self.r.fail_after_write = True
        result = await self.engine.create('Interrupted', [self.bulb])
        async with self.r.operation:
            with self.assertRaises(RuntimeError): self.engine.set_aside(result['transaction'])
            with self.assertRaises(RuntimeError): self.engine.forget('0' * 32)
        with self.assertRaises(ValueError): self.engine.set_aside('missing')
        self.assertEqual(len(self.pending()), 1)

    # ---- forget ----
    async def test_forget_stops_using_a_scene_without_radio_and_reserves_its_slot(self):
        scene = (await self.engine.create('Evening', [self.bulb], True))['scene']
        frames = list(self.r.frames)
        gone = self.engine.forget(scene['id'])
        self.assertEqual(self.r.frames, frames)
        self.assertTrue(gone['forgotten']); self.assertEqual(gone['reference'], 1)
        self.assertEqual(self.store.get(KEY), []); self.assertEqual(self.engine.status()['reserved_references'], [1])
        self.assertEqual(self.r.tables[(40001, 1)], self.engine._values(self.bulb))   # the light was not touched
        with self.assertRaises(ValueError): self.engine.forget(scene['id'])
        with self.assertRaises(ValueError): await self.engine.activate(scene['id'])
        self.assertEqual((await self.engine.create('Fresh', [self.bulb]))['scene']['reference'], 2)

    async def test_forget_is_refused_while_that_scene_has_unfinished_work(self):
        scene = (await self.engine.create('Evening', [self.bulb]))['scene']
        self.r.fail_after_write = True
        result = await self.engine.remove(scene['id'])
        with self.assertRaises(RuntimeError): self.engine.forget(scene['id'])
        self.engine.set_aside(result['transaction'])
        self.assertTrue(self.engine.forget(scene['id'])['forgotten'])
        self.assertEqual(len(self.store.get(SET_ASIDE)), 1)   # one reservation, not two

    # ---- a light that has left the mesh ----
    async def test_scene_naming_a_departed_light_can_still_be_edited_or_removed(self):
        scene = (await self.engine.create('Evening', [self.bulb, self.downlight]))['scene']
        inventory = copy.deepcopy(self.inventory)
        inventory['devices'] = [d for d in inventory['devices'] if d['device_id'] != 40002]
        self.store.import_inventory(inventory); del self.r.core.devices[40002]
        self.r.frames.clear()
        self.assertEqual(self.engine.status()['scenes'][0]['status'], 'needs_review')
        with self.assertRaises(RuntimeError) as caught: await self.engine.verify(scene['id'])
        self.assertIn('no longer on the mesh', str(caught.exception))
        self.assertEqual(self.r.frames, [])
        with self.assertRaises(ValueError): await self.engine.edit(scene['id'], 'Evening', [self.bulb, self.downlight])
        result = await self.engine.edit(scene['id'], 'Evening', [self.bulb])
        self.assertTrue(result['verified'])
        self.assertFalse(any(int.from_bytes(f[:2], 'little') == 40002 for f in self.r.frames))
        self.assertEqual(self.engine.status()['scenes'][0]['status'], 'verified')
        self.assertTrue((await self.engine.remove(scene['id']))['verified'])


if __name__ == '__main__':
    unittest.main()
