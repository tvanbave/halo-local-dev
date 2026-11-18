"""Synthetic SQLite durability cost and one-shot keypad setup boundaries."""
import asyncio
import json
import sqlite3

from avionmesh_pi.onboarding import KeypadOnboarding
from avionmesh_pi.protocol import decrypt_mcp
import test_controller_admission as admission
from test_service import KEY
import unittest


class CostedDatabase:
    def __init__(self, db, now, cost=12):
        self.db, self.now, self.cost = db, now, cost
        self.commits = []
        self.fail_stage = None
    def __getattr__(self, name):
        return getattr(self.db, name)
    def execute(self, sql, parameters=()):
        keyword = sql.split()[0].upper()
        if (self.fail_stage and keyword == 'UPDATE' and 'journals' in sql
                and parameters and self.fail_stage in str(parameters[0])):
            self.fail_stage = None
            raise sqlite3.OperationalError('Synthetic intent persistence failure')
        implicit = not self.db.in_transaction and keyword in ('UPDATE','INSERT','DELETE')
        result = self.db.execute(sql, parameters)
        if keyword == 'COMMIT' or implicit:
            self.now[0] += self.cost
            self.commits.append(self.now[0])
        return result


class OnboardingDurabilityTimingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = admission.KeypadAdmissionTests.asyncSetUp
    asyncTearDown = admission.KeypadAdmissionTests.asyncTearDown
    sleep = admission.KeypadAdmissionTests.sleep
    battery = admission.KeypadAdmissionTests.battery

    def configure(self, *, primary_delay=606, extra_primary_delay=70, cost=12):
        self.battery()
        self.runtime.capabilities = {'battery_onboarding':True,'inventory_complete':True}
        # Only Button2 is unused; already occupied slots must remain intact.
        self.transport.rows[2:] = [bytes([1,48,0,i,1,35+i,8,255]) for i in (3,4)]
        self.setup = KeypadOnboarding(self.runtime, self.keypad)
        self.store.db = CostedDatabase(self.store.db, self.now, cost)
        self.times = {}
        send = self.transport.send
        async def delayed(frame):
            p = decrypt_mcp(frame, KEY).payload
            if p[0] == 1:
                self.now[0] += 173
                await send(frame)
                if self.keypad.progress.get(40001) == 'settling':
                    self.times['selected_reply'] = self.now[0]
                    self.times['selected_commits'] = len(self.store.db.commits)
                return
            if p[0] == 2:
                label = 'secondary' if p[5] & 0x20 else 'primary'
                self.times[label] = self.now[0]
                self.times[label+'_commits'] = len(self.store.db.commits)
                # Token and attempted intent must be durable at the radio edge.
                journal = self.store.journals()[-1]
                self.assertIn('2/'+label, journal['detail']['stages'])
                self.assertGreaterEqual(self.store.get('table_token'), p[4])
                self.assertFalse(self.store.db.in_transaction)
                if label == 'primary':
                    self.now[0] += extra_primary_delay + primary_delay
                await send(frame)
                self.times[label+'_reply'] = self.now[0]
                self.times[label+'_reply_commits'] = len(self.store.db.commits)
                return
            await send(frame)
        self.transport.send = delayed

    async def test_costed_real_sqlite_commits_keep_secondary_inside_original_window(self):
        self.configure()
        result = await self.setup.setup(40001)
        self.assertTrue(result['setup_complete'])
        self.assertEqual([p[5] for p in self.transport.writes], [2,34])
        t=self.times
        self.assertEqual(t['primary_commits']-t['selected_commits'],2)
        self.assertEqual(t['secondary_commits']-t['primary_reply_commits'],2)
        self.assertEqual(t['secondary']-t['selected_reply'],724)
        self.assertEqual(self.store.journals()[-1]['detail']['replies'],{'2/primary':5,'2/secondary':1})

    async def test_slower_primary_still_blocks_secondary_without_retry(self):
        self.configure(primary_delay=751)
        with self.assertRaisesRegex(ValueError,'did not match complete proposal'):
            await self.setup.setup(40001)
        self.assertEqual([p[5] for p in self.transport.writes],[2])
        journal=self.store.journals()[-1]
        self.assertEqual(journal['status'],'uncertain')
        self.assertEqual(journal['detail']['replies']['2/primary'],5)
        self.assertIsNone(journal['detail']['replies']['2/secondary'])
        self.assertEqual(self.store.get('table_token'),2)

    async def test_intent_failure_rolls_back_token_and_never_sends_setter(self):
        self.configure()
        self.store.db.fail_stage = '2/primary'
        with self.assertRaises(sqlite3.OperationalError):
            await self.setup.setup(40001)
        self.assertEqual(self.transport.writes,[])
        self.assertEqual(self.store.get('table_token',0),0)
        self.assertFalse(self.store.db.in_transaction)
        journal=self.store.journals()[-1]
        self.assertEqual(journal['detail']['attempted'],[])
        self.assertEqual(journal['detail']['stages'],[])

    async def test_secondary_intent_failure_retains_primary_reply_and_consumed_token(self):
        self.configure()
        self.store.db.fail_stage = '2/secondary'
        with self.assertRaises(sqlite3.OperationalError):
            await self.setup.setup(40001)
        self.assertEqual([p[5] for p in self.transport.writes],[2])
        self.assertEqual(self.store.get('table_token'),1)
        j=self.store.journals()[-1]
        self.assertEqual(j['status'],'uncertain')
        self.assertEqual(j['detail']['stages'],['2/primary'])
        self.assertEqual(j['detail']['replies']['2/primary'],5)
        self.assertFalse(self.store.db.in_transaction)

    async def test_committed_secondary_intent_survives_cancellation_before_radio(self):
        self.configure()
        allocate=self.store.allocate_sequence
        def crash_after_checkpoint():
            j=self.store.journals()[-1] if self.store.journals() else None
            if j and '2/secondary' in j['detail']['stages']:
                self.assertEqual(j['detail']['replies']['2/primary'],5)
                self.assertEqual(self.store.get('table_token'),2)
                # A separate connection sees the completed atomic checkpoint,
                # even if execution dies before the next radio submission.
                database = self.store.db.execute('PRAGMA database_list').fetchone()[2]
                with sqlite3.connect(database) as reader:
                    token = json.loads(reader.execute(
                        "SELECT value FROM metadata WHERE key='table_token'").fetchone()[0])
                    durable = json.loads(reader.execute(
                        'SELECT detail FROM journals WHERE id=?', (j['id'],)).fetchone()[0])
                self.assertEqual(token, 2)
                self.assertEqual(durable['replies']['2/primary'], 5)
                self.assertEqual(durable['stages'], ['2/primary', '2/secondary'])
                raise asyncio.CancelledError()
            return allocate()
        self.store.allocate_sequence=crash_after_checkpoint
        with self.assertRaises(asyncio.CancelledError):
            await self.setup.setup(40001)
        self.assertEqual([p[5] for p in self.transport.writes],[2])
        j=self.store.journals()[-1]
        self.assertEqual(j['status'],'uncertain')
        self.assertEqual(j['detail']['stages'],['2/primary','2/secondary'])
        self.store.allocate_sequence=allocate
        self.runtime.mesh_ready=self.transport.ready=True
        self.runtime.session()
        with self.assertRaisesRegex(ValueError,'Attempted/changed slot'):
            await self.setup.resume(j['id'])
        self.assertEqual([p[5] for p in self.transport.writes],[2])
        self.assertEqual(self.store.get('table_token'),2)

    async def test_wrong_primary_status_never_allocates_or_sends_secondary(self):
        self.configure()
        exchange=self.runtime.exchange
        async def status_one(command,predicate,**kwargs):
            packet=await exchange(command,predicate,**kwargs)
            if command[3:5] == b'\x02\x30' and not command[8]&32:
                from dataclasses import replace
                return replace(packet,payload=packet.payload[:3]+b'\x01')
            return packet
        self.runtime.exchange=status_one
        with self.assertRaisesRegex(ValueError,'did not match complete proposal'):
            await self.setup.setup(40001)
        self.assertEqual([p[5] for p in self.transport.writes],[2])
        self.assertEqual(self.store.get('table_token'),1)
        self.assertEqual(self.store.journals()[-1]['detail']['replies'],{'2/primary':1})
