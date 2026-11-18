"""Cross-tab battery prompts derive from the actual job task, not a phase cache."""
import asyncio
import unittest
from unittest.mock import patch

import test_http as http
from test_controller_workflow import table


@unittest.skipIf(http.create_app is None, 'HTTP dependencies run in isolated container')
class BatteryJobProgressHttpTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = http.HttpTests.asyncSetUp
    asyncTearDown = http.HttpTests.asyncTearDown

    async def exercise(self, operation, route, method):
        from avionmesh_pi.lifecycle import reload_inventory
        from avionmesh_pi.server import KEYPAD_SERVICE_KEY
        data = self.store.inventory()
        data['devices'][0]['product_id'] = 126
        self.store.import_inventory(data)
        reload_inventory(self.runtime)
        self.runtime.capabilities.update(battery_editor=True, battery_onboarding=True, inventory_complete=True)
        binding = table((65535, 500, 501, 502))
        self.runtime.core.controllers.bindings[40001] = binding
        keypad = self.client.server.app[KEYPAD_SERVICE_KEY]
        keypad.progress[40001] = 'waiting_for_wake'  # unrelated, earlier task
        begin, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def run(*args, **kwargs):
            await begin.wait()
            keypad.progress[40001] = 'waiting_for_wake'
            entered.set()
            await release.wait()
            return {'verified': True}
        body = {'target': 40001}
        if operation == 'keypad_save':
            body.update(revision=binding['revision'], changes=[{'slot':1,'mode':'native','group':291}])
        async def status():
            return (await (await self.client.get('/api/snapshot', headers=self.headers)).json())['status']
        with patch(method, side_effect=run):
            accepted = await (await self.client.post(route, headers=self.headers, json=body)).json()
            self.assertTrue(accepted['accepted'])
            self.assertIsNone((await status())['jobs']['40001']['progress'])
            begin.set()
            await asyncio.wait_for(entered.wait(), 1)
            current = await status()
            progress = current['jobs']['40001']['progress']
            self.assertEqual(progress, {'phase':'waiting_for_wake','job_id':accepted['job_id'],
                'target':40001,'product_id':126,'operation':operation,'instance':current['instance']})
            # A new snapshot has everything a refreshed or observing tab needs.
            self.assertEqual((await status())['jobs']['40001']['progress'], progress)
            self.runtime.generation += 1
            self.assertIsNone((await status())['jobs']['40001']['progress'])
            # Even a matching phase in the new session cannot be attributed to
            # this job if a different task wrote it.
            keypad.progress[40001] = 'waiting_for_wake'
            self.assertIsNone((await status())['jobs']['40001']['progress'])
            release.set()
            await asyncio.sleep(0)
            completed = (await status())['jobs']['40001']
            self.assertEqual(completed['state'], 'completed')
            self.assertIsNone(completed['progress'])
        self.assertEqual(self.transport.frames, [])

    async def test_read_progress_is_owned_and_available_to_observers(self):
        await self.exercise('keypad_read','/api/keypad/read','avionmesh_pi.server.KeypadService.read')

    async def test_save_progress_is_owned_and_available_to_observers(self):
        await self.exercise('keypad_save','/api/keypad/save','avionmesh_pi.server.KeypadService.apply_many')

    async def test_setup_progress_is_owned_and_available_to_observers(self):
        await self.exercise('keypad_setup','/api/controller/setup','avionmesh_pi.server.KeypadOnboarding.setup')
