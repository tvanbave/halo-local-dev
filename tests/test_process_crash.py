"""Abrupt death of owned synthetic SQLite workers; no networking or hardware."""
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from avionmesh_pi.storage import Store
from avionmesh_pi.runtime import Runtime
from support import synthetic_store
from test_service import KEY,INVENTORY,FakeTransport

CHILD=r'''
import json,sys,time
sys.path.insert(0,sys.argv[1])
from avionmesh_pi.storage import Store
store=Store(sys.argv[2],bytes(range(16)))
mode=sys.argv[3]
if mode=='committed':
    sequence=store.allocate_sequence()
    identifier=store.begin('keypad_edit',40001,{'stages':['delete']})
    store.transition(identifier,'prepared','attempted')
elif mode=='rollback':
    identifier=store.begin('keypad_edit',40001,{'stages':[]})
    store.db.execute('BEGIN IMMEDIATE')
    store.set('sequence',999)
    store.db.execute("UPDATE journals SET status='attempted' WHERE id=?",(identifier,))
else:
    data=store.inventory();data['groups'][0]['exposed']=False;store.import_inventory(data)
print('checkpoint',flush=True)
time.sleep(60)
'''

class CrashTests(unittest.IsolatedAsyncioTestCase):
    def run_crash(self,mode,directory):
        process=subprocess.Popen([sys.executable,'-c',CHILD,str(Path(__file__).parents[1]),directory,mode],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertTrue(select.select([process.stdout],[],[],5)[0],'Worker never reached checkpoint')
            self.assertEqual(process.stdout.readline().strip(),'checkpoint')
            process.kill();process.communicate(timeout=5)
            self.assertNotEqual(process.returncode,0)
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=5)
            process.stdout.close();process.stderr.close()
    async def test_kill_after_committed_sequence_and_attempt_preserves_uncertainty(self):
        with tempfile.TemporaryDirectory() as folder:
            store=synthetic_store(folder,KEY);store.close()
            self.run_crash('committed',folder)
            store=Store(folder,KEY)
            try:
                self.assertEqual(store.allocate_sequence(),2)
                self.assertEqual(store.journals()[0]['status'],'uncertain')
                self.assertEqual(store.journals()[0]['detail']['stages'],['delete'])
                with self.assertRaises(ValueError):store.begin('keypad_edit',40001,{})
            finally:store.close()
    async def test_kill_inside_sqlite_transaction_rolls_back_without_phantom_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            store=synthetic_store(folder,KEY);store.close()
            self.run_crash('rollback',folder)
            store=Store(folder,KEY)
            try:
                self.assertEqual(store.get('sequence'),0)
                self.assertEqual(store.journals()[0]['status'],'prepared')
                self.assertEqual(store.allocate_sequence(),1)
            finally:store.close()
    async def test_discovery_ledger_recovers_metadata_commit_before_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            store=synthetic_store(folder,KEY);store.import_inventory(INVENTORY)
            runtime=Runtime(store,KEY,FakeTransport())
            topic='homeassistant/light/pi_lab_group_291/config'
            self.assertTrue(any(m['topic']==topic and m['payload'] for m in runtime.discovery()))
            store.close();self.run_crash('metadata',folder)
            store=Store(folder,KEY)
            try:
                runtime=Runtime(store,KEY,FakeTransport())
                self.assertTrue(any(m['topic']==topic and m['payload']=='' for m in runtime.discovery()))
            finally:store.close()
